"""High-level camera facade: discover, connect, control, reconnect.

Every connect starts with a fresh discovery. That is not a convenience -- the
LanAuth credential (``lsign``) is minted per discovery reply and expires, so a
reconnect that reused the old descriptor would be rejected.
"""
import asyncio
import contextlib
import logging

from .const import PREFIX, WIFI_SET_FIELD5
from .discover import Discovery
from .exceptions import AlreadyConnectedError, NotConnectedError
from .session import Session, make_session
from .types import DeviceDescriptor

logger = logging.getLogger(__name__)


async def find_device(ip_address: str, timeout: int = 20, prefix: str = PREFIX) -> DeviceDescriptor:
    """Discover one camera by address and return a descriptor with a live lsign."""
    loop = asyncio.get_running_loop()
    found = loop.create_future()

    def on_device_found(device):
        # Discovery also reaches the broadcast address, so other cameras on the
        # subnet can answer; take only the one that was asked for.
        if device.addr == ip_address and not found.done():
            found.set_result(device)

    discovery = Discovery(ip_address, prefix=prefix)
    task = loop.create_task(discovery.discover(on_device_found, period=1))
    try:
        await asyncio.wait([task, found], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    if found.done():
        return found.result()
    raise TimeoutError(f'No discovery reply from {ip_address}')


class Device:
    """One camera, connected on demand.

    Usable as an async context manager::

        async with Device('192.168.0.170') as cam:
            await cam.start_video()
            frame = await cam.get_video_frame()
    """

    def __init__(self, ip_address: str, on_video_state_change=None, on_event=None,
                 on_reconnect=None, prefix: str = PREFIX):
        self.ip_address = ip_address
        self.prefix = prefix
        self.descriptor: DeviceDescriptor | None = None
        self.properties: dict = {}
        self._session: Session | None = None
        self.on_video_state_change = on_video_state_change
        self.on_event = on_event
        # Called once after a successful auto-reconnect, in the same task
        # that re-established the session. Use this to drive the head back
        # to a known position: the FT04 reboots to (0, 0) and has no
        # working preset storage, so a reconnect is the natural place to
        # replay a recorded move sequence (see ``Device.home`` /
        # ``Device.recall_preset``). Exceptions are logged and swallowed
        # so a buggy callback doesn't kill the reconnect.
        self.on_reconnect = on_reconnect

        self.enable_reconnect = False
        self._reconnect_task = None
        self._closing = False
        # Remembered so a reconnect can resume whatever was streaming.
        self._want_video = False
        self._want_audio = False
        self.reconnect_min_delay = 1
        self.reconnect_max_delay = 30

    # --- connection ------------------------------------------------------
    async def connect(self, timeout: int = 15):
        if self.is_connected:
            raise AlreadyConnectedError('Already connected to the camera')
        self._closing = False

        # Fresh discovery every time: lsign is single-use.
        self.descriptor = await find_device(self.ip_address, timeout=timeout, prefix=self.prefix)

        self._session = make_session(
            self.descriptor,
            on_device_lost=lambda dev: self.on_device_lost(),
            on_video_state_change=self.on_video_state_change,
            on_event=self.on_event,
            prefix=self.prefix,
        )
        session = self._session
        session.start()
        done, _ = await asyncio.wait(
            [
                asyncio.ensure_future(session.device_is_ready.wait()),
                *[asyncio.shield(t) for t in session.running_tasks()],
            ],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # device_is_ready is also set on teardown, so "ready" is not the same as
        # "connected" -- surface whatever failure the session tasks recorded.
        finished = [t for t in session.running_tasks() if t.done() and not t.cancelled()]
        if finished:
            await asyncio.gather(*finished)
        if not done:
            if self.is_connected:
                await self.close()
            raise TimeoutError('Timeout connecting to the camera')
        if not self.is_connected:
            raise NotConnectedError('Device lost during connection')

        self.properties = self.session.dev_properties
        return self.properties

    def on_device_lost(self):
        self._session = None
        if self.enable_reconnect and not self._closing:
            if self._reconnect_task is None or self._reconnect_task.done():
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Re-establish the session with exponential backoff, resuming streams.

        Each attempt re-discovers, which is what makes a reconnect possible at
        all: the previous lsign is spent.
        """
        delay = self.reconnect_min_delay
        while self.enable_reconnect and not self._closing and not self.is_connected:
            try:
                await asyncio.sleep(delay)
                if self._closing:
                    return
                await self.connect()
                logger.info('Reconnected to %s', self.ip_address)
                if self._want_video:
                    await self.start_video()
                if self._want_audio:
                    await self.start_audio()
                if self.on_reconnect is not None:
                    try:
                        await self.on_reconnect(self)
                    except Exception as err:
                        logger.warning(
                            'on_reconnect hook for %s raised %s; ignoring',
                            self.ip_address, err,
                        )
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.debug('Reconnect to %s failed (%s); retrying in %ss',
                             self.ip_address, err, delay)
                delay = min(delay * 2, self.reconnect_max_delay)

    @property
    def is_connected(self) -> bool:
        return bool(self._session)

    @property
    def session(self) -> Session:
        if not self._session:
            raise NotConnectedError('Not connected to the camera')
        return self._session

    @property
    def has_speaker(self) -> bool:
        """Whether talk-back is real on this unit (declared, not just commanded)."""
        return bool(self.descriptor and self.descriptor.has_speaker)

    @property
    def dev_id(self):
        return self.descriptor.dev_id if self.descriptor else None

    async def close(self):
        self._closing = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
        self._reconnect_task = None

        if self._session:
            session = self._session
            self._session = None
            session.stop()
            if session.main_task:
                with contextlib.suppress(asyncio.CancelledError):
                    await session.main_task

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # --- media ------------------------------------------------------------
    @property
    def is_video_requested(self) -> bool:
        return self.session.is_video_requested

    async def start_video(self):
        self._want_video = True
        return await self.session.start_video()

    async def stop_video(self):
        self._want_video = False
        return await self.session.stop_video()

    async def get_video_frame(self):
        frame = await self.session.get_video_frame()
        if not frame:
            raise NotConnectedError('Not connected to the camera')
        return frame

    async def start_audio(self):
        self._want_audio = True
        return await self.session.start_audio()

    async def stop_audio(self):
        self._want_audio = False
        return await self.session.stop_audio()

    async def get_audio_frame(self):
        return await self.session.get_audio_frame()

    async def start_talk(self):
        """Open talk-back (SpeakStart + AudioPlay, as the vendor app does)."""
        return await self.session.start_talk()

    async def send_audio(self, pcm: bytes):
        return await self.session.send_audio(pcm)

    async def stop_talk(self):
        return await self.session.stop_talk()

    # --- control ----------------------------------------------------------
    async def ptz(self, direction):
        return await self.session.ptz(direction)

    async def ptz_stop(self):
        return await self.session.ptz_stop()

    async def ptz_step(self, direction, duration=1.2):
        return await self.session.ptz_step(direction, duration)

    # --- preset / cruise experiments -------------------------------------
    # The new SDK's PtzCtrl has the cruise-control code space 1..4/22/24
    # (extracted from libgojni.so via Ghidra). Whether the FT04 firmware
    # accepts them is the open question these methods exist to test.
    # See docs/SDK_COMMANDS_EXTRACTED.md.
    async def ptz_raw(self, func_code, para1=0, para2=0, channel=0):
        return await self.session.ptz_raw(func_code, para1, para2, channel)

    async def ptz_preset_begin(self, preset_id, channel=0):
        return await self.session.ptz_preset_begin(preset_id, channel)

    async def ptz_preset_end(self, preset_id, channel=0):
        return await self.session.ptz_preset_end(preset_id, channel)

    async def ptz_preset_call(self, preset_id, channel=0):
        return await self.session.ptz_preset_call(preset_id, channel)

    async def ptz_preset_delete(self, preset_id, channel=0):
        return await self.session.ptz_preset_delete(preset_id, channel)

    async def ptz_cruise_start(self, dwell_secs, channel=0):
        return await self.session.ptz_cruise_start(dwell_secs, channel)

    async def ptz_cruise_stop(self, channel=0):
        return await self.session.ptz_cruise_stop(channel)

    # --- position-less "home" + recorded move sequence ---------------------
    # The FT04 has no working preset storage. The head reboots to (0, 0)
    # and the only way to land on a known position is to drive the head
    # there from (0, 0) using timed moves. See Session.home / .recall_preset
    # for the rationale.
    async def home(self, traverse_secs: float = 18.0):
        """Slam the PTZ head to the (0, 0) corner. See Session.home."""
        return await self.session.home(traverse_secs)

    async def recall_preset(self, sequence, settle_secs: float = 0.4):
        """Replay a recorded sequence of (direction, duration) from home."""
        return await self.session.recall_preset(sequence, settle_secs)

    async def send_raw_command(self, cmdid, body_hex='', timeout=None):
        return await self.session.send_raw_command(cmdid, body_hex, timeout)

    async def get_network_info(self):
        return await self.session.get_network_info()

    async def get_wifi_status(self):
        return await self.session.get_wifi_status()

    async def scan_wifi(self, window=2.0):
        return await self.session.scan_wifi(window=window)

    async def set_wifi(self, ssid, password, field5=WIFI_SET_FIELD5):
        """Join a Wi-Fi network. Read Session.set_wifi before calling this:
        it can strand the camera, and a missing reply is not a failure."""
        return await self.session.set_wifi(ssid, password, field5)

    async def get_time(self):
        return await self.session.get_time()

    async def set_time(self, when=None, tzname=None, dst=None, offset_min=None):
        return await self.session.set_time(when, tzname, dst, offset_min)

    async def get_motion_sensitivity(self):
        return await self.session.get_motion_sensitivity()

    async def set_motion_sensitivity(self, level):
        return await self.session.set_motion_sensitivity(level)

    async def get_motion_schedule(self):
        return await self.session.get_motion_schedule()

    async def set_motion_schedule(self, enabled=True, weekdays=None, end_sec=None):
        kwargs = {}
        if weekdays is not None:
            kwargs['weekdays'] = weekdays
        if end_sec is not None:
            kwargs['end_sec'] = end_sec
        return await self.session.set_motion_schedule(enabled, **kwargs)

    async def get_ircut(self):
        return await self.session.get_ircut()

    async def set_ircut(self, mode):
        return await self.session.set_ircut(mode)

    async def get_night_light(self):
        return await self.session.get_night_light()

    async def set_night_light(self, mode):
        return await self.session.set_night_light(mode)

    async def enable_night_light(self):
        return await self.session.enable_night_light()

    # No get_flashlight counterpart: reading cid 201 toggles the light, so the
    # only honest flashlight API is the press itself.
    async def toggle_flashlight(self):
        return await self.session.toggle_flashlight()

    async def get_flip(self):
        return await self.session.get_flip()

    async def set_flip(self, mode):
        return await self.session.set_flip(mode)

    async def set_status_led(self, value):
        return await self.session.set_status_led(value)

    async def reboot(self):
        return await self.session.reboot()

    async def iot_cmd_get(self, cid, with_did=False):
        return await self.session.iot_cmd_get(cid, with_did)

    async def iot_cmd_set(self, cid, value, with_did=False):
        return await self.session.iot_cmd_set(cid, value, with_did)

    datapoint_get = iot_cmd_get  # deprecated names, kept for callers
    datapoint_set = iot_cmd_set
