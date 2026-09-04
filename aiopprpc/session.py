"""The PPRPC session engine: one TCP connection carrying control and media.

Lifecycle
---------
1. UDP discovery has already produced a descriptor with a **fresh** ``lsign``.
2. TCP connect to ``host:20190``.
3. ``LanAuth(2650) {0, did, lsign}`` -> ``session_key`` (the A/V crypto key).
4. ``SyncConn(106) {1:1}``.
5. Answer every ``ConnHB(107)`` the camera sends, promptly.

The camera hangs up on a connection that is idle or slow to answer -- a cold
socket that says nothing is dropped after about three seconds -- so LanAuth goes
out immediately after connect and heartbeats are answered from the packet queue
rather than from a polling loop.

Unlike aiopppp's UDP sessions this is a byte stream, so the read loop keeps a
reassembly buffer and pulls whole packets out of it (``iter_tcp_packets``).
There is no magic number to resynchronise on, so a framing error is
unrecoverable: the session tears itself down and the caller reconnects (which
must re-discover, because lsign is single-use).
"""
import asyncio
import datetime
import logging
import time
from enum import Enum

from .const import (
    EVENT_KIND_ALARM,
    EVENT_KIND_DEVICE_ONLINE,
    EVENT_NAMES,
    FLASHLIGHT_TOGGLE_PAYLOAD,
    MEDIA_FORMAT_MJPEG,
    MEDIA_FORMAT_PCM,
    PREFIX,
    PTZ_DIRECTIONS,
    SCHEDULE_END_ALL_DAY,
    TALK_CHANNEL,
    TALK_ENC_TRAILER,
    TALK_FRAME_BYTES,
    UNREADABLE_DATAPOINTS,
    WEEKDAYS,
    WIFI_SET_FIELD5,
    Commands,
    Datapoint,
    Encryption,
    Flag,
    Ptz,
    RpcType,
    command_name,
    datapoint_name,
)
from .crypto import ENC_METHODS, decrypt_av
from .exceptions import AuthError, CommandError, NotConnectedError, PacketError
from .protocol import (
    AVPacket,
    RPCPacket,
    as_str,
    build_av,
    build_rpc,
    find,
    iter_tcp_packets,
    pb_bytes,
    pb_fields,
    pb_message,
    pb_repeated,
    pb_signed,
    pb_string,
    pb_varint,
    split_av_payload,
    to_signed,
    walk,
)
from .types import (
    AudioFrame,
    DeviceDescriptor,
    FlipMode,
    IRCutMode,
    MotionSensitivity,
    NightLightMode,
    VideoFrame,
    describe_time,
)

logger = logging.getLogger(__name__)

#: Field 2 of a heartbeat REPLY. The app sends -30 here on every single reply
#: (85/85 in audio.pcap, never anything else), as a negative number in an
#: unsigned varint field. It is not an acknowledgement -- field 4 of the
#: outbound REQUEST is; see AV_ACK_INTERVAL_SEC.
HEARTBEAT_RSSI = -30

#: How often to send our own ConnHB REQUEST carrying the A/V acknowledgement.
#: The app does this every 0.50-0.51 s without exception.
AV_ACK_INTERVAL_SEC = 0.5

#: How long to wait for the camera's answer to our ack. Short on purpose: the
#: ack loop is sequential, so a camera that goes quiet would otherwise drop our
#: acknowledgement rate to one per command timeout just when it matters.
AV_ACK_TIMEOUT_SEC = 1.0

#: Field 2 of an outbound heartbeat REQUEST. The app puts its measured
#: round-trip time in milliseconds here (189-442 ms over a cloud relay); we
#: measure our own, which on a LAN is single digits. Only a starting value.
AV_ACK_INITIAL_RTT_MS = 5

#: One byte of 16-bit PCM silence, for padding a short talk frame.
SILENCE_BYTE = bytes(1)

#: JPEG frame markers. Built from hex rather than typed as escapes -- a stray
#: literal byte in the source is a nasty thing to debug.
JPEG_SOI = bytes.fromhex('ffd8')
JPEG_EOI = bytes.fromhex('ffd9')


class SessionLogAdapter(logging.LoggerAdapter):
    """Tag every session log line with the device id -- several cameras log
    through this module concurrently and untagged lines are unattributable."""

    def process(self, msg, kwargs):
        return f'[{self.extra["dev"]}] {msg}', kwargs


class State(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2


class SharedFrameBuffer:
    """Latest-wins publish/subscribe. Slow consumers skip frames rather than
    building a backlog -- for live video the newest frame is the only one that
    matters."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.latest_frame = None

    async def publish(self, frame):
        async with self.condition:
            self.latest_frame = frame
            self.condition.notify_all()

    async def get(self):
        async with self.condition:
            await self.condition.wait()
            return self.latest_frame


class AudioQueue:
    """A short FIFO for audio. Deliberately *not* SharedFrameBuffer.

    Video wants latest-wins: a stale JPEG is worse than no JPEG, and a consumer
    that falls behind should skip to now. Audio is the opposite -- it is a
    continuous timeline, so a dropped 60 ms packet is an audible click and the
    frame to discard when a consumer stalls is the oldest, not the newest.

    The bound is about two seconds. Past that a listener is so far behind that
    catching up matters more than completeness, and an unbounded queue would
    just grow for as long as nobody reads it. Drops are counted rather than
    hidden: silent audio loss is the kind of thing that gets blamed on the
    camera for weeks.
    """

    MAXLEN = 32

    def __init__(self):
        self.queue = asyncio.Queue(self.MAXLEN)
        self.dropped = 0

    async def publish(self, frame):
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # a consumer got there first
                    pass

    async def get(self):
        return await self.queue.get()

    def clear(self):
        """Drop whatever is queued. Called when audio stops, so the next
        listener does not open on seconds of stale sound."""
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return


class PacketQueueMixin:
    """Control packets are handled off the read loop so that decrypting or
    answering one can never stall the socket."""

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.packet_queue = asyncio.Queue()
        self.process_packet_task = None

    async def process_packet_queue(self):
        while True:
            packet = await self.packet_queue.get()
            await self.handle_incoming_packet(packet)

    def start_packet_queue(self):
        self.process_packet_task = asyncio.create_task(self.process_packet_queue())

    async def handle_incoming_packet(self, packet):
        raise NotImplementedError


class MediaQueueMixin:
    """A/V packets take their own queue and their own buffers.

    FT04 sends one whole JPEG per packet, so there is no reassembly here at all
    -- decode and publish. That is the whole difference from aiopppp's chunked
    video path.
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.media_queue = asyncio.Queue()
        self.frame_buffer = SharedFrameBuffer()
        self.audio_buffer = AudioQueue()
        self.process_media_task = None
        self.video_frames = 0
        self.audio_frames = 0

    async def process_media_queue(self):
        while True:
            packet = await self.media_queue.get()
            await self.handle_incoming_media_packet(packet)

    def start_media_queue(self):
        self.process_media_task = asyncio.create_task(self.process_media_queue())

    async def handle_incoming_media_packet(self, packet):
        raise NotImplementedError


class Session(PacketQueueMixin, MediaQueueMixin):
    #: No packet at all from the camera for this long means the link is dead.
    #: The camera heartbeats continuously, so this is generous.
    RECV_TIMEOUT_SEC = 15
    #: While streaming, a gap this long means the stream stalled: ask again.
    VIDEO_REREQUEST_SEC = 3
    #: StreamReader buffer size. Deliberately far above the 64 KiB default:
    #: asyncio closes the TCP receive window once its buffer passes twice the
    #: limit, and a camera whose media sender gives up when it blocks must
    #: never be given a reason to block.
    RECV_BUFFER_LIMIT = 4 << 20
    #: Seconds between attempts to revive a stalled stream, and how many to
    #: make before reconnecting. Reconnecting is the LAST resort: a stalled
    #: stream on a demonstrably healthy link does not justify destroying a
    #: session that then takes a discovery cycle to rebuild.
    MEDIA_RECOVERY_SEC = 4
    MEDIA_RECOVERY_TRIES = 2
    #: Default wait for a command response.
    COMMAND_TIMEOUT = 5.0
    #: Wait for LanAuth specifically -- a stale lsign is answered fast, but a
    #: cold camera can take a moment.
    AUTH_TIMEOUT = 8.0
    #: A TCP reassembly buffer larger than this is not a big frame, it is a
    #: desynchronised stream: one bad length varint makes the parser wait
    #: forever for a packet that does not exist while bytes keep arriving --
    #: which looks exactly like the media freezing for no reason. Real packets
    #: on this hardware are a 640x480 JPEG at most, well under 100 kB.
    MAX_REASSEMBLY_BYTES = 1 << 20
    #: How often to log media throughput while streaming.
    MEDIA_STATS_SEC = 10.0
    #: Answering a heartbeat later than this is worth a warning: the camera
    #: sends them every 0.25-0.5s and hangs up on a link that goes quiet.
    HEARTBEAT_SLOW_SEC = 1.0

    def __init__(self, dev: DeviceDescriptor, on_disconnect=None, *args,
                 on_video_state_change=None, on_event=None, prefix=PREFIX, **kwargs):
        super().__init__(*args, **kwargs)
        self.dev = dev
        self.prefix = prefix
        self.log = SessionLogAdapter(logger, {'dev': dev.dev_id})
        self.state = State.DISCONNECTED
        self.session_key = None
        self.dev_properties = {}

        self.reader = None
        self.writer = None
        self.main_task = None
        self.read_task = None
        self.device_is_ready = asyncio.Event()

        self.on_disconnect = on_disconnect
        #: Called with the new is_video_requested value whenever streaming
        #: starts or stops (including when the session is torn down).
        self.on_video_state_change = on_video_state_change
        #: Called as ``on_event(name, payload_dict)`` for pushed camera events.
        self.on_event = on_event

        self.is_video_requested = False
        self.is_audio_requested = False
        self.is_talking = False
        self.video_stale_at = None
        self.audio_params = {'sample_rate': 8000, 'bits': 16, 'channels': 1}
        self.video_params = {}

        now = datetime.datetime.now()
        self.last_recv_at = now
        self.last_video_at = now
        self.last_audio_at = None
        #: media_format -> packets received, so a stall can be attributed to
        #: one stream rather than guessed at.
        self.av_counts = {}
        self.av_undecodable = 0
        #: The camera's own A/V sequence number, last seen: video and audio
        #: share ONE counter on channel 0 (proven in intercom.pcap: 26 video
        #: and 52 audio packets, zero overlap). This is what we acknowledge.
        self.last_av_seq = None
        #: Sequence number of the last A/V packet we have acknowledged, and the
        #: round-trip time we report alongside it.
        self.last_av_ack = None
        self._ack_rtt_ms = AV_ACK_INITIAL_RTT_MS
        self.ack_task = None
        self.heartbeats_answered = 0
        self.heartbeat_slowest = 0.0
        self._stats_logged_at = now
        self._stats_mark = (0, 0)

        self._seq = 0
        self._talk_seq = 0
        self._talk_buffer = b''
        self._talk_started_audio = False
        self._recovery_rung = 0
        self._recovery_at = None
        self._talk_frame_ms = 40.0
        #: seq -> Future, for the usual one-request/one-response commands.
        self._waiters = {}
        #: seq -> list, for commands that answer more than once (WifiScan).
        self._collectors = {}

        #: Dispatch tables, filled here so they can hold bound methods.
        self._request_handlers = {
            int(Commands.CONN_HB): self._handle_heartbeat,
            int(Commands.NOTIFY): self._handle_notify,
        }
        self._media_handlers = {
            MEDIA_FORMAT_MJPEG: self._handle_video_packet,
            MEDIA_FORMAT_PCM: self._handle_audio_packet,
        }

    def __str__(self):
        return f'Session({self.dev}) ({self.state.name})'

    # --- capability probing ---------------------------------------------
    @property
    def supported_commands(self):
        return self.dev.supported_commands

    def supports(self, cmd) -> bool:
        """Whether the camera declares this command in its skill.json.

        An empty declaration means "unknown", not "unsupported" -- discovery
        may have carried no skill blob -- so we answer True in that case and
        let the camera reject the command itself.
        """
        declared = self.supported_commands
        return not declared or int(cmd) in declared

    # --- transport ------------------------------------------------------
    async def _connect(self):
        self.state = State.CONNECTING
        self.log.info('Connecting to %s:%d', self.dev.addr, self.dev.port)
        self.reader, self.writer = await asyncio.open_connection(
            self.dev.addr, self.dev.port, limit=self.RECV_BUFFER_LIMIT)
        self.last_recv_at = datetime.datetime.now()

    async def _write(self, data: bytes):
        if not self.writer:
            raise NotConnectedError('Not connected to the camera')
        self.writer.write(data)
        await self.writer.drain()

    async def _read_loop(self):
        """Feed the two queues from the TCP stream."""
        buffer = b''
        while True:
            try:
                chunk = await self.reader.read(65536)
            except OSError as err:
                # A reset is a lost device, not a crashed task. Unguarded, this
                # escaped read_task; the supervisor in __main__ then saw a task
                # finish without new_device_fut resolving and shut the whole
                # server down. A camera reassociating to another AP is the most
                # likely producer of an RST, which is exactly when the page
                # needs to stay up to show what happened.
                self.log.info('Connection lost (%s)', err)
                self._on_device_lost()
                return
            if not chunk:
                self.log.info('Camera closed the connection')
                self._on_device_lost()
                return
            self.last_recv_at = datetime.datetime.now()
            buffer += chunk
            try:
                packets, buffer = iter_tcp_packets(buffer)
            except PacketError as err:
                # A TCP stream has no magic number to resynchronise on, so a
                # framing error means the stream is unusable from here. Show the
                # bytes: "unsupported flag 12" is only useful next to what was
                # actually on the wire.
                self.log.error('Unrecoverable framing error (%s) at %s...; reconnecting',
                               err, buffer[:32].hex(' '))
                self._on_device_lost()
                return
            if len(buffer) > self.MAX_REASSEMBLY_BYTES:
                # The parser is waiting for a packet that will never complete:
                # a mis-read length varint stalls parsing while the socket keeps
                # delivering, so control AND media go silent with the link still
                # up. Say so rather than sitting here until a stall timer fires.
                self.log.error(
                    'Reassembly buffer reached %d bytes: the stream is desynchronised '
                    '(first bytes %s); reconnecting',
                    len(buffer), buffer[:32].hex(' '))
                self._on_device_lost()
                return
            for packet in packets:
                if isinstance(packet, AVPacket):
                    self.media_queue.put_nowait(packet)
                else:
                    self.packet_queue.put_nowait(packet)
            if buffer:
                self.log.debug('read %dB -> %d packets, %dB held for reassembly',
                               len(chunk), len(packets), len(buffer))

    # --- sending --------------------------------------------------------
    def _next_seq(self) -> int:
        # Sequence numbers are parsed as 4-byte varints, so keep them well
        # inside that range; they only need to be unique among in-flight calls.
        self._seq = (self._seq + 1) % 0xFFFFFF
        return self._seq

    async def _send_rpc(self, seq: int, cmd: int, body: bytes = b'', *,
                        rpc_type=RpcType.REQ, code=None, enc=Encryption.AES256):
        packet = build_rpc(seq, int(cmd), body, enc=enc, rpc_type=rpc_type,
                           code=code, prefix=self.prefix)
        self.log.debug('send> seq=%s cmd=%s %s body=%dB', seq, command_name(cmd),
                       'RESP' if rpc_type == RpcType.RESP else 'REQ', len(body))
        await self._write(packet)

    async def send_command(self, cmd, body: bytes = b'', *, with_response=True,
                           timeout=None, check_code=True):
        """Send one command; return its response fields as ``{field: value}``.

        Responses are matched on the sequence number the request carried, so
        several commands can be in flight at once. ``with_response=False`` is
        for calls the camera may not survive answering (reboot).
        """
        seq = self._next_seq()
        future = None
        if with_response:
            future = asyncio.get_running_loop().create_future()
            self._waiters[seq] = future
        try:
            await self._send_rpc(seq, cmd, body)
        except Exception:
            self._waiters.pop(seq, None)
            raise
        if not with_response:
            return None
        # Past this point the packet is on the wire, so every failure below is a
        # "sent, then something went wrong" -- see PPRPCError.sent.
        try:
            code, plain = await asyncio.wait_for(future, timeout or self.COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            err = CommandError(command_name(cmd), None,
                              f'no response to {command_name(cmd)} within '
                              f'{timeout or self.COMMAND_TIMEOUT}s')
            err.sent = True
            raise err from None
        except asyncio.CancelledError:
            # stop() cancels every pending waiter. Tell the caller the session
            # went away instead of masquerading as their own cancellation.
            if future.cancelled() and self.state == State.DISCONNECTED:
                err = NotConnectedError(
                    f'session closed while waiting for {command_name(cmd)}')
                err.sent = True
                raise err from None
            raise
        finally:
            self._waiters.pop(seq, None)
        if check_code and code:
            err = CommandError(command_name(cmd), code)
            err.sent = True  # the camera answered -- it just said no
            raise err
        return pb_fields(plain or b'')

    async def collect_responses(self, cmd, body: bytes = b'', *, window=5.0):
        """Send one command and gather every response to it within ``window``.

        Returns the raw plaintext of each response, not parsed fields: a
        response whose body repeats a field (WifiScan packs one submessage per
        access point) loses everything after the first if it goes through
        ``pb_fields``, which is first-wins by design.
        """
        seq = self._next_seq()
        collector = self._collectors[seq] = []
        try:
            await self._send_rpc(seq, cmd, body)
            await asyncio.sleep(window)
        finally:
            # Hold the list itself, not the key: stop() clears _collectors
            # wholesale, so indexing self._collectors[seq] after the sleep
            # raised KeyError whenever the session died inside the window --
            # a 500 on a scan whose answer had already arrived.
            self._collectors.pop(seq, None)
        good = [plain or b'' for code, plain in collector if not code]
        for code, _plain in collector:
            if code:
                # Otherwise a refusal is indistinguishable from "nothing found".
                self.log.warning('%s response carried code %s', command_name(cmd), code)
        if not good and self.state == State.DISCONNECTED:
            # Returning [] here would report a session that died mid-window as
            # "the camera answered, and found nothing" -- the empty list is the
            # same value a genuinely empty result has. Partial results are kept
            # (the local list survives stop()'s clear); only a total blank while
            # disconnected is escalated.
            raise NotConnectedError(
                f'session closed while collecting {command_name(cmd)} responses')
        return good

    # --- receiving ------------------------------------------------------
    def _decrypt(self, packet: RPCPacket) -> bytes:
        if not packet.payload:
            # An empty body is meaningful and common: most SETs answer with {}
            # and carry no ciphertext at all. Not a decryption failure.
            return b''
        decrypt = ENC_METHODS.get(packet.encryption_type, ENC_METHODS[int(Encryption.NONE)])[0]
        plain = decrypt(packet.sequence, packet.command_id, packet.rpc_type,
                        packet.payload, self.prefix)
        if plain is None:
            # Only happens with the wrong PREFIX for this firmware; the packet
            # is unusable but the session is otherwise fine.
            self.log.warning('Cannot decrypt %s response (wrong PREFIX for this firmware?)',
                             command_name(packet.command_id))
            return b''
        return plain

    async def handle_incoming_packet(self, packet):
        if not isinstance(packet, RPCPacket):
            self.log.debug('recv< non-RPC type=%s len=%s',
                           packet.header.message_type, packet.header.length)
            return
        plain = self._decrypt(packet)
        self.log.debug('recv< seq=%s cmd=%s %s code=%s body=%dB', packet.sequence,
                       command_name(packet.command_id),
                       'RESP' if packet.rpc_type == RpcType.RESP else 'REQ',
                       packet.response_code, len(plain))
        if packet.rpc_type == RpcType.RESP:
            self._handle_response(packet, plain)
            return
        handler = self._request_handlers.get(packet.command_id)
        if handler is None:
            self.log.info('Unsolicited %s from camera, ignored', command_name(packet.command_id))
            return
        await handler(packet, plain)

    def _handle_response(self, packet, plain):
        collector = self._collectors.get(packet.sequence)
        if collector is not None:
            collector.append((packet.response_code, plain))
            return
        future = self._waiters.get(packet.sequence)
        if future is not None and not future.done():
            future.set_result((packet.response_code, plain))
            return
        # A response whose caller already timed out, or one of the camera's
        # own late duplicates. Note it, but do not treat it as an error.
        self.log.debug('Late/unmatched response to %s seq=%s',
                       command_name(packet.command_id), packet.sequence)

    async def _send_av_ack(self):
        """One outbound ConnHB carrying the last A/V sequence we received.

        Body mirrors the app's: ``{1: now_ms, 2: rtt_ms, 4: last_av_seq}``.
        Field 4 is omitted until media has actually arrived, exactly as the app
        omits it on the heartbeats it sends before the stream starts.
        """
        now_ms = int(time.time() * 1000)
        body = pb_varint(1, now_ms) + pb_varint(2, max(0, int(self._ack_rtt_ms)))
        if self.last_av_seq is not None:
            body += pb_varint(4, self.last_av_seq)
        started = time.monotonic()
        await self.send_command(Commands.CONN_HB, body, timeout=AV_ACK_TIMEOUT_SEC)
        # The camera answers {1: echo, 2: 20, 3: ts}; the round trip is what we
        # report in field 2 next time, the way the app reports its own.
        self._ack_rtt_ms = (time.monotonic() - started) * 1000
        self.last_av_ack = self.last_av_seq

    async def _av_ack_loop(self):
        """Acknowledge received A/V continuously, as the app does.

        Without this the camera stops sending media after ~152 unacknowledged
        packets -- measured at sequence 150/151 in 14 stalls out of 14 -- while
        answering heartbeats and RPCs perfectly, which makes it look like the
        media path died rather than like flow control doing its job.
        """
        while self.state == State.CONNECTED:
            await asyncio.sleep(AV_ACK_INTERVAL_SEC)
            if self.state != State.CONNECTED:
                return
            try:
                await self._send_av_ack()
            except (CommandError, NotConnectedError) as err:
                # A missed ack is not fatal on its own; the next one carries a
                # higher sequence number and closes the gap.
                self.log.debug('A/V ack failed: %s', err)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception('A/V ack loop failed')
                return

    async def _handle_heartbeat(self, packet, plain):
        """Answer ConnHB. Being slow here is how the link dies."""
        started = time.monotonic()
        now_ms = int(time.time() * 1000)
        body = pb_varint(1, now_ms) + pb_signed(2, HEARTBEAT_RSSI) + pb_varint(3, now_ms)
        await self._send_rpc(packet.sequence, Commands.CONN_HB, body,
                             rpc_type=RpcType.RESP, code=0)
        # The camera heartbeats 2-4 times a second and hangs up on a quiet link,
        # so how long we took to answer is the first thing worth knowing when a
        # session dies for no obvious reason.
        elapsed = time.monotonic() - started
        self.heartbeats_answered += 1
        self.heartbeat_slowest = max(self.heartbeat_slowest, elapsed)
        if elapsed > self.HEARTBEAT_SLOW_SEC:
            self.log.warning('Took %.2fs to answer a heartbeat (queue %d deep)',
                             elapsed, self.packet_queue.qsize())

    async def _handle_notify(self, packet, plain):
        """Handle a pushed event (cmd 13) and acknowledge it.

        Shape (captured on the cloud notify channel)::

            {1: msg_id, 2: topic, 6: ts_ms, 7: {2: did, 4: <event>, 6: ts_ms}}

        where ``7.4.1`` discriminates: 66 = alarm, with ``7.4.2[0]`` the event
        code (12 = motion) and ``7.4.4`` the camera name; 67 = device online.

        NOTE: this has only ever been observed on the *cloud* notify channel.
        Whether the camera pushes events on a LAN-direct session is unverified,
        so do not treat "no events" as a bug.
        """
        tree = walk(plain) or []
        msg_id = find(tree, [1])
        kind = find(tree, [7, 4, 1])
        payload = {'command': packet.command_id, 'kind': kind,
                   'timestamp': find(tree, [7, 6]) or find(tree, [6])}

        if kind == EVENT_KIND_ALARM:
            blob = find(tree, [7, 4, 2])
            code = blob[0] if isinstance(blob, (bytes, bytearray)) and blob else None
            name = EVENT_NAMES.get(code, f'event{code}')
            payload.update({'code': code, 'source': as_str(find(tree, [7, 4, 4]) or b'')})
            self.log.info('EVENT %s (code %s) from %r', name, code, payload['source'])
            self._fire_event(name, payload)
        elif kind == EVENT_KIND_DEVICE_ONLINE:
            payload['did'] = as_str(find(tree, [7, 4, 4]) or b'')
            self.log.info('EVENT device_online %s', payload['did'])
            self._fire_event('device_online', payload)
        else:
            self.log.info('Unrecognised pushed event (7.4.1=%s), ignored', kind)

        if msg_id is not None:
            # The app answers a notify with just the message id it carried.
            body = pb_bytes(2, msg_id) if isinstance(msg_id, (bytes, bytearray)) \
                else pb_varint(2, msg_id)
            await self._send_rpc(packet.sequence, Commands.NOTIFY, body,
                                 rpc_type=RpcType.RESP, code=0)

    def _fire_event(self, name, payload):
        if not self.on_event:
            return
        try:
            self.on_event(name, payload)
        except Exception:
            self.log.exception('on_event callback failed for %s', name)

    # --- media ----------------------------------------------------------
    async def handle_incoming_media_packet(self, packet: AVPacket):
        self.av_counts[packet.media_format] = self.av_counts.get(packet.media_format, 0) + 1
        self.last_av_seq = packet.sequence
        handler = self._media_handlers.get(packet.media_format)
        if handler is None:
            self.log.debug('A/V packet in unknown media format %s, dropped', packet.media_format)
            return
        await handler(packet)

    def _decode_av(self, packet: AVPacket):
        """Strip the frame prefix and decrypt the encrypted head."""
        body = split_av_payload(packet.payload)
        if body is None:
            return None
        if not self.session_key:
            return None
        return decrypt_av(self.session_key, packet.sequence, packet.timestamp,
                          packet.channel, body, packet.encrypted_length)

    async def _handle_video_packet(self, packet: AVPacket):
        body = self._decode_av(packet)
        if not body:
            return
        # One whole JPEG per packet on this hardware; trim to SOI..EOI because
        # the tail after EOI is padding, and a browser will choke on it.
        start = body.find(JPEG_SOI)
        end = body.rfind(JPEG_EOI)
        if start == -1 or end <= start:
            self.av_undecodable += 1
            self.log.debug('A/V video packet carried no complete JPEG, dropped')
            return
        self.last_video_at = datetime.datetime.now()
        self.video_stale_at = None
        self._recovery_rung = 0
        self._recovery_at = None
        self.video_frames += 1
        await self.frame_buffer.publish(VideoFrame(idx=packet.sequence, data=body[start:end + 2]))

    async def _handle_audio_packet(self, packet: AVPacket):
        body = self._decode_av(packet)
        if not body:
            self.av_undecodable += 1
            return
        self.last_audio_at = datetime.datetime.now()
        self.audio_stale_at = None
        self.audio_frames += 1
        await self.audio_buffer.publish(
            AudioFrame(idx=packet.sequence, data=body,
                       sample_rate=self.audio_params.get('sample_rate', 8000)))

    async def get_video_frame(self):
        return await self.frame_buffer.get()

    async def get_audio_frame(self):
        return await self.audio_buffer.get()

    # --- lifecycle ------------------------------------------------------
    async def _lan_auth(self):
        """Exchange the discovery-fresh lsign for the A/V session key."""
        if not self.dev.lsign:
            raise AuthError('No lsign: re-run discovery immediately before connecting')
        body = pb_varint(1, 0) + pb_string(2, self.dev.dev_id) + pb_string(3, self.dev.lsign)
        try:
            fields = await self.send_command(Commands.LAN_AUTH, body, timeout=self.AUTH_TIMEOUT)
        except CommandError as err:
            raise AuthError(f'LanAuth rejected ({err}); lsign may have expired') from err
        session_key = as_str(fields.get(1, ''))
        if not session_key:
            raise AuthError('LanAuth returned no session key')
        self.session_key = session_key
        self.log.info('Authenticated, session_key=%s', session_key)

    async def _app_handshake(self):
        """The opening exchange every capture shows the app performing.

        `2653` (name unrecovered, answers `{1:4, 2:<big>, 3:<big>}`) goes first,
        then three status datapoints. Nothing here is required to get video; we
        send it because the app does, and it is best effort throughout.

        These go out as the app's exact message -- did present AND a `00` value
        byte (docs/decoded/light.txt:39,43,51) -- rather than as a value-less
        read. What these three datapoints do is unknown, and dp 201 proved that
        a value-less request is not automatically a harmless read: on that
        datapoint it actuates. The app's bytes are the only shape observed to be
        safe on this hardware, so send those and nothing else. The answers are
        discarded either way.
        """
        await self._attempt('capability probe', self.send_command(Commands.CAPABILITIES))
        for datapoint in (Datapoint.STATUS_POLL, Datapoint.STATUS_POLL_23131,
                          Datapoint.STATUS_POLL_23113):
            await self._attempt(f'status poll {int(datapoint)}',
                                self.datapoint_set(datapoint, b'\x00', with_did=True))

    async def _attempt(self, what, coro):
        """Run a probe whose failure must not abort the session."""
        try:
            return await coro
        except (CommandError, NotConnectedError) as err:
            self.log.debug('%s failed: %s', what, err)
            return None

    async def _sync_conn(self):
        await self.send_command(Commands.SYNC_CONN, pb_varint(1, 1))

    async def setup_device(self):
        """Read the properties the integration exposes. Best effort: a camera
        that does not answer one of these must still finish connecting."""
        self.dev_properties.update({
            'did': self.dev.dev_id,
            'ip': self.dev.addr,
            'sdk': self.dev.sdk,
        })

        async def attempt(name, coro):
            try:
                return await coro
            except (CommandError, PacketError, ValueError) as err:
                self.log.debug('setup: %s unavailable (%s)', name, err)
                return None

        net = await attempt('network info', self.get_network_info())
        if net:
            self.dev_properties.update(net)
        status = await attempt('wifi status', self.get_wifi_status())
        if status:
            self.dev_properties['rssi'] = status.get('rssi')
            # WifiStatus carries the current SSID as well, and this is the only
            # other source of it. NetInfo wins when it answered (it is richer,
            # and it is what filled 'ssid' just above), but it times out on a
            # real fraction of connects -- 11 of 39 in the recorded hardware
            # run -- while WifiStatus answers 6-18 ms later with the SSID right
            # there. Dropping it left the session with no idea which network the
            # camera was on for its entire life.
            if status.get('ssid') and not self.dev_properties.get('ssid'):
                self.dev_properties['ssid'] = status['ssid']
        flip = await attempt('flip', self.get_flip())
        if flip is not None:
            self.dev_properties['flip'] = int(flip)
        ircut = await attempt('ircut', self.get_ircut())
        if ircut is not None:
            self.dev_properties['ircut'] = int(ircut)
        sensitivity = await attempt('motion sensitivity', self.get_motion_sensitivity())
        if sensitivity is not None:
            self.dev_properties['motion_sensitivity'] = int(sensitivity)
        night = await attempt('night light', self.get_night_light())
        if night is not None:
            self.dev_properties['night_light'] = int(night)
        clock = await attempt('time', self.get_time())
        if clock:
            # Namespaced, and only the readable parts: dev_properties sits
            # beside flip/ircut/rssi and a bare 'local' key there means nothing.
            self.dev_properties['device_time'] = clock.get('local')
            self.dev_properties['device_time_drift_s'] = clock.get('drift_s')
            self.dev_properties['tz_offset_min'] = clock.get('tz_offset_min')
        self.log.debug('device properties: %s', self.dev_properties)

    async def _run(self):
        try:
            await self._connect()
            self.read_task = asyncio.create_task(self._read_loop())
            # The camera drops a connection that stays quiet for ~3s, so
            # authenticate straight away rather than after any preamble.
            await self._lan_auth()
            await self._sync_conn()
            self.state = State.CONNECTED
            # Start acknowledging immediately: the window fills in about six
            # seconds once media flows.
            self.ack_task = asyncio.create_task(self._av_ack_loop())
            await self._app_handshake()
            await self.setup_device()
            self.device_is_ready.set()
            self.log.info('Session ready')
            # Loop on the state, not forever: loop_step() can tear the session
            # down, and stop() cannot cancel this task from inside itself.
            while self.state == State.CONNECTED:
                await self.loop_step()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # One camera must never take the process down.
            self.log.warning('Session failed: %s', err, exc_info=not isinstance(
                err, (AuthError, CommandError, OSError)))
            self._on_device_lost()

    def media_stats(self) -> dict:
        """A snapshot of what is actually arriving, per stream."""
        now = datetime.datetime.now()
        return {
            'video_frames': self.video_frames,
            'audio_frames': self.audio_frames,
            'last_av_seq': self.last_av_seq,
            'last_av_ack': self.last_av_ack,
            # How far the camera has run ahead of our acknowledgement. It stops
            # sending once this reaches ~152, so it must stay small.
            'unacked': (None if self.last_av_seq is None
                        else self.last_av_seq - (self.last_av_ack or 0)),
            'av_by_format': dict(self.av_counts),
            'undecodable': self.av_undecodable,
            'audio_dropped': self.audio_buffer.dropped,
            'video_idle_s': round((now - self.last_video_at).total_seconds(), 1),
            'audio_idle_s': (None if self.last_audio_at is None
                             else round((now - self.last_audio_at).total_seconds(), 1)),
            'recv_idle_s': round((now - self.last_recv_at).total_seconds(), 1),
            'media_queue': self.media_queue.qsize(),
            'packet_queue': self.packet_queue.qsize(),
            'heartbeats': self.heartbeats_answered,
            'heartbeat_slowest_s': round(self.heartbeat_slowest, 3),
        }

    def _log_media_stats(self, now):
        """Log throughput periodically while streaming.

        Rates, not just totals: "video 7.1/s audio 16.6/s" and "video 0.0/s
        audio 16.6/s" carry almost the same totals a moment apart and mean
        completely different things.
        """
        elapsed = (now - self._stats_logged_at).total_seconds()
        if elapsed < self.MEDIA_STATS_SEC:
            return
        video, audio = self.video_frames, self.audio_frames
        previous_video, previous_audio = self._stats_mark
        self._stats_mark = (video, audio)
        self._stats_logged_at = now
        self.log.info(
            'media: video %.1f/s audio %.1f/s (totals %d/%d, cam seq %s, acked %s) | '
            'undecodable %d | '
            'audio dropped %d | queues media=%d packet=%d | heartbeats %d, slowest %.2fs',
            (video - previous_video) / elapsed, (audio - previous_audio) / elapsed,
            video, audio, self.last_av_seq, self.last_av_ack,
            self.av_undecodable, self.audio_buffer.dropped,
            self.media_queue.qsize(), self.packet_queue.qsize(),
            self.heartbeats_answered, self.heartbeat_slowest)

    async def _recover_media(self) -> bool:
        """Try to revive a stalled stream. False once the session was dropped.

        Re-requesting is cheap and usually enough; reconnecting costs a
        discovery cycle, so it is the last resort rather than the first.
        """
        self._recovery_rung += 1
        if self._recovery_rung <= self.MEDIA_RECOVERY_TRIES:
            self.log.info('Media stalled (attempt %d): re-requesting video',
                          self._recovery_rung)
            await self._attempt('video re-request', self._request_video())
            return True
        self.log.warning('Media did not recover after %d attempts, reconnecting. %s',
                         self.MEDIA_RECOVERY_TRIES, self.media_stats())
        self._on_device_lost()
        return False

    async def loop_step(self):
        now = datetime.datetime.now()

        if self.is_video_requested or self.is_audio_requested:
            self._log_media_stats(now)

        if self.is_video_requested:
            idle = (now - self.last_video_at).total_seconds()
            if idle > self.VIDEO_REREQUEST_SEC:
                if not self.video_stale_at:
                    # Say what else is still arriving. Video stopping while
                    # audio keeps flowing is a camera-side stream problem; both
                    # stopping while the socket stays busy would be ours.
                    self.video_stale_at = now
                    self._recovery_rung = 0
                    self._recovery_at = None
                    self.log.info('No video for %.1fs. %s', idle, self.media_stats())
                due = (self._recovery_at is None
                       or (now - self._recovery_at).total_seconds() >= self.MEDIA_RECOVERY_SEC)
                if due:
                    self._recovery_at = now
                    if not await self._recover_media():
                        return

        if (now - self.last_recv_at).total_seconds() > self.RECV_TIMEOUT_SEC:
            self.log.warning('Nothing received for %ds, connection is dead. %s',
                             self.RECV_TIMEOUT_SEC, self.media_stats())
            self._on_device_lost()

    def start(self):
        self.device_is_ready.clear()
        self.start_packet_queue()
        self.start_media_queue()
        self.main_task = asyncio.create_task(self._run())
        return self.main_task

    def running_tasks(self):
        return tuple(t for t in (self.main_task, self.read_task, self.process_packet_task,
                                 self.process_media_task, self.ack_task) if t)

    def _on_device_lost(self):
        if self.state == State.DISCONNECTED and not self.writer:
            return
        self.log.warning('Device lost')
        self.stop()
        if self.on_disconnect:
            self.on_disconnect(self.dev)

    def stop(self):
        """Tear the session down. Idempotent: reachable from the read loop, the
        run loop and the owner."""
        if self.state == State.DISCONNECTED and not self.writer:
            return
        self.log.info('Stopping session')
        self.state = State.DISCONNECTED
        self.device_is_ready.set()  # unblock anyone waiting for a session that will not come

        for task in (self.read_task, self.process_packet_task, self.process_media_task,
                     self.ack_task, self.main_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()
        self._collectors.clear()

        if self.writer:
            try:
                self.writer.close()
            except OSError:
                pass
            self.writer = None
        self.reader = None
        if self.is_video_requested:
            self.is_video_requested = False
            self._notify_video_state()
        self.is_audio_requested = False
        self.is_talking = False
        self.ack_task = None

    def _notify_video_state(self):
        if self.on_video_state_change:
            self.on_video_state_change(self.is_video_requested)

    # --- video / audio commands -----------------------------------------
    async def _request_video(self):
        fields = await self.send_command(Commands.VIDEO_PLAY)
        # RESP {5: qos, 6: fps, 7: format} -- format 4 is MJPEG.
        self.video_params = {'qos': fields.get(5), 'fps': fields.get(6), 'format': fields.get(7)}
        self.dev_properties.update({'fps': fields.get(6), 'video_format': fields.get(7)})
        return self.video_params

    async def start_video(self):
        if self.is_video_requested:
            return
        self.log.info('Start video')
        self.last_video_at = datetime.datetime.now()
        self.video_stale_at = None
        # The app sends this marker alongside VideoPlay every time it opens a
        # live view. Empty body both ways, and not required to get video.
        await self._attempt('live view start',
                            self.send_command(Commands.LIVE_VIEW_START))
        await self._request_video()
        self.is_video_requested = True
        self._notify_video_state()

    async def stop_video(self):
        if not self.is_video_requested:
            return
        self.is_video_requested = False
        self.video_stale_at = None
        self._notify_video_state()
        await self.send_command(Commands.VIDEO_STOP)
        await self._attempt('live view stop',
                            self.send_command(Commands.LIVE_VIEW_STOP))

    async def start_audio(self):
        if self.is_audio_requested:
            return
        # RESP {5: 121(PCM), 6: rate, 7: bits, 8: channels}
        fields = await self.send_command(Commands.AUDIO_PLAY)
        self.audio_params = {
            'sample_rate': fields.get(6, 8000),
            'bits': fields.get(7, 16),
            'channels': fields.get(8, 1),
        }
        self.is_audio_requested = True
        self.last_audio_at = datetime.datetime.now()
        self.log.info('Start audio %s', self.audio_params)

    async def stop_audio(self):
        if not self.is_audio_requested:
            return
        self.is_audio_requested = False
        self.audio_buffer.clear()
        await self.send_command(Commands.AUDIO_STOP)

    async def start_talk(self):
        """Open the talk-back uplink, exactly as the vendor app does.

        The app's intercom capture shows the sequence is **two** commands, not
        one::

            2616 SpeakStart {}  -> {1:2, 5:121, 6:8000, 7:16, 8:1}
            2614 AudioPlay  {}  -> {5:121, 6:8000, 7:16, 8:1}

        Intercom is full duplex: the downlink audio session has to be running
        too. Sending SpeakStart alone and then pushing frames is what reboots
        the camera -- the audio engine was never started.

        (``SpeakStart``'s field 1 = 2 is *not* the A/V channel. The app uploads
        on channel 0, same as everything else.)
        """
        fields = await self.send_command(Commands.SPEAK_START)
        # Full duplex: bring the downlink up too, unless the caller already has
        # it running -- then leave it alone on stop.
        self._talk_started_audio = not self.is_audio_requested
        if self._talk_started_audio:
            await self.start_audio()

        self.is_talking = True
        self._talk_seq = 0
        self._talk_buffer = b''
        rate = fields.get(6, 8000)
        bits = fields.get(7, 16)
        channels = fields.get(8, 1)
        byte_rate = max(1, rate * max(1, bits // 8) * max(1, channels))
        self._talk_frame_ms = TALK_FRAME_BYTES * 1000 / byte_rate
        self.log.info('Talk-back open: %sHz %s-bit %sch, %dB frames every %.0fms',
                      rate, bits, channels, TALK_FRAME_BYTES, self._talk_frame_ms)
        return {'sample_rate': rate, 'bits': bits, 'channels': channels,
                'channel': TALK_CHANNEL, 'frame_bytes': TALK_FRAME_BYTES,
                'frame_ms': self._talk_frame_ms}

    async def send_audio(self, pcm: bytes):
        """Push signed 16-bit little-endian PCM to the camera's speaker.

        Buffered and emitted only as whole ``TALK_FRAME_BYTES`` frames, the size
        the vendor app uses. Pacing is the caller's job: one frame per
        ``frame_ms`` (40 ms), matching the app.
        """
        if not self.is_talking:
            raise NotConnectedError('Talk-back is not open; call start_talk() first')
        self._talk_buffer += pcm
        while len(self._talk_buffer) >= TALK_FRAME_BYTES:
            frame = self._talk_buffer[:TALK_FRAME_BYTES]
            self._talk_buffer = self._talk_buffer[TALK_FRAME_BYTES:]
            await self._send_talk_frame(frame)

    async def _send_talk_frame(self, frame: bytes):
        self._talk_seq += 1
        # Wall-clock milliseconds, like the app: its uplink timestamps are
        # absolute epoch ms, not a stream-relative counter.
        timestamp = int(time.time() * 1000)
        packet = build_av(
            self.session_key, TALK_CHANNEL, self._talk_seq, timestamp,
            MEDIA_FORMAT_PCM, frame,
            # The app encrypts all but the last 16 bytes and sends no prefix,
            # under header flag 8. None of that matches the downlink.
            enc_length=len(frame) - TALK_ENC_TRAILER,
            prefix=False, flag=Flag.NORMAL,
        )
        self.log.debug('talk> seq=%s ts=%s %dB (enc %d)', self._talk_seq, timestamp,
                       len(frame), len(frame) - TALK_ENC_TRAILER)
        await self._write(packet)

    async def stop_talk(self):
        """Close the uplink: SpeakStop, then AudioStop -- the app sends both."""
        if not self.is_talking:
            return
        if self._talk_buffer:
            await self._send_talk_frame(self._talk_buffer.ljust(TALK_FRAME_BYTES, SILENCE_BYTE))
            self._talk_buffer = b''
        self.is_talking = False
        await self.send_command(Commands.SPEAK_STOP)
        if self._talk_started_audio:
            self._talk_started_audio = False
            await self.stop_audio()

    # --- PTZ -------------------------------------------------------------
    async def ptz(self, direction):
        """Start moving. The camera keeps moving until ``ptz_stop()``."""
        code = PTZ_DIRECTIONS[direction] if isinstance(direction, str) else int(direction)
        await self.send_command(Commands.PTZ_CTRL, pb_varint(2, int(code)))

    async def ptz_stop(self):
        await self.ptz(Ptz.STOP)

    async def ptz_step(self, direction, duration=1.2):
        """Move for a bounded time, then stop -- what a UI button wants."""
        await self.ptz(direction)
        try:
            await asyncio.sleep(duration)
        finally:
            await self.ptz_stop()

    # --- network ---------------------------------------------------------
    async def get_network_info(self) -> dict:
        fields = await self.send_command(Commands.NET_INFO)
        return {
            'ssid': as_str(fields.get(2, b'')),
            'wifi_type': fields.get(3),
            'ip': as_str(fields.get(4, b'')),
            'netmask': as_str(fields.get(5, b'')),
            'gateway': as_str(fields.get(6, b'')),
            'dns1': as_str(fields.get(7, b'')),
            'dns2': as_str(fields.get(8, b'')),
            'mac': as_str(fields.get(9, b'')),
        }

    async def get_wifi_status(self) -> dict:
        fields = await self.send_command(Commands.WIFI_STATUS)
        return {
            'ssid': as_str(fields.get(2, b'')),
            # RSSI is a negative dBm value sent through an unsigned varint.
            'rssi': to_signed(fields.get(3, 0)) if isinstance(fields.get(3), int) else None,
        }

    async def scan_wifi(self, window=2.0) -> list:
        """List nearby access points.

        The camera answers in ONE response, and the APs are read out of it with
        pb_repeated. docs/FT04_COMMAND_REFERENCE.md said the command "repeats
        per AP" meaning one response each; this used to collect whole responses
        and keep field 2 of each, which on hardware returned exactly one network
        every time -- a single response arrives and pb_fields keeps only the
        first of a repeated field. Five live scans: one response each, 8-63 ms,
        bodies of 100-148 B, far more than the ~19 B one entry needs.

        That the surplus is further APs in a repeated field 2 is INFERRED from
        those lengths, not decoded: no capture in docs/decoded/ holds a multi-AP
        scan. Hence the debug log of the body below. If the layout turns out to
        be something else this degrades to the one AP it already returned.

        ``window`` is how long to keep listening for further responses. No
        second response has ever been observed, so this is mostly dead time;
        2 s is still ~30x the slowest answer seen.

        Duplicate SSIDs are returned as-is rather than merged: two radios
        advertising one name are two real entries, and nothing in the reply has
        been decoded well enough to tell them apart.
        """
        results = []
        for plain in await self.collect_responses(Commands.WIFI_SCAN, window=window):
            # The exact repeat layout is inferred from lengths, not from a
            # decoded capture -- log the body so the next hardware run can
            # settle it without a rebuild.
            self.log.debug('WifiScan body %dB %s', len(plain), plain.hex(' '))
            for entry in pb_repeated(plain, 2):
                fields = pb_fields(entry)
                ssid = as_str(fields.get(1, b''))
                if not ssid:
                    continue  # hidden or unnamed network: nothing to show or pick
                rssi = fields.get(2)
                results.append({'ssid': ssid,
                                'rssi': to_signed(rssi) if isinstance(rssi, int) else None})
        return results

    async def set_wifi(self, ssid: str, password: str, field5: str = WIFI_SET_FIELD5):
        """Tell the camera to join a Wi-Fi network.

        This is the one command here that can make the camera unreachable, and
        almost nothing about its outcome is knowable. Read before relying on it:

        * WifiSet answers with an empty body, so the reply carries no status.
          The only WifiSet ever captured (docs/decoded/net.txt:39) re-set the
          network the camera was ALREADY on, over the vendor cloud relay rather
          than a LAN session. What a *rejected* WifiSet answers has never been
          seen, and neither has a move to a different network, on any wire.
        * A camera that acts on this has to leave the current network, so on a
          LAN session the reply races the radio: no reply is as likely an
          outcome as a reply, and it does NOT mean failure. Callers must treat
          CommandError with ``code is None`` (a timeout) and NotConnectedError
          with ``sent`` set as "sent, outcome unknown", not as an error. Nothing
          observable from this host distinguishes a successful join from a
          camera that ignored the command or stranded itself.
        * There is no read-back. The only evidence of a join that exists is the
          SSID a *reconnected* session reads from NetInfo afterwards.

        ``field5`` is replayed verbatim from the capture; see WIFI_SET_FIELD5
        for why its meaning is unknown. Passing None or '' omits the field
        entirely -- a packet shape never seen on the wire, so it is a probe.
        """
        body = pb_string(2, str(ssid)) + pb_string(3, str(password))
        if field5:
            body += pb_string(5, str(field5))
        await self.send_command(Commands.WIFI_SET, body)

    # --- time -------------------------------------------------------------
    async def get_time(self) -> dict:
        """Read the camera clock.

        Returns the raw ``time``/``tz_offset_min`` the camera sent plus a
        rendered form -- ``local``, ``utc``, ``utc_offset`` and ``drift_s`` --
        because a bare epoch integer is unreadable and the only thing anyone
        actually wants to know is whether the clock is right.
        """
        fields = await self.send_command(Commands.TIME_GET)
        epoch, offset = fields.get(3), fields.get(8)
        return {'time': epoch, 'tz_offset_min': offset,
                **describe_time(epoch, offset)}

    async def set_time(self, when=None, tzname=None, dst=None, offset_min=None):
        """Set the clock. Defaults to this host's local time and zone."""
        now = when or datetime.datetime.now().astimezone()
        local = time.localtime(now.timestamp())
        if dst is None:
            # datetime.astimezone() yields a fixed-offset tzinfo whose dst() is
            # None on every platform, so ask the C library instead.
            dst = 1 if local.tm_isdst > 0 else 0
        if offset_min is None:
            # The app sends the zone's STANDARD offset together with a separate
            # DST flag -- Europe/Sofia in summer was captured as offset 120 with
            # dst 1, not as the current +180. time.timezone is that standard
            # offset, west-positive, hence the negation.
            offset_min = -time.timezone // 60
        if tzname is None:
            # The camera wants an IANA name ("Europe/Sofia"). zoneinfo exposes
            # it as .key; a plain fixed-offset tzinfo does not, so fall back to
            # the platform's abbreviation rather than sending nothing.
            tzname = getattr(now.tzinfo, 'key', None) or time.tzname[bool(dst)]
        body = (pb_varint(3, int(now.timestamp() * 1000)) + pb_string(6, tzname)
                + pb_varint(7, int(dst)) + pb_varint(8, offset_min))
        await self.send_command(Commands.TIME_SET, body)
        return {'time': int(now.timestamp()), 'tz_offset_min': offset_min, 'timezone': tzname}

    # --- motion ------------------------------------------------------------
    async def get_motion_sensitivity(self) -> MotionSensitivity:
        fields = await self.send_command(Commands.MOTION_SENSITIVITY_GET)
        return MotionSensitivity(fields.get(1, 0))

    async def set_motion_sensitivity(self, level):
        level = int(level)
        # Level 0 (close/off) is sent as an empty body -- the app never writes
        # an explicit zero, and the camera reads a missing field as 0.
        body = pb_varint(2, level) if level else b''
        await self.send_command(Commands.MOTION_SENSITIVITY_SET, body)

    async def get_motion_schedule(self) -> dict:
        """Read the arming schedule: which weekdays, and how late each runs."""
        fields = await self.send_command(Commands.MOTION_SCHEDULE_GET)
        window = pb_fields(fields.get(2, b'')) if isinstance(fields.get(2), (bytes, bytearray)) else {}
        end = pb_fields(window.get(3, b'')) if isinstance(window.get(3), (bytes, bytearray)) else {}
        days = window.get(2, b'')
        return {
            'enabled': bool(fields.get(1, 0)),
            # Weekday bytes 00..06 = Sunday..Saturday, one byte per armed day.
            'weekdays': list(days) if isinstance(days, (bytes, bytearray)) else [],
            'end_sec': end.get(2),
        }

    async def set_motion_schedule(self, enabled=True, weekdays=WEEKDAYS,
                                  end_sec=SCHEDULE_END_ALL_DAY):
        window = pb_bytes(2, bytes(weekdays)) + pb_message(3, pb_varint(2, int(end_sec)))
        # Disabling is expressed by omitting field 2, exactly as the app does.
        body = (pb_varint(2, 1) if enabled else b'') + pb_message(3, window)
        await self.send_command(Commands.MOTION_SCHEDULE_SET, body)

    # --- night vision ------------------------------------------------------
    async def get_ircut(self) -> IRCutMode:
        fields = await self.send_command(Commands.IRCUT_GET)
        return IRCutMode(fields.get(1, IRCutMode.AUTO))

    async def set_ircut(self, mode):
        await self.send_command(Commands.IRCUT_SET, pb_varint(2, int(mode)))

    async def get_night_light(self) -> NightLightMode:
        """Night-light mode, read through datapoint 589."""
        value = await self.datapoint_get(Datapoint.NIGHT_LIGHT_GET)
        return NightLightMode(value[0] if value else 0)

    async def set_night_light(self, mode):
        """Set night-light mode through datapoint 588, read back through 589.

        What the capture proves is the transport, not the labels: 588 = 2 read
        back from 589 as 2, and 588 = 0 read back as 0
        (docs/decoded/night.txt:50-64). A third write of 1 is the last
        ExecIOTCMD in that capture and was never read back.

        The member NAMES come from the order the app's buttons were pressed
        while night.pcap was recorded -- prose, not bytes, and the same evidence
        class that once had motion LOW and MEDIUM swapped. Treat "FULL_COLOR
        turns the white illuminator on" as unverified. The light that is
        *observed* to change on this unit is :meth:`toggle_flashlight`.
        """
        await self.datapoint_set(Datapoint.NIGHT_LIGHT_SET, bytes([int(mode)]))

    async def enable_night_light(self):
        """Replay the app's night-screen write (dp 23116 = 01). **Unverified.**

        Takes no argument because only the ``01`` direction has ever been seen.
        The app writes it once, with the did, as its night screen opens
        (docs/decoded/night.txt:18); no capture ever writes ``00``, so an
        ``enabled=False`` would be a byte this protocol has not been observed
        to carry. The ack has no value field and there is no get datapoint, so
        nothing can confirm it landed -- and on this unit neither direction
        produced any visible effect. Kept as a probe for parity with the app,
        which is why it is not on the test server's page.
        """
        await self.datapoint_set(Datapoint.NIGHT_LIGHT_ENABLE, b'\x01', with_did=True)

    async def toggle_flashlight(self):
        """Flip the white light. **There is no way to read its state.**

        dp 201 is momentary: the camera flips the light for any ExecIOTCMD
        addressed to it and ignores the payload, so this takes no argument --
        there is nothing to set, only a press. The app's exact bytes go on the
        wire, did included, because both captured presses carried them
        (docs/decoded/light.txt:55,59).

        There is deliberately no getter. A value-less "read" of dp 201 actuates
        it just as a write does, so asking the light what it is doing turns it
        off; see :data:`UNREADABLE_DATAPOINTS`. A caller that needs to know the
        state has to look at the picture.
        """
        await self.datapoint_set(Datapoint.FLASHLIGHT_TOGGLE,
                                 FLASHLIGHT_TOGGLE_PAYLOAD, with_did=True)

    # --- orientation / LED / system ---------------------------------------
    async def get_flip(self) -> FlipMode:
        fields = await self.send_command(Commands.FLIP_GET)
        return FlipMode(fields.get(1, FlipMode.UPRIGHT))

    async def set_flip(self, mode):
        await self.send_command(Commands.FLIP_SET, pb_varint(2, int(mode)))

    async def set_status_led(self, value):
        """Status indicator LED. No getter has ever been captured, so the page
        cannot show a current value for it -- write-only, as far as we know."""
        await self.send_command(Commands.STATUS_LED_SET, pb_varint(2, int(value)))

    async def reboot(self):
        # Fire and forget: the camera may go down before it answers.
        await self.send_command(Commands.REBOOT, with_response=False)

    # --- generic datapoints -------------------------------------------------
    def _datapoint_body(self, dp, value=None, with_did=False):
        """Build the ExecIOTCMD envelope: ``{1:{[2:did,] 4:{1:dp[,2:val]}, 6:ts, 7:1}}``.

        Whether the did is present is per-datapoint, copied from the app: 588
        and 589 go without it (docs/decoded/night.txt:46,50), while 201, 554,
        23113, 23116 and 23131 all carry it (light.txt:39,43,51,55;
        night.txt:18). The default is False only because 588/589 are the
        busiest callers here.

        Field 7 is 1 on reads as well as writes -- it is not a write flag; the
        read of 589 at night.txt:46 sends 7:1 too. The only read/write
        discriminator on the wire is whether the inner value field is present.
        """
        inner = pb_varint(1, int(dp))
        if value is not None:
            inner += pb_bytes(2, value)
        parts = pb_string(2, self.dev.dev_id) if with_did else b''
        parts += pb_message(4, inner) + pb_varint(6, int(time.time())) + pb_varint(7, 1)
        return pb_message(1, parts)

    async def datapoint_set(self, dp, value: bytes, with_did=False):
        """Write a datapoint. The ack never carries the new value: 588 echoes
        ``00`` whatever was written (docs/decoded/night.txt:50-52), and 201 and
        23116 answer with no value field at all. Confirm a write by reading its
        paired get datapoint, where one exists."""
        await self.send_command(Commands.EXEC_IOT_CMD,
                                self._datapoint_body(dp, value, with_did))

    async def datapoint_get(self, dp, with_did=False) -> bytes | None:
        """Read a datapoint. Returns None when the value comes back absent.

        Absent is not the same as zero: for the identical request shape the
        camera answers dp 554 with ``00`` but 23131 and 23113 with no value
        field at all (docs/decoded/light.txt:39-53). Absent means only that the
        camera had nothing to say about it.

        Raises ValueError for a datapoint that actuates when read -- see
        :data:`UNREADABLE_DATAPOINTS`. That is not a hypothetical guard: a
        "read" of dp 201 presses the flashlight button.
        """
        reason = UNREADABLE_DATAPOINTS.get(dp)
        if reason is not None:
            raise ValueError(f'datapoint {datapoint_name(int(dp))} cannot be read: {reason}')
        fields = await self.send_command(Commands.EXEC_IOT_CMD,
                                         self._datapoint_body(dp, None, with_did))
        tree = walk(fields.get(1, b'')) if isinstance(fields.get(1), (bytes, bytearray)) else None
        value = find(tree, [4, 2])
        return bytes(value) if isinstance(value, (bytes, bytearray)) else None


def make_session(device: DeviceDescriptor, on_device_lost=None, on_video_state_change=None,
                 on_event=None, prefix=PREFIX) -> Session:
    """Create a session for the camera."""
    return Session(
        device,
        on_disconnect=on_device_lost,
        on_video_state_change=on_video_state_change,
        on_event=on_event,
        prefix=prefix,
    )
