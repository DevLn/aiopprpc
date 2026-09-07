"""Wire constants: ports, framing, crypto secret, command IDs, IoT command ids.

Everything here is byte-verified against real FT04/BK7252 hardware; the
authoritative write-up is docs/FT04_COMMAND_REFERENCE.md. Following the
aiopppp convention, names annotate the raw numbers but never hide them --
every helper accepts and returns plain ints.

Leaf module: imports nothing internal.
"""
from enum import IntEnum

#: Discovery (UDP broadcast) and the control/AV session (TCP) share one port.
PORT = 20190

#: UDP datagrams carry this magic ahead of the fixed header. TCP has none.
UDP_MAGIC = b'Qp'

#: Control-plane AES prefix. This is a real firmware secret, proven on FT04
#: fw 3.12.13 (126/126 captured enc=3 packets decrypt with valid PKCS#7 and
#: coherent protobuf). Other units may ship a different one, so every entry
#: point that uses it takes an override.
PREFIX = 'A2r0i1m1a2M0a1x6toriQue'

#: Field 5 of WifiSet(2602). The one captured WifiSet carried the literal
#: "359" (docs/decoded/net.txt:39) and it is replayed verbatim because it is
#: the only value ever seen on that field.
#:
#: Best current reading: **the account's country dialling code**. +359 is
#: Bulgaria, which is where the owner of the captured device lives, and every
#: capture sets the camera clock to Europe/Sofia. The same "359" prefixes that
#: account's cloud notify topic (/xc/2u/notify/359-<32 hex>; light.txt:19,
#: reboot.txt:36, motiondetected.txt:90) -- and that 32-hex is NOT derived from
#: the device (it matches no hash of the did PPFT04049C9EEFF92B, the MAC, or
#: any combination tried), so the topic reads as <country>-<account hash>.
#: Vendor apps of this class key accounts by phone number, which fits exactly.
#:
#: Note this makes it an ACCOUNT attribute, not a radio setting. Three reasons
#: not to treat it as a Wi-Fi regulatory domain: the identical literal
#: namespaces an MQTT topic, where a regulatory domain has no business; the
#: value beside it is account-derived; and real Wi-Fi country encodings are
#: ISO-3166 ("BG", or numeric 100 for Bulgaria), never the E.164 dialling code.
#: So do not tune this hoping to change channels or TX power.
#:
#: Still unproven from the camera's side: it never emits the value in either
#: direction, so whether it even reads the field is unknown. An earlier doc
#: revision called it "the leading digits of the device id", which the bytes
#: refute outright -- the did contains no "359". One capture from an account
#: registered in another country would settle the whole question.
#: Overridable because if this reading is right it differs per account.
WIFI_SET_FIELD5 = '359'


class MessageType(IntEnum):
    """High nibble of the fixed header byte."""

    HEARTBEAT = 3
    RPC = 4  # protobuf RPC -- everything we send
    JSON_RPC = 5
    AV = 6  # video/audio media


class Flag(IntEnum):
    """Low nibble of the fixed header byte. Only these three are ever seen."""

    NORMAL = 8
    ALT = 9  # the camera sends AV packets with this
    EXT = 10


class Encryption(IntEnum):
    """Top 6 bits of the RPC enc/type byte."""

    NONE = 0
    AES256 = 3


class RpcType(IntEnum):
    """Low 2 bits of the RPC enc/type byte."""

    REQ = 0
    RESP = 1


#: Header byte for everything we put on the TCP control channel: type 4, flag 8.
TCP_RPC_HEADER = (MessageType.RPC << 4) | Flag.NORMAL
#: ...and for the UDP discovery datagram, which additionally carries UDP_MAGIC.
UDP_RPC_HEADER = TCP_RPC_HEADER


class Commands(IntEnum):
    """Command IDs. Names marked "marker" carry an empty body both ways."""

    NOTIFY = 13  # camera -> us: pushed event (motion, device online)
    SYNC_CONN = 106
    CONN_HB = 107  # camera -> us: heartbeat; reply promptly or it hangs up

    DISCOVERY = 2600  # UDP broadcast only
    WIFI_SCAN = 2601
    WIFI_SET = 2602
    WIFI_STATUS = 2603
    VIDEO_PLAY = 2610
    VIDEO_STOP = 2611
    FLIP_SET = 2613
    AUDIO_PLAY = 2614
    AUDIO_STOP = 2615
    SPEAK_START = 2616  # talk-back: open the uplink, then push AV packets
    SPEAK_STOP = 2617
    TIME_SET = 2631
    TIME_GET = 2632
    STATUS_LED_SET = 2633
    IRCUT_SET = 2635
    IRCUT_GET = 2636
    # The names below are the library's functional labels, kept because they
    # describe what the camera was OBSERVED to do. The SDK's own name for the
    # same id (docs/SDK_COMMANDS_EXTRACTED.md, ServiceDesc table) is noted
    # where it differs; a capture, not this comment, decides which is right.
    LIVE_VIEW_STOP = 2638  # sent when live view closes -- SDK: SecretGet
    MOTION_SCHEDULE_SET = 2639  # SDK: NotifySet
    MOTION_SCHEDULE_GET = 2640  # SDK: NotifyGet
    LIVE_VIEW_START = 2645  # sent when live view opens -- SDK: PspList (preset list read)
    REBOOT = 2647
    FLIP_GET = 2649
    LAN_AUTH = 2650
    CAPABILITIES = 2653  # {} -> {1:4, 2:<big>, 3:<big>}, first each session -- SDK: StorageInfo, but the reply does not read as sizes
    MOTION_SENSITIVITY_SET = 2661  # SDK: AlarmSet
    MOTION_SENSITIVITY_GET = 2662  # SDK: AlarmGet
    PTZ_CTRL = 2664
    NET_INFO = 2685
    EXEC_IOT_CMD = 2780  # carries IoT commands: PpiotCmd{cmds: [BaseCmd{cid, pint}]}


#: Command IDs that nothing in the library sends -- ids the camera advertises in
#: ``skill.json support_cmds``, and the SDK's preset/cruise family a probe may
#: send -- so they have no place in ``Commands``. Naming them makes a capability
#: dump or a probe log readable instead of a wall of digits.
#:
#: Names are the SDK's own, from the ServiceDesc table in
#: docs/SDK_COMMANDS_EXTRACTED.md (every one of the ~30 ids the library uses
#: matched that table). A name says what the SDK calls the command, not what
#: the FT04 firmware does with it; a trailing ``?`` marks the one id whose
#: observed reply contradicts the SDK name.
OTHER_COMMAND_NAMES = {
    60: 'PUBLIC_60?',  # not an ipc command; only id in the PUBLIC class
    2612: 'VIDEO_QOS_SET',
    2622: 'HISTORY_PLAN_GET',  # answers a weekday+time schedule
    2625: 'HISTORY_PLAY',
    2626: 'HISTORY_PAUSE',
    2627: 'HISTORY_THUM_GET',
    2628: 'HISTORY_DEL',
    2630: 'CONFIG_GET',
    2643: 'PSP_ADD',  # preset family: undeclared by the FT04, untested -- see probe_presets.py
    2644: 'PSP_DEL',
    2646: 'PSP_CALL',
    2648: 'RESET',  # factory reset -- destructive, never send it blind
    2651: 'TIMEDCRUISE_SET',
    2652: 'TIMEDCRUISE_GET',
    2653: 'STORAGE_INFO?',  # SDK name; the {1:4, 2:<big>, 3:<big>} reply does not read as sizes
    2663: 'SCREENSHOT',
}

COMMAND_NAMES = {int(cmd): cmd.name for cmd in Commands}
COMMAND_NAMES.update(OTHER_COMMAND_NAMES)

#: ``support_cmds`` is one flat list of *cids*: JSON ``SkillCmd{class_code,
#: cmds}``, the twin of ``ModelSupportCmds{class_code, cids}`` in the SDK's
#: ``common.proto``. Two id spaces share it: RPC command ids (>= 2600 in this
#: SDK; the ``Commands`` above) and IoT command ids, the ``cid`` of a
#: ``BaseCmd`` carried inside ExecIOTCMD(2780). Below this bound an IPAV entry
#: is an IoT cid. The camera does not declare every cid it answers: 201, 554,
#: 588 and 589 all work and none is listed. Earlier revisions called IoT cids
#: "datapoints"; the SDK reserves that word for a separate DataPointCmd/dp_id
#: mechanism (SkillDpconf, DPData) that this camera never uses.
IOT_CID_MAX = 2000
DATAPOINT_ID_MAX = IOT_CID_MAX  # deprecated name, kept for callers


def command_name(value: int) -> str:
    """Annotate a command ID without hiding it: ``2664 (PTZ_CTRL)``."""
    name = COMMAND_NAMES.get(int(value))
    return f'{value} ({name})' if name else str(value)


def iot_cmd_name(value: int) -> str:
    """Same, for IoT cids: ``589 (NIGHT_LIGHT_GET)``."""
    name = IOT_CMD_NAMES.get(int(value))
    return f'{value} ({name})' if name else str(value)


datapoint_name = iot_cmd_name  # deprecated name, kept for callers


def describe_supported(groups) -> dict:
    """Annotate a raw ``skill.json support_cmds`` array.

    Takes the array itself, not a flattened set, because the ``class_code`` is
    load-bearing: the IPAV group mixes RPC command ids with IoT cids (the
    ``BaseCmd.cid`` sent through ExecIOTCMD), while ``PUBLIC``'s small numbers
    are neither. Returns ``{class_code: {'commands': [...], 'iot_cids': [...]}}``
    with every id annotated and sorted; a class with no IoT cids omits the key.

    Neither list is exhaustive of what the camera answers: night light
    (588/589) is absent from this unit's ``support_cmds`` and works anyway. An
    id that is missing here is **undeclared**, not unsupported.
    """
    described = {}
    for group in groups or []:
        class_code = group.get('class_code') or '?'
        commands, iot_cids = [], []
        for value in sorted(int(i) for i in group.get('cmds') or []):
            # Only IPAV is known to mix the two id spaces; leave other classes
            # alone rather than inventing an IoT cid that was never one.
            if class_code == 'IPAV' and value < IOT_CID_MAX:
                iot_cids.append(iot_cmd_name(value))
            else:
                commands.append(command_name(value))
        described[class_code] = {'commands': commands}
        if iot_cids:
            described[class_code]['iot_cids'] = iot_cids
    return described


class Ptz(IntEnum):
    """PtzCtrl(2664) field 2. Motion is CONTINUOUS until STOP is sent.

    Direction<->arrow mapping is confirmed on hardware and matches the vendor
    app (on a ceiling mount the physical motion can look mirrored; the app
    behaves the same way).
    """

    STOP = 10
    UP = 11
    DOWN = 12
    LEFT = 13
    RIGHT = 14


PTZ_DIRECTIONS = {
    'up': Ptz.UP,
    'down': Ptz.DOWN,
    'left': Ptz.LEFT,
    'right': Ptz.RIGHT,
    'stop': Ptz.STOP,
}


class IotCmd(IntEnum):
    """IoT command ids: the ``cid`` of a ``BaseCmd`` sent inside ExecIOTCMD (2780).

    The SDK's ``common.proto`` frames ExecIOTCMD as ``PpiotCmd{did, cmds:
    [BaseCmd{cid, pint, pfloat, pstr, pbs}], timestamp, ans}``. These are
    *commands* in the vendor's own vocabulary -- the same word (``cids``) the
    camera uses for its ``support_cmds`` list -- not datapoints: the SDK's
    datapoint model (``DataPointCmd``, ``dp_id``, ``SkillDpconf``) is a separate
    mechanism this camera never exercises.
    """

    #: The app's "flashlight" button, and the one light control on this unit
    #: that visibly works. It is a momentary TOGGLE with no readable state:
    #: every ExecIOTCMD addressed to it flips the white light whatever the
    #: payload, and a value-less "read" flips it too -- so there is no way to
    #: ask the camera about the light without changing it. Why a value-less
    #: request actuates is unknown; that it does is observed on live hardware
    #: (2026-08-31), which is the only evidence for the toggle. The capture
    #: agrees as far as it goes: the app sent the same 00 00 00 for BOTH of its
    #: button presses (docs/decoded/light.txt:55,59) -- so those three bytes
    #: cannot be encoding a wanted state -- and the camera answered with no
    #: value field, having no state to report. See UNREADABLE_IOT_CMDS.
    FLASHLIGHT_TOGGLE = 201
    STATUS_POLL = 554  # polled at session start; meaning unknown
    NIGHT_LIGHT_SET = 588  # write NightLightMode
    NIGHT_LIGHT_GET = 589  # read NightLightMode back
    STATUS_POLL_23113 = 23113
    #: Written 01, with the did, when the app's night screen opens -- once, in
    #: the whole capture corpus (docs/decoded/night.txt:18). Never written 00,
    #: never read back, and the ack carries no value field. "Enable" is the
    #: name of the app screen it appeared on, not a proven meaning: on this
    #: unit neither direction did anything observable. See
    #: Session.enable_night_light.
    NIGHT_LIGHT_ENABLE = 23116
    STATUS_POLL_23131 = 23131


IOT_CMD_NAMES = {int(cid): cid.name for cid in IotCmd}
Datapoint = IotCmd  # deprecated name, kept for callers
DATAPOINT_NAMES = IOT_CMD_NAMES

#: What the app sends for a flashlight press. Both captured presses carried
#: exactly these three bytes (docs/decoded/light.txt:55,59). The camera ignores
#: their value, so send what the app sends rather than inventing a byte.
FLASHLIGHT_TOGGLE_PAYLOAD = b'\x00\x00\x00'

#: IoT cids that ACTUATE when read, mapped to what to do instead.
#: ``Session.iot_cmd_get`` refuses these. Reading cid 201 is not a read at all
#: -- it flips the light exactly like a write does -- so a caller that "just
#: checks the state" would silently press the button and throw the result away.
#: That is precisely what the test server's old "read state" button did.
UNREADABLE_IOT_CMDS = {
    IotCmd.FLASHLIGHT_TOGGLE: 'reading it toggles the light; use Session.toggle_flashlight()',
}
UNREADABLE_DATAPOINTS = UNREADABLE_IOT_CMDS  # deprecated name, kept for callers


#: Media formats in the AV packet's format byte (low 7 bits).
MEDIA_FORMAT_MJPEG = 4
MEDIA_FORMAT_PCM = 121

#: The A/V channel the camera streams its own video and audio on.
AV_CHANNEL = 0

#: Talk-back uplink framing, taken byte-for-byte from the vendor app's own
#: intercom capture (docs/decoded/intercom.txt + captures/intercom.pcap). The
#: uplink is NOT a mirror of the downlink -- every one of these differs:
#:
#:   downlink (camera -> us)      uplink (us -> camera)
#:   flag 9                       flag 8
#:   962 B payload, enc 960       640 B payload, enc 624
#:   "01 00" whole-frame prefix   no prefix, ciphertext starts at byte 0
#:
#: Guessing any of this wrong reboots the camera.
TALK_CHANNEL = 0
#: 320 samples = 40 ms of 8 kHz/16-bit/mono. The app sends one every ~40 ms.
TALK_FRAME_BYTES = 640
#: The app leaves the last 16 bytes of each frame unencrypted -- they decode as
#: ordinary PCM samples, so this is a quirk of its encryptor, not a trailer.
#: Reproduce it exactly: enc_length = frame - 16.
TALK_ENC_TRAILER = 16

#: Every AV payload starts with this 2-byte prefix on FT04 (whole frame per
#: packet). Fragmenting firmwares use "01 <index> 00" with index >= 1; that
#: shape is handled on read but has never been seen on this unit.
AV_PAYLOAD_PREFIX = b'\x01\x00'

#: Discriminator in the pushed-event message at field 7.4.1.
EVENT_KIND_ALARM = 66  # field 7.4.2 is the blob, 7.4.4 the camera name
EVENT_KIND_DEVICE_ONLINE = 67  # field 7.4.4 is the did

#: Byte 0 of an alarm blob. Matches skill.json ``events: [9, 12, 255]``.
EVENT_MOTION = 12
EVENT_NAMES = {9: 'event9', EVENT_MOTION: 'motion', 255: 'event255'}

#: Weekday byte values used by the motion schedule (2639/2640 field 2).
WEEKDAYS = (0, 1, 2, 3, 4, 5, 6)  # Sunday..Saturday
#: 23:59, the "all day" end-of-window the app sends.
SCHEDULE_END_ALL_DAY = 86340
