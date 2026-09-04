"""Value objects and device-facing enums. Leaf module: imports nothing internal.

The enum values are the raw wire values, byte-verified against hardware
captures (docs/FT04_COMMAND_REFERENCE.md). Several of them are non-intuitive --
see the comments -- so always go through these names rather than guessing.
"""
import datetime
from enum import IntEnum


class FlipMode(IntEnum):
    """Image orientation, cmd 2613/2649 field 2/1."""

    UPRIGHT = 1
    MIRROR = 2  # horizontal flip
    FLIP = 3  # vertical flip
    ROTATE_180 = 4


class IRCutMode(IntEnum):
    """Night-vision (IR-cut filter) mode, cmd 2635/2636."""

    ON = 1  # force night / IR
    OFF = 2  # force day / colour
    AUTO = 3


class NightLightMode(IntEnum):
    """Night-light mode, datapoint 588 (set) / 589 (get) via ExecIOTCMD.

    The VALUES are byte-verified: 588 = 2 read back from 589 as 2 and 588 = 0
    read back as 0 (docs/decoded/night.txt:50-64). The NAMES are not -- they
    come from the order the vendor app's buttons were pressed while the capture
    was recorded, which is prose about the capture, not the capture. That is the
    same evidence class that once had MotionSensitivity LOW and MEDIUM swapped,
    so do not rely on FULL_COLOR meaning "white LED on" until someone has
    watched the light while writing it.

    This is *not* the camera's flashlight. That is datapoint 201, a stateless
    toggle with no mode and no read-back -- see Session.toggle_flashlight.
    """

    INTELLIGENT = 0
    BLACK_WHITE = 1
    FULL_COLOR = 2


class MotionSensitivity(IntEnum):
    """Motion sensitivity, cmd 2661/2662.

    Ordinary ordering: low < medium < high. CLOSE (0) means detection off and
    is sent as an empty body.

    An earlier revision had LOW and MEDIUM swapped, on the strength of a
    hand-written note about which buttons were pressed while motion.pcap was
    recorded. The capture only proves the app wrote 1, 2, 0 then 3 -- it cannot
    say which label sat on which button. Corrected against the device itself.
    """

    CLOSE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class StatusLed(IntEnum):
    """Status indicator LED, cmd 2633 field 2. No getter has been captured, so
    the page cannot show a current value -- write-only, as far as we know."""

    ON = 1
    OFF = 2


class EventCode(IntEnum):
    """Alarm codes carried in byte 0 of the cmd-13 event blob.

    The set comes from the device's own skill.json (``events: [9, 12, 255]``);
    only MOTION has been captured so far.
    """

    EVENT9 = 9
    MOTION = 12
    EVENT255 = 255


class DeviceDescriptor:
    """One camera as returned by UDP discovery.

    Unlike aiopppp's descriptor this carries ``lsign``: it is the LanAuth
    credential, it is minted fresh per discovery reply and it expires, so a
    descriptor is only good for one connection attempt. Re-discover before
    every (re)connect.

    ``addr`` is the address the reply actually came FROM, which is not
    necessarily the address the camera reports for itself (field 2) -- on a
    double-NATed unit the self-reported IP is unreachable.
    """

    def __init__(self, dev_id, addr, port=20190, lsign='', sdk='', skill=None):
        self.dev_id = dev_id
        self.addr = addr
        self.port = port
        self.lsign = lsign
        self.sdk = sdk
        self.skill = skill or {}

    def __eq__(self, other):
        return self.dev_id == other.dev_id

    def __hash__(self):
        return hash((self.dev_id, self.addr, self.port))

    def __str__(self):
        return f'{self.dev_id}@{self.addr}:{self.port}'

    def __repr__(self):
        return f'DeviceDescriptor({self})'

    def capability(self, name, default=None):
        """Read one flag from the device's own capability declaration.

        ``skill.json`` carries a per-module ``mconf`` block (media config) and a
        ``base`` block. The device is the authority on what hardware it has --
        ``support_cmds`` only says a command *exists*, not that anything is
        wired to it.
        """
        for module in self.skill.get('mconf') or []:
            conf = module.get('conf') or {}
            if name in conf:
                return conf[name]
        return (self.skill.get('base') or {}).get(name, default)

    @property
    def has_speaker(self) -> bool:
        """What skill.json claims about a speaker -- **do not gate talk-back on
        this.**

        The FT04 reports ``speaker: 0`` and nevertheless has a working speaker:
        talk-back runs fine from the vendor app. The flag is either wrong or
        means something other than "a speaker is fitted". Kept because it is
        what the device says, not because it is true.
        """
        return bool(self.capability('speaker', 0))

    @property
    def supported_commands(self):
        """Command IDs the camera declares in its skill.json, as a set of ints.

        Empty when discovery carried no skill blob -- callers must treat an
        empty set as "unknown", not as "nothing supported".
        """
        cmds = set()
        for group in self.skill.get('support_cmds') or []:
            cmds.update(int(x) for x in group.get('cmds') or [])
        return cmds


class VideoFrame:
    def __init__(self, idx, data):
        self.idx = idx
        self.data = data  # one complete JPEG


class AudioFrame:
    def __init__(self, idx, data, sample_rate=8000):
        self.idx = idx
        self.data = data  # signed 16-bit little-endian PCM
        self.sample_rate = sample_rate


def describe_time(epoch, offset_min=None, now=None) -> dict:
    """Render a camera clock reading in a form a human can check at a glance.

    ``epoch`` is what TimeGet(2632) field 3 carries. The camera answers in
    seconds but TimeSet is written in milliseconds, and firmware that echoes
    the set value back has been seen to answer in milliseconds too, so the
    magnitude decides rather than the field number.

    ``drift_s`` is the camera's clock minus this host's, positive when the
    camera is ahead. That is the number that actually matters -- a camera whose
    clock has drifted stamps its motion events wrongly.
    """
    if epoch in (None, 0):
        return {}
    epoch = int(epoch)
    # Anything past ~2001 in milliseconds is far beyond any plausible seconds
    # value (which would be year 33658), so the split is unambiguous.
    if epoch > 100_000_000_000:
        epoch //= 1000
    tz = (datetime.timezone(datetime.timedelta(minutes=int(offset_min)))
          if offset_min is not None else datetime.timezone.utc)
    when = datetime.datetime.fromtimestamp(epoch, tz)
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    raw_offset = when.strftime('%z')  # '+0200'
    utc_offset = f'{raw_offset[:3]}:{raw_offset[3:]}'
    return {
        'local': f'{when:%Y-%m-%d %H:%M:%S} {utc_offset}',
        'utc': f'{datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc):%Y-%m-%d %H:%M:%S} UTC',
        'utc_offset': utc_offset,
        'drift_s': round(epoch - now),
    }
