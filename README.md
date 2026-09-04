# aiopprpc

An asynchronous Python library for **local** control of PPRPC Wi-Fi cameras —
the cheap XC Things / FtyCam units (FT04, Beken BK7252; vendor apps
`com.xcthings.ftycam.google` / `.fchan`).

No cloud, no vendor app, no codec: video arrives as whole JPEGs and audio as raw
PCM. The protocol was reverse-engineered and byte-verified against real
hardware — see [FT04_COMMAND_REFERENCE.md](https://github.com/DevLn/pprpc_workspace/blob/main/docs/FT04_COMMAND_REFERENCE.md)
in the workspace repo.

```bash
pip install aiopprpc
```

Requires Python 3.10+, `aiohttp` and `pycryptodome`.

## Try it

Close the vendor app first — the camera accepts **one client at a time**.

```bash
python -m aiopprpc 192.168.0.170     # then open http://localhost:4000/
python -m aiopprpc                   # broadcast: find every camera on the LAN
```

The test server gives you the live MJPEG stream, a snapshot, hold-to-move PTZ
buttons, and selects for orientation, night vision, night light, motion
sensitivity and the status LED. A live state line shows the connection and the
video/audio frame counters, so a command that did nothing looks different from
one that worked.

**Listen** plays audio through Web Audio at roughly 100 ms latency. The raw
stream is still at `/<dev_id>/audio` as an endless WAV, but pointing a browser's
own player at it buffers for many seconds before making a sound -- which is
indistinguishable from broken.

The video stream **survives a camera reboot** without reloading the page: the
handler re-resolves the session by device id every couple of seconds, so it
picks up the new one discovery builds after the camera comes back.

## Use it

```python
import asyncio
from aiopprpc import Device, FlipMode, IRCutMode, NightLightMode

async def main():
    async with Device('192.168.0.170') as cam:
        print(cam.properties)          # ip, ssid, rssi, mac, flip, ircut, time, ...

        await cam.start_video()
        frame = await cam.get_video_frame()
        open('snapshot.jpg', 'wb').write(frame.data)

        await cam.ptz_step('left', duration=1.0)   # moves, then stops
        await cam.set_ircut(IRCutMode.AUTO)
        await cam.set_night_light(NightLightMode.FULL_COLOR)   # mode 2; label unverified
        await cam.toggle_flashlight()              # flips the white light (dp 201)
        await cam.set_flip(FlipMode.ROTATE_180)

asyncio.run(main())
```

`Device` reconnects on its own if you ask it to:

```python
cam = Device('192.168.0.170', on_event=lambda name, data: print(name, data))
cam.enable_reconnect = True
await cam.connect()
```

Every reconnect re-runs discovery, and it has to: the LanAuth credential
(`lsign`) is minted per discovery reply and expires, so a stale descriptor is
rejected. This is the single most common cause of a failed connect.

## Device support

Verified on FT04 / BK7252UQN48, firmware 3.12.13.

| Feature | API | Status |
|---|---|---|
| Discovery (UDP 20190) | `find_device`, `Discovery` | ✅ byte-verified |
| Session (LanAuth → SyncConn → heartbeats) | `Device.connect` | ✅ |
| Video — MJPEG 640×480 ~10 fps | `start_video`, `get_video_frame` | ✅ |
| Audio — PCM 8 kHz/16-bit/mono | `start_audio`, `get_audio_frame` | ✅ |
| Talk-back | `start_talk`, `send_audio`, `stop_talk` | ✅ framing matches the vendor app byte for byte |
| PTZ (up/down/left/right, continuous) | `ptz`, `ptz_stop`, `ptz_step` | ✅ |
| Orientation (upright / mirror / flip / 180°) | `get_flip`, `set_flip` | ✅ |
| Night vision, IR-cut (on / off / auto) | `get_ircut`, `set_ircut` | ✅ |
| Night-light mode | `get_night_light`, `set_night_light` | ✅ values round-trip; the mode *names* are unverified |
| Flashlight (white light) | `toggle_flashlight` | ✅ toggles — stateless, and deliberately has no getter |
| Motion sensitivity | `get/set_motion_sensitivity` | ✅ |
| Motion arming schedule | `get/set_motion_schedule` | ✅ |
| Status indicator LED | `set_status_led` | ✅ set only — no getter has been captured |
| Network info / Wi-Fi status / scan | `get_network_info`, `get_wifi_status`, `scan_wifi` | ✅ |
| Join another Wi-Fi network | `set_wifi` | 🟡 sends fine; the *outcome* is unknowable — see below |
| Clock get/set | `get_time`, `set_time` | ✅ |
| Reboot | `reboot` | ✅ |
| Generic datapoints (ExecIOTCMD) | `datapoint_get`, `datapoint_set` | ✅ |
| Pushed motion events | `on_event` callback | 🟡 see below |
| SD-card playback | — | ❌ not implemented |

### About motion events

`Device(on_event=...)` fires for camera-pushed events (`motion`,
`device_online`), and the parser matches the captured message exactly. **But**
the only motion push ever captured came over the vendor **cloud** notify
channel. Whether this camera also pushes motion on a **LAN-direct** session is
**unverified**. Wire the callback up by all means — just don't treat silence as
a bug until someone confirms it on hardware.

### About `set_wifi`

This is the one call that can make the camera unreachable, and almost nothing
about its result is knowable. It sends cleanly; what happens next is not
observable from this host.

- **A missing reply is not a failure.** A camera that obeys has to leave the
  network the session runs over, so it may never answer. `set_wifi` raises
  `CommandError` with `code is None` (a timeout) or `NotConnectedError` — both
  with `err.sent` **True**, meaning the request did go out. Treat those as
  "sent, outcome unknown". Only `err.sent == False` (nothing was transmitted) or
  a `CommandError` with a real `code` (the camera answered and refused) are
  actual failures.
- **A reply is not a success either.** WifiSet answers with an empty body that
  carries no status, and there is no read-back that could reach you afterwards.
  The only post-hoc evidence of a join is the SSID a *reconnected* session reads
  back from `get_network_info()`.
- **The evidence base is one packet.** The only WifiSet ever captured re-set the
  SSID the camera was already on, over the vendor cloud relay. Joining a
  *different* network has never been captured on any wire, and what a rejected
  WifiSet answers has never been seen.
- **`field5` is replayed verbatim** from that capture (`"359"`). Its meaning is
  unknown — see `WIFI_SET_FIELD5` in `const.py`. It is overridable because if it
  is account-scoped it will differ on another unit.

The test server's Wi-Fi panel is built around all of this: it labels its button
*send*, not *join*, never reports a timeout as an error, and watches the
reconnect to report what the camera itself says afterwards.

### Things worth knowing

- **`lsign` is single-use and expires.** Always discover immediately before
  connecting. The library does this for you inside `Device.connect()`.
- **The camera drops idle connections after ~3 seconds** and relies on you
  answering its `ConnHB` heartbeats promptly. The session answers them off a
  packet queue so a slow consumer cannot stall the link.
- **Motion sensitivity** is `0 = close (off)`, `1 = low`, `2 = medium`,
  `3 = high`, and `close` is written as an empty body. An earlier release had
  low and medium swapped; the capture only ever proved which *values* the app
  wrote, never which label was on the button.
- **The flashlight is a toggle you cannot read.** Datapoint 201 flips the white
  light for *any* message addressed to it: the payload is ignored, and a
  value-less "read" flips it too. So there is one `toggle_flashlight()` and no
  getter -- `datapoint_get()` raises `ValueError` for it (see
  `UNREADABLE_DATAPOINTS`), because a read would be a button press with the
  answer thrown away. The only way to know whether the light is on is to look at
  the picture. An earlier release called dp 201 a no-op and pointed at
  night-light `FULL_COLOR` instead; hardware says otherwise.
- **Night-light mode (dp 588/589) is a different thing** and is the only light
  setting with a read-back. Its values round-trip, but its mode *names* come from
  the order the app's buttons were pressed during the capture -- the same kind of
  evidence that once had motion low/medium swapped -- so treat
  "`FULL_COLOR` = white LED on" as unverified.
- **dp 23116 ("night light enable") does nothing observable.** The app writes
  `01` to it once when its night screen opens; no capture ever writes the off
  direction and nothing reads it back. It is kept as `enable_night_light()`, a
  probe, and is not on the test server's page.
- **A/V must be acknowledged, or the camera stops streaming.** `ConnHB(107)` is
  bidirectional: as well as answering the camera's heartbeats you must send your
  own every ~0.5 s carrying **field 4 = the last A/V sequence number received**.
  Skip it and the camera halts *all* A/V — video included — after about 152
  unacknowledged packets, while still answering heartbeats and RPCs perfectly, so
  it looks like the media path died rather than like flow control. The library
  does this for you (`Session._av_ack_loop`); `media_stats()` reports `unacked` so
  you can watch the window. Field 2 of the *reply* is a constant −30 and is not
  the acknowledgement.
- **Audio is a FIFO, video is latest-wins.** `frame_buffer` keeps only the
  newest JPEG; `audio_buffer` is a short queue that drops the *oldest* packet
  and counts the drops, because a missing 60 ms of PCM is an audible click
  rather than a skipped frame.
- **RSSI is a negative number in an unsigned varint field**, so `-60` arrives on
  the wire as `2**64 - 60`. The library converts it.
- **Talk-back is not a mirror of the downlink.** Every framing field differs,
  and getting any of them wrong reboots the camera. The uplink is 640-byte
  frames (320 samples = 40 ms) on channel 0, header flag 8, with only the first
  624 bytes encrypted and **no** `01 00` prefix, timestamped in epoch
  milliseconds, sent every 40 ms. It is also **two** commands to open, not one:
  `SpeakStart` *and* `AudioPlay`, because intercom is full duplex — skipping
  `AudioPlay` leaves the audio engine unstarted and the camera reboots a few
  hundred milliseconds in. `start_talk()`/`send_audio()`/`stop_talk()` do all of
  this for you; the framing is verified byte-for-byte against the vendor app's
  own capture.
- **Ignore `skill.json speaker: 0`.** This unit reports it and has a perfectly
  good speaker. Do not gate talk-back on `Device.has_speaker`.

- **The AES prefix is a firmware secret** that is proven for this unit and kept
  as the default. If your camera answers discovery but nothing decrypts, pass
  your own: `Device(ip, prefix='...')`.

## Layout

| Module | What it does |
|---|---|
| `const.py` | Command IDs, datapoint IDs, ports, the AES prefix |
| `types.py` | `DeviceDescriptor`, `VideoFrame`, `AudioFrame`, the value enums |
| `crypto.py` | AES key derivation and payload encryption (control + A/V) |
| `protocol.py` | Packet parse/build, TCP reassembly, the protobuf subset |
| `discover.py` | UDP discovery → `DeviceDescriptor` with a fresh `lsign` |
| `session.py` | The TCP session engine: lifecycle, media, every command |
| `device.py` | `Device` facade, `find_device`, reconnect |
| `http_server.py` | The `python -m aiopprpc` test server |

## Testing

There is no unit-test suite. Verify against real hardware:

```bash
pip install -e .
python -m aiopprpc <camera-ip>       # then open http://localhost:4000/
```

To keep a record of a run — useful when the camera drops the link and takes the
console scrollback with it:

```bash
python -m aiopprpc <camera-ip> --log-level DEBUG --log-file aiopprpc.log
```

DEBUG logs every RPC in and out and every talk-back frame, timestamped to the
millisecond, so the gap between the last thing sent and the connection dropping
is visible. The file is appended to, never truncated.

## License

Apache-2.0.
