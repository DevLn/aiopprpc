"""PPRPC wire format: packet parsing, framing and the protobuf subset we need.

Framing
-------
Every packet starts with a one-byte fixed header ``(message_type << 4) | flag``,
followed by a varint body length, followed by the body. UDP datagrams prepend
the magic ``Qp``; TCP carries no magic at all, which is why a TCP stream can
only be resynchronised by parsing it strictly from the start (see
``iter_tcp_packets``).

RPC body = varint(seq) varint(cmd) byte(enc << 2 | rpc_type) [varint(code) if RESP] payload
A/V body = format_byte enc_type varint(channel) varint(seq) varint(ts) varint(enc_length) payload

The RPC header fields are always in the clear -- only the payload is encrypted --
and since the per-packet AES key is derived from seq/cmd/rpc_type, every packet
carries the recipe for its own decryption.

Protobuf
--------
We never need full protobuf: no schemas exist for this device. The helpers here
build the handful of field shapes the camera accepts (pb_varint, pb_string,
pb_bytes, pb_message) and read replies either shallowly (pb_fields) or
recursively (walk/find) for the nested messages -- events, IoT commands and the
motion schedule.
"""
from dataclasses import dataclass
from typing import Final

from .const import (
    AV_PAYLOAD_PREFIX,
    PREFIX,
    UDP_MAGIC,
    Encryption,
    Flag,
    MessageType,
    RpcType,
)
from .crypto import ENC_METHODS, encrypt_av
from .exceptions import PacketError

SUPPORTED_TYPES: Final = frozenset(range(3, 9))
SUPPORTED_FLAGS: Final = frozenset(int(f) for f in Flag)
MAX_VARINT_BYTES: Final = 10

#: Wire types of the protobuf fields the camera actually uses.
WIRE_VARINT, WIRE_I64, WIRE_LEN, WIRE_I32 = 0, 1, 2, 5

#: 64-bit varints are unsigned on the wire; RSSI and the heartbeat's "-30" are
#: signed values sent through that unsigned field, so they read as huge numbers
#: (e.g. -60 arrives as 2**64 - 60) unless converted with to_signed().
UINT64_MASK: Final = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class FixedHeader:
    message_type: int
    flag: int
    length: int
    is_udp: bool
    size: int  # bytes consumed by magic + header byte + length varint


@dataclass(frozen=True, slots=True)
class RPCPacket:
    header: FixedHeader
    sequence: int
    command_id: int
    encryption_type: int
    rpc_type: int
    response_code: int | None
    payload: bytes

    @property
    def is_request(self) -> bool:
        return self.rpc_type == RpcType.REQ


@dataclass(frozen=True, slots=True)
class AVPacket:
    header: FixedHeader
    is_key_frame: bool
    media_format: int
    encryption_type: int
    channel: int
    sequence: int
    timestamp: int
    encrypted_length: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class RawPacket:
    header: FixedHeader
    payload: bytes


# --- varints -----------------------------------------------------------------
def decode_varint(data: bytes, offset: int = 0, *, max_bytes: int = MAX_VARINT_BYTES) -> tuple[int, int]:
    """Decode one unsigned protobuf-style varint; return (value, new_offset)."""
    value = 0
    for index in range(max_bytes):
        if offset >= len(data):
            raise PacketError('truncated varint')
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (7 * index)
        if byte < 0x80:
            return value, offset
    raise PacketError('varint exceeds supported size')


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    if value < 0:
        raise ValueError('varint value must be non-negative')
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def to_signed(value: int) -> int:
    """Read an unsigned 64-bit varint as the signed value it stands for."""
    return value - (1 << 64) if value >> 63 else value


def to_unsigned(value: int) -> int:
    """Encode a negative number the way the camera does: as a u64 varint."""
    return value & UINT64_MASK


# --- parsing -----------------------------------------------------------------
def parse_fixed_header(data: bytes, *, udp: bool | None = None) -> tuple[FixedHeader, int]:
    """Parse a fixed header. ``udp=None`` auto-detects the Qp magic."""
    offset = 0
    detected_udp = data.startswith(UDP_MAGIC)
    if udp is True and not detected_udp:
        raise PacketError('missing UDP magic 0x5170')
    is_udp = detected_udp if udp is None else udp
    if is_udp:
        if not detected_udp:
            raise PacketError('missing UDP magic 0x5170')
        offset = len(UDP_MAGIC)
    if offset >= len(data):
        raise PacketError('missing type/flag byte')
    type_and_flag = data[offset]
    offset += 1
    message_type = type_and_flag >> 4
    flag = type_and_flag & 0x0F
    if message_type not in SUPPORTED_TYPES:
        raise PacketError(f'unsupported message type {message_type}')
    if flag not in SUPPORTED_FLAGS:
        raise PacketError(f'unsupported flag {flag}')
    length, offset = decode_varint(data, offset, max_bytes=4)
    return FixedHeader(message_type, flag, length, is_udp, offset), offset


def _parse_rpc(header: FixedHeader, body: bytes) -> RPCPacket:
    cursor = 0
    sequence, cursor = decode_varint(body, cursor, max_bytes=4)
    command_id, cursor = decode_varint(body, cursor, max_bytes=4)
    if cursor >= len(body):
        raise PacketError('RPC packet missing encryption/type byte')
    flags = body[cursor]
    cursor += 1
    encryption_type = flags >> 2
    rpc_type = flags & 0x03
    if rpc_type not in (RpcType.REQ, RpcType.RESP):
        raise PacketError(f'unsupported RPC type {rpc_type}')
    response_code = None
    if rpc_type == RpcType.RESP:
        response_code, cursor = decode_varint(body, cursor, max_bytes=4)
    return RPCPacket(header, sequence, command_id, encryption_type, rpc_type,
                     response_code, body[cursor:])


def _parse_av(header: FixedHeader, body: bytes) -> AVPacket:
    if len(body) < 2:
        raise PacketError('AV packet missing format/encryption bytes')
    format_byte = body[0]
    encryption_type = body[1]
    cursor = 2
    channel, cursor = decode_varint(body, cursor, max_bytes=4)
    sequence, cursor = decode_varint(body, cursor, max_bytes=4)
    timestamp, cursor = decode_varint(body, cursor, max_bytes=9)
    encrypted_length, cursor = decode_varint(body, cursor, max_bytes=4)
    return AVPacket(header, bool(format_byte >> 7), format_byte & 0x7F, encryption_type,
                    channel, sequence, timestamp, encrypted_length, body[cursor:])


def _parse_raw(header: FixedHeader, body: bytes) -> RawPacket:
    return RawPacket(header, body)


#: message_type -> body parser. Heartbeat (3) and anything unknown fall through
#: to RawPacket rather than raising: a stray frame must not kill the read loop.
PARSERS = {
    int(MessageType.RPC): _parse_rpc,
    int(MessageType.JSON_RPC): _parse_rpc,
    int(MessageType.AV): _parse_av,
}


def parse_packet(data: bytes, *, udp: bool | None = None,
                 exact: bool = True) -> RPCPacket | AVPacket | RawPacket:
    """Parse one complete packet. Payloads are returned still encrypted."""
    header, offset = parse_fixed_header(data, udp=udp)
    end = offset + header.length
    if end > len(data):
        raise PacketError(
            f'truncated packet: declared {header.length} body bytes, have {len(data) - offset}')
    if exact and end != len(data):
        raise PacketError(f'trailing data: {len(data) - end} bytes')
    return PARSERS.get(header.message_type, _parse_raw)(header, data[offset:end])


def iter_tcp_packets(data: bytes) -> tuple[list, bytes]:
    """Pull every complete packet out of a TCP receive buffer.

    Returns ``(packets, remainder)``; a trailing partial packet is handed back
    as the remainder to prepend to the next read, not treated as an error.
    """
    packets = []
    offset = 0
    while offset < len(data):
        try:
            header, body_offset = parse_fixed_header(data[offset:], udp=False)
        except PacketError as error:
            if 'truncated' in str(error) or 'missing' in str(error):
                break
            raise
        packet_size = body_offset + header.length
        if len(data) - offset < packet_size:
            break
        packets.append(parse_packet(data[offset:offset + packet_size], udp=False))
        offset += packet_size
    return packets, data[offset:]


# --- protobuf builders --------------------------------------------------------
def pb_varint(field: int, value: int) -> bytes:
    return bytes([field << 3 | WIRE_VARINT]) + encode_varint(value)


def pb_signed(field: int, value: int) -> bytes:
    """A negative number in a plain varint field, the way this firmware does it."""
    return pb_varint(field, to_unsigned(value))


def pb_bytes(field: int, value: bytes) -> bytes:
    return bytes([field << 3 | WIRE_LEN]) + encode_varint(len(value)) + value


def pb_string(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode())


def pb_message(field: int, *parts: bytes) -> bytes:
    """Nest a submessage (the IoT-command and schedule bodies are two deep)."""
    return pb_bytes(field, b''.join(parts))


def pb_packed_varints(field: int, values) -> bytes:
    """A packed ``repeated int64`` field, e.g. ``BaseCmd.pint`` in ExecIOTCMD.

    proto3 packs repeated scalars into one length-delimited field holding the
    concatenated varints, so ``[0]`` is ``00`` and ``[0, 0, 0]`` is ``00 00 00``
    -- exactly the bytes the captures show for the small values in use.
    """
    payload = b''.join(encode_varint(to_unsigned(int(v))) for v in values)
    return pb_bytes(field, payload)


# --- protobuf readers ---------------------------------------------------------
def _pb_scan(data: bytes):
    """Yield ``(field, value)`` for every field in order, repeats included.

    Stops silently at the first byte that is not valid protobuf -- the callers
    all read known-good camera bodies, and a partial read beats an exception on
    a body that is merely longer than we understand.
    """
    cursor = 0
    while cursor < len(data):
        try:
            tag, cursor = decode_varint(data, cursor)
        except PacketError:
            return
        field, wire = tag >> 3, tag & 7
        if not field:
            return
        try:
            if wire == WIRE_VARINT:
                value, cursor = decode_varint(data, cursor)
            elif wire == WIRE_LEN:
                length, cursor = decode_varint(data, cursor)
                value, cursor = data[cursor:cursor + length], cursor + length
            elif wire == WIRE_I32:
                value, cursor = data[cursor:cursor + 4], cursor + 4
            elif wire == WIRE_I64:
                value, cursor = data[cursor:cursor + 8], cursor + 8
            else:
                return
        except PacketError:
            return
        yield field, value


def pb_fields(data: bytes) -> dict:
    """Shallow field map ``{field_number: int | bytes}``, first value wins.

    Length-delimited fields come back as raw bytes -- they may be a string or a
    nested message, and only the caller knows which.

    First-wins is fine for the singular bodies this reads, but it silently
    discards repeats: use pb_repeated() when a field can occur more than once.
    """
    out: dict[int, int | bytes] = {}
    for field, value in _pb_scan(data):
        out.setdefault(field, value)
    return out


def pb_repeated(data: bytes, field: int) -> list:
    """Every value of one field, in wire order -- the repeats pb_fields drops.

    Deliberately not built on walk(): walk guesses whether a length-delimited
    value is a nested message or opaque bytes by trying to parse it, and an
    SSID is exactly the kind of short arbitrary string that can parse as
    protobuf by accident. Here the caller states the field it wants and gets
    the raw bytes, with no guessing in the middle.
    """
    return [value for num, value in _pb_scan(data) if num == field]


def walk(data: bytes):
    """Recursive dump as ``[(field, kind, value)]``, or None if not protobuf.

    ``kind`` is 'V' varint, 'M' nested message, 'B' opaque bytes, 'I32'/'I64'.
    A length-delimited field is reported as 'M' when its content itself parses
    as protobuf, else 'B' -- the usual heuristic, and good enough here because
    the shapes we read (events, IoT commands, schedules) are known in advance.
    """
    out = []
    cursor = 0
    while cursor < len(data):
        try:
            tag, cursor = decode_varint(data, cursor)
        except PacketError:
            return None
        field, wire = tag >> 3, tag & 7
        if not field or field > 0x1FFFFFFF:
            return None
        try:
            if wire == WIRE_VARINT:
                value, cursor = decode_varint(data, cursor)
                out.append((field, 'V', value))
            elif wire == WIRE_LEN:
                length, cursor = decode_varint(data, cursor)
                if cursor + length > len(data):
                    return None
                chunk, cursor = data[cursor:cursor + length], cursor + length
                sub = walk(chunk) if chunk else None
                out.append((field, 'M', sub) if sub is not None else (field, 'B', chunk))
            elif wire == WIRE_I32:
                out.append((field, 'I32', data[cursor:cursor + 4]))
                cursor += 4
            elif wire == WIRE_I64:
                out.append((field, 'I64', data[cursor:cursor + 8]))
                cursor += 8
            else:
                return None
        except PacketError:
            return None
    return out


def find(tree, path):
    """Follow a field-number path through a walk() tree, e.g. ``[7, 4, 2]``."""
    node = tree
    for want in path:
        hit = None
        for field, _kind, value in node or []:
            if field == want:
                hit = value
                break
        if hit is None:
            return None
        node = hit
    return node


def as_str(value) -> str:
    return value.decode('utf-8', 'replace') if isinstance(value, (bytes, bytearray)) else str(value)


def as_int(value, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, 'little') if value else default
    return default


# --- frame builders -----------------------------------------------------------
def build_rpc(seq: int, cmd: int, body: bytes = b'', *, enc: int = Encryption.AES256,
              rpc_type: int = RpcType.REQ, code: int | None = None,
              prefix: str | bytes = PREFIX, udp: bool = False) -> bytes:
    """Frame one RPC ready for the wire, encrypting ``body`` per ``enc``.

    ``udp=True`` prepends the Qp magic -- only discovery needs it.
    """
    payload = ENC_METHODS[int(enc)][1](seq, cmd, rpc_type, body, prefix)
    frame = encode_varint(seq) + encode_varint(cmd) + bytes([int(enc) << 2 | int(rpc_type)])
    if rpc_type == RpcType.RESP:
        frame += encode_varint(code or 0)
    frame += payload
    header = bytes([(MessageType.RPC << 4) | Flag.NORMAL]) + encode_varint(len(frame))
    return (UDP_MAGIC if udp else b'') + header + frame


def build_av(session_key, channel: int, seq: int, timestamp: int, media_format: int,
             payload: bytes, *, is_key_frame: bool = False, enc: int = Encryption.AES256,
             enc_length: int | None = None, prefix: bool = False,
             flag: int = Flag.NORMAL) -> bytes:
    """Frame one outgoing A/V packet (talk-back audio).

    The defaults are the **uplink** shape, read off the vendor app's intercom
    capture: header flag 8, no ``01 00`` prefix, ciphertext from byte 0. That is
    deliberately not the downlink shape (flag 9, with the prefix) -- the camera
    sends and receives differently, and feeding it the downlink shape is one of
    the ways it reboots.
    """
    media, enc_length = encrypt_av(session_key, seq, timestamp, channel, payload, enc_length)
    if not enc_length:
        enc = Encryption.NONE
    body = bytes([(0x80 if is_key_frame else 0) | (media_format & 0x7F), int(enc)])
    body += encode_varint(channel) + encode_varint(seq) + encode_varint(timestamp)
    body += encode_varint(enc_length) + (AV_PAYLOAD_PREFIX if prefix else b'') + media
    header = bytes([(MessageType.AV << 4) | int(flag)]) + encode_varint(len(body))
    return header + body


def split_av_payload(payload: bytes) -> bytes | None:
    """Strip the A/V whole-frame prefix, returning the media bytes.

    FT04 always sends ``01 00``. Fragmenting firmwares use ``01 <index> 00``
    with index >= 1; that variant is accepted here but has never been observed
    on this hardware.
    """
    if len(payload) < 3 or payload[0] != 0x01:
        return None
    return payload[2:] if payload[1] == 0x00 else payload[3:]
