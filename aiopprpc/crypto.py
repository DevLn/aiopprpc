"""AES key derivation and payload encryption for PPRPC.

Two independent schemes, both proven on FT04 fw 3.12.13:

* **Control RPC** -- key = ``ascii_hex(MD5(PREFIX + ",ID:<cmd>-SEQ:<seq>-RPC:<rpc_type>"))``,
  IV = ``key[:16]``, AES-256-CBC with PKCS#7. The key is per-packet: it is
  derived from the very header fields the packet carries in the clear, so a
  received packet always tells us how to decrypt itself.
* **A/V media** -- key = ``ascii_hex(MD5(session_key + ",AVSeq:<seq>-TT:<ts>-AVChannel:<ch>"))``,
  IV = ``key[-16:]`` (note: the *tail*, not the head), AES-256-CBC with **no
  padding**, and only the first ``enc_length`` bytes of the payload are
  encrypted -- the remainder is plaintext.

The MD5 hex digest is 32 ASCII characters, which is exactly a 256-bit key; that
is why the digest is used as text rather than as raw bytes.
"""
import hashlib

from Crypto.Cipher import AES

from .const import PREFIX, Encryption

AES_BLOCK = 16


def derive_rpc_key(seq: int, cmd: int, rpc_type: int, prefix: str | bytes = PREFIX) -> bytes:
    """32-byte ASCII-hex AES key for one control RPC payload."""
    prefix_bytes = prefix.encode() if isinstance(prefix, str) else prefix
    suffix = f',ID:{cmd}-SEQ:{seq}-RPC:{rpc_type}'.encode()
    return hashlib.md5(prefix_bytes + suffix).hexdigest().encode()


def derive_av_key(session_key: str | bytes, seq: int, timestamp: int, channel: int) -> bytes:
    """32-byte ASCII-hex AES key for one A/V payload.

    Keyed on the LanAuth session key, not on PREFIX: the media path needs no
    vendor secret once the session is up.
    """
    key_bytes = session_key.encode() if isinstance(session_key, str) else session_key
    suffix = f',AVSeq:{seq}-TT:{timestamp}-AVChannel:{channel}'.encode()
    return hashlib.md5(key_bytes + suffix).hexdigest().encode()


def encrypt_rpc(seq: int, cmd: int, rpc_type: int, plain: bytes, prefix: str | bytes = PREFIX) -> bytes:
    """AES-256-CBC + PKCS#7 one RPC body.

    An empty body stays empty: the camera's own Discovery datagram carries
    ``enc=3`` with a zero-length payload, so padding nothing up to a full block
    would not match the wire.
    """
    if not plain:
        return b''
    key = derive_rpc_key(seq, cmd, rpc_type, prefix)
    pad = AES_BLOCK - len(plain) % AES_BLOCK
    padded = plain + bytes([pad]) * pad
    return AES.new(key, AES.MODE_CBC, key[:AES_BLOCK]).encrypt(padded)


def decrypt_rpc(seq: int, cmd: int, rpc_type: int, blob: bytes, prefix: str | bytes = PREFIX) -> bytes | None:
    """Return the plaintext, or None if the blob does not decrypt cleanly.

    Valid PKCS#7 after decryption is the tell that the prefix is right for this
    firmware -- a wrong key gives random trailing bytes and is rejected here
    rather than handed on as garbage protobuf.
    """
    if not blob or len(blob) % AES_BLOCK:
        return None
    key = derive_rpc_key(seq, cmd, rpc_type, prefix)
    plain = AES.new(key, AES.MODE_CBC, key[:AES_BLOCK]).decrypt(blob)
    pad = plain[-1]
    if not 1 <= pad <= AES_BLOCK or plain[-pad:] != bytes([pad]) * pad:
        return None
    return plain[:-pad]


def _passthrough_decrypt(seq, cmd, rpc_type, blob, prefix=PREFIX):
    return blob


def _passthrough_encrypt(seq, cmd, rpc_type, plain, prefix=PREFIX):
    return plain


#: Dispatch table keyed by the packet's own encryption byte: (decrypt, encrypt).
#: The session looks the method up rather than branching on enc type.
ENC_METHODS = {
    int(Encryption.NONE): (_passthrough_decrypt, _passthrough_encrypt),
    int(Encryption.AES256): (decrypt_rpc, encrypt_rpc),
}


def decrypt_av(session_key: str | bytes, seq: int, timestamp: int, channel: int,
               body: bytes, enc_length: int) -> bytes:
    """Decrypt the encrypted head of an A/V payload, pass the tail through.

    ``enc_length`` comes from the packet header (1040 for video, 960 for audio
    on this hardware). It must be a whole number of AES blocks and must fit in
    the body; anything else means we misparsed and the body is returned as-is.
    """
    if not enc_length or enc_length % AES_BLOCK or len(body) < enc_length:
        return body
    key = derive_av_key(session_key, seq, timestamp, channel)
    head = AES.new(key, AES.MODE_CBC, key[-AES_BLOCK:]).decrypt(body[:enc_length])
    return head + body[enc_length:]


def encrypt_av(session_key: str | bytes, seq: int, timestamp: int, channel: int,
               body: bytes, enc_length: int | None = None) -> tuple[bytes, int]:
    """Encrypt an outgoing A/V payload (talk-back). Returns (payload, enc_length).

    There is no padding in this scheme: only whole blocks from the front are
    encrypted and the remainder rides in the clear. ``enc_length`` defaults to
    as much as will fit, but talk-back passes it explicitly -- the vendor app
    leaves the last 16 bytes of every mic frame unencrypted and the camera
    expects exactly that.
    """
    if enc_length is None:
        enc_length = len(body) - len(body) % AES_BLOCK
    enc_length = max(0, min(enc_length, len(body)))
    enc_length -= enc_length % AES_BLOCK
    if not enc_length:
        return body, 0
    key = derive_av_key(session_key, seq, timestamp, channel)
    head = AES.new(key, AES.MODE_CBC, key[-AES_BLOCK:]).encrypt(body[:enc_length])
    return head + body[enc_length:], enc_length
