"""UDP discovery: find cameras and, crucially, mint a fresh ``lsign``.

The camera answers a broadcast ``Discovery(2600)`` datagram on UDP 20190 with
its device id, SDK version, a gzipped ``skill.json`` capability list, and
``lsign`` -- the one-shot token LanAuth needs. **lsign changes on every reply
and expires**, so a descriptor is only good for one connection attempt:
re-discover immediately before every connect and every reconnect.

The reply's own source address is what we record. The camera also reports an IP
for itself (field 2), but on a double-NATed unit that address is unreachable --
the observed hardware answered from 192.168.0.170 while claiming 192.168.3.114.
"""
import asyncio
import gzip
import json
import logging

from .const import PORT, PREFIX, Commands, RpcType
from .crypto import decrypt_rpc
from .exceptions import PacketError
from .protocol import RPCPacket, as_str, build_rpc, parse_packet, pb_fields
from .types import DeviceDescriptor

DEFAULT_DISCOVERY_ADDRESS = '255.255.255.255'

#: Fields of the Discovery reply (docs/FT04_BK7252_RESEARCH.md step 5).
FIELD_DID = 1
FIELD_SELF_IP = 2  # the camera's own view of its address; may be unreachable
FIELD_PORT = 4
FIELD_SDK = 5
FIELD_LSIGN = 8
FIELD_SKILL = 9  # gzipped skill.json

GZIP_MAGIC = b'\x1f\x8b'

logger = logging.getLogger(__name__)


class DiscoverUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_receive):
        super().__init__()
        self.on_receive = on_receive
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.on_receive(data, addr)


async def create_udp_server(port, on_receive):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoverUDPProtocol(on_receive),
        local_addr=('0.0.0.0', port),
        allow_broadcast=True,
    )
    return transport


def parse_skill(blob) -> dict:
    """Decode the gzipped skill.json the camera embeds in its reply."""
    if not isinstance(blob, (bytes, bytearray)) or blob[:2] != GZIP_MAGIC:
        return {}
    try:
        return json.loads(gzip.decompress(blob).decode('utf-8', 'replace'))
    except (OSError, ValueError):
        logger.debug('Discovery reply carried an undecodable skill blob')
        return {}


def parse_discovery_reply(data: bytes, addr, prefix=PREFIX) -> DeviceDescriptor | None:
    """Turn one datagram into a descriptor, or None if it is not for us."""
    try:
        packet = parse_packet(data, udp=None, exact=False)
    except PacketError:
        return None
    if not isinstance(packet, RPCPacket) or packet.command_id != Commands.DISCOVERY:
        return None
    plain = decrypt_rpc(packet.sequence, packet.command_id, packet.rpc_type,
                        packet.payload, prefix)
    if plain is None:
        # Wrong prefix for this firmware, or not really a discovery reply.
        logger.debug('Discovery reply from %s will not decrypt with this prefix', addr)
        return None
    fields = pb_fields(plain)
    dev_id = as_str(fields.get(FIELD_DID, ''))
    if not dev_id:
        return None
    port = fields.get(FIELD_PORT)
    return DeviceDescriptor(
        dev_id=dev_id,
        # Reach the camera where it answered from, not where it says it lives.
        addr=addr[0],
        port=port if isinstance(port, int) and 0 < port < 65536 else PORT,
        lsign=as_str(fields.get(FIELD_LSIGN, '')),
        sdk=as_str(fields.get(FIELD_SDK, '')),
        skill=parse_skill(fields.get(FIELD_SKILL)),
    )


class Discovery:
    """Broadcast Discovery(2600) and hand each reply to a callback."""

    def __init__(self, remote_addr=DEFAULT_DISCOVERY_ADDRESS, remote_port=PORT,
                 local_port=0, prefix=PREFIX):
        self.transport = None
        self.remote_addr = remote_addr
        self.remote_port = remote_port
        self.local_port = local_port
        self.prefix = prefix
        # Discovery requests are numbered so consecutive probes are distinct on
        # the wire; kept below 0x80 so the varint stays one byte, exactly like
        # the vendor app's datagram.
        self._seq = 0

    def get_discovery_packet(self) -> bytes:
        """The Qp-framed empty encrypted Discovery request.

        Byte-identical to what the vendor app broadcasts:
        ``51 70 48 04 <seq> a8 14 0c``. The body is empty even though enc=3 --
        there is no ciphertext at all, so do not "helpfully" pad it.
        """
        self._seq = self._seq % 0x7F + 1
        return build_rpc(self._seq, Commands.DISCOVERY, b'', rpc_type=RpcType.REQ, udp=True)

    def get_targets(self) -> list:
        """Where to send: the requested address plus the broadcasts that reach it.

        When pointed at one camera we still add its subnet broadcast and the
        global broadcast -- some units answer only the broadcast form.
        """
        targets = [self.remote_addr]
        if self.remote_addr != DEFAULT_DISCOVERY_ADDRESS and not self.remote_addr.endswith('.255'):
            subnet_broadcast = self.remote_addr.rsplit('.', 1)[0] + '.255'
            targets.append(subnet_broadcast)
            targets.append(DEFAULT_DISCOVERY_ADDRESS)
        return targets

    def on_receive(self, data, addr, callback):
        try:
            device = parse_discovery_reply(data, addr, self.prefix)
        except Exception:
            # A stray datagram on the discovery socket must never raise out of
            # the asyncio callback and wedge the transport.
            logger.debug('Dropping undecodable discovery datagram from %s', addr, exc_info=True)
            return
        if device is None:
            return
        logger.info('found device %s (sdk %s)', device, device.sdk or '?')
        if not device.lsign:
            # Without lsign LanAuth cannot run; report it rather than failing
            # opaquely three steps later.
            logger.warning('%s answered discovery without an lsign', device)
        callback(device)

    async def discover(self, callback, period=10):
        assert period >= 1, 'need to wait for camera response more than 1 second'
        logger.info('Start discovery on %s:%d', self.remote_addr, self.remote_port)

        self.transport = await create_udp_server(
            self.local_port, lambda data, addr: self.on_receive(data, addr, callback),
        )
        bound_port = self.transport.get_extra_info('sockname')[1]
        logger.info('Discovery listening on local UDP port %d', bound_port)
        targets = self.get_targets()
        try:
            while True:
                packet = self.get_discovery_packet()
                for target in targets:
                    logger.debug('discover> %s to %s:%d', packet.hex(' '), target, self.remote_port)
                    try:
                        self.transport.sendto(packet, (target, self.remote_port))
                    except OSError as err:
                        # e.g. no route for the global broadcast on a host with
                        # several interfaces; the other targets may still work.
                        logger.debug('Cannot send discovery to %s: %s', target, err)
                await asyncio.sleep(period)
        finally:
            logger.info('Stop discovery')
            if self.transport:
                self.transport.close()
                self.transport = None
