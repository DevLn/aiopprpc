"""A small aiohttp test server: watch the stream and drive the camera from a browser.

Not part of the library's public API -- it exists so the protocol can be
exercised by hand (``python -m aiopprpc <ip>``) without Home Assistant. The
control endpoints probe the session with ``getattr`` so a camera that lacks a
capability answers 501 rather than 500.

Two things here are deliberately not the obvious implementation:

* The media handlers look the session up by ``dev_id`` on **every** iteration
  instead of capturing it once. A reboot destroys the Session object and
  discovery builds a new one under the same id a few seconds later; holding the
  old object meant the stream hung forever on a dead frame buffer and the page
  had to be reloaded by hand.
* Audio is played through Web Audio from a ``fetch`` reader rather than by
  pointing an ``<audio>`` element at the WAV stream. An endless WAV makes the
  element buffer for many seconds before it emits a sound -- which looks exactly
  like "audio is broken".
"""
import asyncio
import logging
import math
import struct
import uuid
from functools import partial

from aiohttp import web

from .const import WIFI_SET_FIELD5, describe_supported
from .exceptions import CommandError, NotConnectedError
from .types import FlipMode, IRCutMode, MotionSensitivity, NightLightMode, StatusLed

logger = logging.getLogger(__name__)

#: dev_id -> Session, filled in by __main__ as discovery finds cameras.
SESSIONS = {}

#: 1 s of 440 Hz sine at 8 kHz signed 16-bit LE -- a test tone for talk-back.
_TONE_PCM = b''.join(
    struct.pack('<h', int(8000 * math.sin(2 * math.pi * 440 * i / 8000)))
    for i in range(8000)
)

#: How long a media handler waits for a frame before checking whether the
#: session was replaced under it and writing a keepalive.
STREAM_POLL = 2.0

#: Devices whose video the operator switched off. Without this the MJPEG
#: handler restarts video the moment it is stopped, so "stop video" would not
#: stick for as long as a browser had the stream open.
VIDEO_OFF = set()

#: Selects rendered on the control page: (command, label, enum, current-property).
SETTING_SELECTS = [
    ('set-flip', 'Orientation', FlipMode, 'flip'),
    ('set-ircut', 'Night vision', IRCutMode, 'ircut'),
    ('set-motion-sensitivity', 'Motion', MotionSensitivity, 'motion_sensitivity'),
    ('set-status-led', 'Status LED', StatusLed, None),
]

#: The light controls, kept apart from the selects above because the light on
#: this camera is split across two unrelated mechanisms: a mode datapoint with
#: a read-back (588/589, below) and a stateless toggle (201, its own button).
LIGHT_SELECTS = [
    ('set-night-light', 'Night light mode', NightLightMode, 'night_light'),
]

#: After a successful set, re-read the value the camera now reports, so the
#: page shows the camera's answer instead of what we asked for. Only settings
#: with a real getter appear here -- Status LED has none, and inventing one
#: would put a number on the page that nothing verified.
SETTING_REFRESH = {
    'set-flip': ('flip', 'get_flip'),
    'set-ircut': ('ircut', 'get_ircut'),
    'set-night-light': ('night_light', 'get_night_light'),
    'set-motion-sensitivity': ('motion_sensitivity', 'get_motion_sensitivity'),
}


def _json_error(message, status):
    return web.json_response({'status': 'error', 'message': message}, status=status)


# Per-camera state for the recorded "home" preset. Single slot per dev_id;
# the page also mirrors this to localStorage so a reload doesn't lose it.
# Module-level so the dict survives across HTTP requests (each request
# creates a fresh closure for the command handlers, which would otherwise
# start every preset-save with an empty dict).
_preset_storage: dict[str, list] = {}


def _get_session(request):
    session = SESSIONS.get(request.match_info['dev_id'])
    if session is None:
        return None, _json_error('unknown device', 404)
    return session, None


async def index(request):
    cameras = ''.join(
        f'<li><a href="/camera/{dev_id}">{dev_id}</a> &mdash; {session.dev.addr}</li>'
        for dev_id, session in SESSIONS.items()
    ) or '<li><i>no cameras discovered yet</i></li>'
    return web.Response(
        text=('<!doctype html><html><head><title>PPRPC Cameras</title>'
              '<meta http-equiv="refresh" content="5"></head><body>'
              f'<h1>PPRPC Cameras</h1><ul>{cameras}</ul>'
              '<p><small>Page refreshes every 5 s as discovery finds cameras.</small></p>'
              '</body></html>'),
        headers={'content-type': 'text/html'},
    )


def _member_name(enum_cls, value):
    """Name a raw setting value without hiding it: ``Full Color (2)``."""
    try:
        return f'{enum_cls(value).name.replace("_", " ").title()} ({value})'
    except ValueError:
        return f'{value} (unknown for {enum_cls.__name__})'


def _describe_setting(session, enum_cls, prop):
    """What the camera last said this setting is, as page text."""
    if not prop:
        return 'no getter on this camera'
    current = session.dev_properties.get(prop)
    return 'not read' if current is None else _member_name(enum_cls, current)


def _render_selects(session, specs):
    html = ''
    for cmd, label, enum_cls, prop in specs:
        options = ''.join(
            f'<option value="{int(member)}">'
            f'{member.name.replace("_", " ").title()} ({int(member)})</option>'
            for member in enum_cls
        )
        # The placeholder is always what is selected, never the camera's value.
        # A <select> fires no change event when the user re-picks the option
        # already showing, so seeding it with the current value made re-applying
        # that value do nothing at all -- a control that silently ignores you.
        # The camera's own reading goes beside the select instead, where it can
        # be labelled as a reading and refreshed after every set.
        marker = f' id="cur-{prop}"' if prop else ''
        html += (f'<label>{label} '
                 f'<select onchange="setSetting(this, \'{cmd}\')">'
                 f'<option value="" selected>&mdash; choose</option>{options}'
                 f'</select></label> '
                 f'<span class="note"{marker}>camera: '
                 f'{_describe_setting(session, enum_cls, prop)}</span> ')
    return html


def _camera_page_html(dev_id, session):
    selects = _render_selects(session, SETTING_SELECTS)
    lights = _render_selects(session, LIGHT_SELECTS)

    js = '''
    <script>
    const DEV = document.location.pathname.split('/').pop();

    function setStatus(text, isError) {
        const el = document.getElementById('status');
        el.textContent = text;
        el.style.color = isError ? '#b00' : '#080';
    }

    async function sendCommand(cmd, params) {
        setStatus(`${cmd} ...`, false);
        try {
            const resp = await fetch(`/${DEV}/c/${cmd}`, {
                method: 'POST', body: JSON.stringify(params || {}),
            });
            const data = await resp.json();
            const ok = data.status === 'ok';
            setStatus(`${cmd}: ${ok ? 'ok' : data.message}`, !ok);
            refreshState();
            // Callers need the outcome: a control whose only feedback is a
            // timestamp must not print one after a 404, 501 or 500. The body
            // itself is returned, not just a boolean, because set-wifi has a
            // third outcome ("sent, no reply") that a boolean cannot carry.
            // Still truthy/falsy, so callers that only test it are unaffected.
            return ok ? data : null;
        } catch (e) {
            setStatus(`${cmd}: ${e}`, true);
            refreshState();
            return null;
        }
    }

    // PTZ moves continuously until STOP, so hold-to-move is the honest control:
    // press starts, release stops.
    function hold(dir) { recNoteMoveStart(dir); sendCommand('ptz', {direction: dir}); }
    function release() { recNoteMoveStop(); sendCommand('ptz-stop', {}); }

    // --- Position-less "home" + recorded move sequence ---
    // The FT04 has no working preset storage, so the only way to land on
    // a known vantage point is to drive the head there from (0, 0) using
    // timed moves. The page keeps the recorded sequence in localStorage
    // (per device-id) and replays it as a single API call.

    // Recording state. Each move is {dir, startedMs, endedMs}. The
    // direction is one of 'left','right','up','down'; started/ended are
    // wall-clock ms since the recording began. Dwell in each direction
    // is summed and rounded to 0.05s; the page's buttons send 'start'
    // and 'stop' messages and the user picks when to commit.
    const _recording = { active: false, startedAt: 0, moves: [], current: null };

    function recLog(msg, isErr=false) {
        const el = document.getElementById('rec-log');
        if (!el) return;
        el.textContent = (isErr ? '[err] ' : '') + msg + '\\n' + el.textContent;
    }
    function recRefresh() {
        const el = document.getElementById('rec-current');
        if (!el) return;
        if (!_recording.active) {
            el.textContent = 'idle';
        } else if (_recording.current) {
            el.textContent = `recording: ${_recording.current.dir} for ${((Date.now()-_recording.current.startedMs)/1000).toFixed(1)}s`;
        } else {
            el.textContent = `recording: ${_recording.moves.length} moves so far`;
        }
    }
    function recTickInterval() {
        if (_recording.tickHandle) return;
        _recording.tickHandle = setInterval(recRefresh, 200);
    }
    function recTickStop() {
        if (_recording.tickHandle) { clearInterval(_recording.tickHandle); _recording.tickHandle = null; }
    }

    async function homeGo() {
        // Drives the head to (0, 0). Long-running; updates the page while
        // it runs so the user knows the request didn't hang.
        const t = +document.getElementById('home-traverse').value || 18;
        recLog(`home: driving left+up for ${t}s each (about ${2*t}s total)`);
        const t0 = Date.now();
        const r = await sendCommand('home', {traverse_secs: t});
        const dt = ((Date.now()-t0)/1000).toFixed(1);
        if (!r) { recLog('home: failed -- see status line', true); return; }
        recLog(`home: returned in ${dt}s`);
    }

    async function recStart() {
        if (_recording.active) { recLog('recording already active', true); return; }
        // Make sure the head is at (0, 0) before recording -- the move
        // sequence is replayed from (0, 0), so an offset start will land
        // somewhere other than the recorded vantage point.
        recLog('recording: send "home" first if the head is not at (0, 0)');
        _recording.active = true;
        _recording.startedAt = Date.now();
        _recording.moves = [];
        _recording.current = null;
        recTickInterval();
        recRefresh();
    }
    async function recStop() {
        if (!_recording.active) { recLog('not recording', true); return; }
        // Close out the current move if any
        if (_recording.current) {
            const dur = (Date.now() - _recording.current.startedMs) / 1000;
            if (dur > 0.05) _recording.moves.push([_recording.current.dir, +dur.toFixed(2)]);
            _recording.current = null;
        }
        _recording.active = false;
        recTickStop();
        recRefresh();
        recLog(`recording: stopped, ${_recording.moves.length} moves captured`);
    }
    function recNoteMoveStart(dir) {
        if (!_recording.active) return;
        // Close the previous move
        if (_recording.current) {
            const dur = (Date.now() - _recording.current.startedMs) / 1000;
            if (dur > 0.05) _recording.moves.push([_recording.current.dir, +dur.toFixed(2)]);
        }
        _recording.current = {dir, startedMs: Date.now()};
        recRefresh();
    }
    function recNoteMoveStop() {
        if (!_recording.active || !_recording.current) return;
        const dur = (Date.now() - _recording.current.startedMs) / 1000;
        if (dur > 0.05) _recording.moves.push([_recording.current.dir, +dur.toFixed(2)]);
        _recording.current = null;
        recRefresh();
    }
    async function recSave() {
        if (_recording.active) { recLog('stop recording before saving', true); return; }
        const r = await sendCommand('preset-save', {sequence: JSON.stringify(_recording.moves)});
        if (!r) return;
        recLog(`saved ${r.saved} moves to server`);
        // Mirror to localStorage so the sequence survives a page reload
        try { localStorage.setItem('pprpc-preset-' + DEV, JSON.stringify(_recording.moves)); } catch (e) {}
    }
    async function recLoad() {
        // Pull the server's saved sequence (if any) into the page.
        const r = await sendCommand('preset-get', {});
        if (!r) return;
        // Normalise: server sends [dir, dur] arrays (Python tuples JSON-serialised
        // as arrays). Old localStorage may have {dir, dur} objects. Both become
        // [dir, dur] pairs.
        _recording.moves = (r.sequence || []).map(m =>
            Array.isArray(m) ? m : [m.dir, m.dur]
        );
        recLog(`loaded ${_recording.moves.length} moves from server`);
        recRefresh();
    }
    async function recReplay() {
        if (_recording.moves.length === 0) { recLog('no moves to replay', true); return; }
        const settle = +document.getElementById('rec-settle').value || 0.4;
        // m is [dir, dur] from the server; dir is a string, dur is a number.
        const total = _recording.moves.reduce((s, m) => s + m[1], 0);
        recLog(`replaying ${_recording.moves.length} moves (${total.toFixed(1)}s of moves + settle=${settle}s)`);
        const t0 = Date.now();
        const r = await sendCommand('recall-preset', {
            sequence: JSON.stringify(_recording.moves), settle_secs: settle,
        });
        const dt = ((Date.now()-t0)/1000).toFixed(1);
        if (!r) { recLog('replay: failed -- see status line', true); return; }
        recLog(`replay: returned in ${dt}s`);
    }
    function recClear() {
        _recording.moves = [];
        _recording.current = null;
        recRefresh();
        recLog('cleared recorded moves');
    }
    // Try to load any locally-saved sequence on first paint so the user
    // doesn't have to re-record after a reload.
    document.addEventListener('DOMContentLoaded', () => {
        try {
            const raw = localStorage.getItem('pprpc-preset-' + DEV);
            if (raw) {
                const parsed = JSON.parse(raw);
                // Normalise old {dir,dur} objects and new [dir,dur] pairs to [dir,dur].
                _recording.moves = parsed.map(m =>
                    Array.isArray(m) ? m : [m.dir, m.dur]
                );
                recLog(`restored ${_recording.moves.length} moves from local cache`);
            }
        } catch (e) {}
    });

    // The select always sits on its placeholder, so picking the same option
    // twice sends the command twice instead of silently doing nothing the
    // second time. The placeholder itself is not a value: +"" is 0, which is a
    // real enum member for every setting here, so it has to be filtered out
    // before it becomes a command.
    //
    // The send is debounced, and the reset to the placeholder happens only
    // AFTER it. A closed <select> fires change on every arrow key, so resetting
    // inside the handler snapped the keyboard cursor back to the top on each
    // press: the selection could never move past the first option, and arrowing
    // toward "High" silently wrote "Close". Waiting instead collapses a run of
    // key presses into one write, at the option the user stopped on.
    const settingTimers = {};
    function setSetting(el, cmd) {
        clearTimeout(settingTimers[cmd]);
        const value = el.value;
        if (value === '') return;
        settingTimers[cmd] = setTimeout(async () => {
            await sendCommand(cmd, {value: +value});
            el.selectedIndex = 0;
        }, 350);
    }

    // The flashlight has no readable state, so the only honest feedback is
    // "the press reached the camera" and a reminder to look at the picture.
    async function flashlight() {
        const ok = await sendCommand('toggle-flashlight', {});
        const at = new Date().toLocaleTimeString();
        const el = document.getElementById('fl-log');
        // "Failed" cannot mean "the light did not change": send_command puts the
        // packet on the wire and only then waits for the ack, so a timeout is
        // exactly the case where the camera most likely DID flip the light.
        el.textContent = ok
            ? `toggled at ${at} \\u2014 on or off is unknowable, watch the picture`
            : `toggle NOT CONFIRMED at ${at} \\u2014 the camera did not answer; it may`
              + ` or may not have flipped, watch the picture`;
        el.style.color = ok ? '#444' : '#b00';
    }

    async function load(path, label) {
        const el = document.getElementById('dump');
        el.textContent = `${label}: reading...`;
        try {
            const resp = await fetch(`/${DEV}/${path}`);
            el.textContent = `${label}:\\n` + JSON.stringify(await resp.json(), null, 2);
        } catch (e) {
            el.textContent = `${label} failed: ${e}`;
        }
    }

    // --- live state -------------------------------------------------------
    // Without this the stream buttons look inert: "start audio" only tells the
    // camera to send audio, and nothing on the page moved to show it worked.
    // The frame counters are the proof that packets are arriving.
    async function refreshState() {
        try {
            const s = await (await fetch(`/${DEV}/state`)).json();
            const m = s.media || {};
            const bits = [
                s.connected ? 'connected' : `${s.state} (reconnecting)`,
                `video ${s.video ? 'on' : 'off'} / ${s.video_frames} frames`
                    + (m.video_idle_s > 2 ? ` (idle ${m.video_idle_s}s)` : ''),
                `audio ${s.audio ? 'on' : 'off'} / ${s.audio_frames} frames`
                    + (m.audio_idle_s > 2 ? ` (idle ${m.audio_idle_s}s)` : ''),
            ];
            // recv_idle separates "the camera went quiet" from "we stopped
            // parsing what it is still sending" -- the two look identical
            // from the outside and need opposite fixes.
            if (m.recv_idle_s > 2) bits.push(`socket idle ${m.recv_idle_s}s`);
            if (m.undecodable) bits.push(`${m.undecodable} undecodable`);
            if (s.talking) bits.push('TALKING');
            if (s.audio_dropped) bits.push(`${s.audio_dropped} audio frames dropped`);
            const el = document.getElementById('state');
            el.textContent = bits.join(' \\u00b7 ');
            el.style.color = s.connected ? '#444' : '#b00';
            // Settings are re-read from the camera after every successful set,
            // so these move on their own. Without this the selects would keep
            // showing the value read at connect and quietly go stale.
            for (const [prop, text] of Object.entries(s.settings || {})) {
                const cur = document.getElementById(`cur-${prop}`);
                if (cur) cur.textContent = `camera: ${text}`;
            }
            // textContent, never interpolated into the page HTML: an SSID is
            // arbitrary text from the network and this file has no escaper.
            const was = wifiNow;
            wifiNow = (s.network || {}).ssid || null;
            const nowEl = document.getElementById('wifi-now');
            if (nowEl) {
                nowEl.textContent = wifiNow
                    ? `camera reports it is on "${wifiNow}" at ${(s.network || {}).ip || '?'}`
                    : 'the camera has not reported which network it is on';
            }
            // Only on a change: this poll runs every 2 s and wifiEcho() disarms
            // the send button, so calling it unconditionally would make the
            // control impossible to arm.
            if (was !== wifiNow) wifiEcho();
            wifiTick(s);
        } catch (e) { /* the server itself is gone; the next tick will say so */ }
    }

    async function readClock() {
        const el = document.getElementById('clock');
        el.textContent = 'reading clock...';
        try {
            const t = await (await fetch(`/${DEV}/time`)).json();
            if (t.status !== 'ok') { el.textContent = `clock: ${t.message}`; return; }
            const drift = t.drift_s > 0 ? `+${t.drift_s}` : `${t.drift_s}`;
            el.textContent = `camera clock ${t.local} (UTC${t.utc_offset}) `
                           + `\\u2014 ${drift}s vs this host`;
            el.style.color = Math.abs(t.drift_s) > 60 ? '#b00' : '#444';
        } catch (e) { el.textContent = `clock failed: ${e}`; }
    }

    // --- Wi-Fi ------------------------------------------------------------
    // The only control here that can make the camera unreachable, and the only
    // one whose success is invisible: a camera that obeys has to leave this
    // network, so it may never answer. Hence the arming step, and hence this is
    // the one caller that reads the response body -- "sent, no reply" is a
    // third outcome, and folding it into ok/failed would be a lie in the
    // direction that matters.
    let wifiNow = null;    // the SSID the camera itself last reported
    let wifiWatch = null;  // an attempt whose aftermath is being watched

    function wifiFields() {
        // Exactly the parameter names of the server-side set_wifi wrapper: the
        // dispatcher calls handler(**params).
        return {
            ssid: document.getElementById('wifi-ssid').value,
            password: document.getElementById('wifi-pass').value,
            field5: document.getElementById('wifi-field5').value,
        };
    }

    function wifiWarnings(f) {
        const w = [];
        if (f.ssid !== f.ssid.trim()) w.push('the SSID starts or ends with whitespace');
        if (/[^\\x20-\\x7e]/.test(f.ssid)) w.push('the SSID contains non-ASCII characters');
        // An unknown current network is the LEAST safe state, so it must not be
        // the quietest one: without this the guardrail simply vanishes.
        if (wifiNow === null) {
            w.push('the camera has not reported its current network, so nothing here'
                 + ' can tell a move from a re-set');
        } else if (f.ssid && f.ssid !== wifiNow) {
            w.push(`this MOVES the camera off "${wifiNow}" -- the case no capture covers`);
        }
        if (!f.password) w.push('no password: only an open network can accept this');
        else if (f.password.length < 8) {
            w.push(`${f.password.length}-character password: WPA/WPA2 needs at least 8`);
        }
        if (!f.field5) w.push('field 5 empty: it is left out of the packet, a shape never seen');
        return w;
    }

    function wifiEcho() {
        // Any edit revokes the arming, or a payload that was reviewed and
        // accepted could be changed while the button stayed live.
        const arm = document.getElementById('wifi-arm');
        if (arm.checked) { arm.checked = false; wifiArm(); }
        const f = wifiFields();
        const warn = wifiWarnings(f);
        const el = document.getElementById('wifi-echo');
        // Quoted, with a character count. A trailing space, a case error or a
        // homoglyph is invisible in an <input> and is the failure class that
        // actually strands cameras; a confirm() dialog cannot show it either.
        el.textContent = `will send ssid ${JSON.stringify(f.ssid)} (${f.ssid.length} chars),`
            + ` password ${f.password.length} chars, field 5 ${JSON.stringify(f.field5)}`
            + warn.map(x => `\\n! ${x}`).join('');
        el.style.color = warn.length ? '#b00' : '#444';
    }

    function wifiArm() {
        document.getElementById('wifi-btn').disabled =
            !document.getElementById('wifi-arm').checked;
    }

    function wifiShow(el) {
        // An anti-typo control, not a convenience: typing a WPA key blind into
        // the box that strands the camera is the stated failure mode.
        document.getElementById('wifi-pass').type = el.checked ? 'text' : 'password';
    }

    function wifiUseCurrent() {
        if (wifiNow === null) {
            wifiLog('nothing to copy: the camera has not reported a current network', true);
            return;
        }
        document.getElementById('wifi-ssid').value = wifiNow;
        wifiEcho();
    }

    function wifiLog(line, isError) {
        const el = document.getElementById('wifi-log');
        el.textContent += `\\n${new Date().toLocaleTimeString()}  ${line}`;
        if (isError) el.style.color = '#b00';
    }

    async function setWifi() {
        const f = wifiFields();
        if (!f.ssid.trim()) { wifiLog('nothing sent: the SSID is empty', true); return; }
        const warn = wifiWarnings(f);
        const lines = [
            'Send WifiSet (2602) to the camera?',
            '',
            `  ssid      ${JSON.stringify(f.ssid)}  (${f.ssid.length} chars)`,
            `  password  ${f.password.length} chars`,
            `  field 5   ${JSON.stringify(f.field5)}`,
            '',
        ];
        if (warn.length) lines.push('Warnings:', ...warn.map(x => `  - ${x}`), '');
        lines.push(
            'A camera that acts on this has to leave the network it is on. Nothing',
            'captured shows whether it acts at once, at reboot, or at all -- but if',
            'the name or password is wrong it may end up joined to nothing, and then',
            'this host cannot reach it again and the recovery is physical.',
            '',
            'The one WifiSet ever captured answered with an empty body; what a',
            'rejected one answers has never been seen. So neither a reply nor a',
            'silence here can confirm or deny a join.');
        if (!confirm(lines.join('\\n'))) { wifiLog('cancelled, nothing sent'); return; }

        // Disarm before sending, and leave the button disabled on every path:
        // re-sending requires deliberately re-ticking the box.
        document.getElementById('wifi-arm').checked = false;
        document.getElementById('wifi-btn').disabled = true;
        wifiLog(`sending WifiSet: ssid ${JSON.stringify(f.ssid)},`
              + ` password ${f.password.length} chars, field 5 ${JSON.stringify(f.field5)}`);
        const data = await sendCommand('set-wifi', f);
        if (!data) {
            // Two different 500s land here and they are opposites: "not
            // connected" means nothing was transmitted, while a response code
            // means the camera received it and declined. Claiming "nothing
            // reached the camera" for both was false in the second case, so
            // this points at the status line rather than asserting either.
            wifiLog('not accepted -- the status line above carries the reason. If it names a'
                  + ' response code the camera received the request and refused it; if it'
                  + ' says not connected, nothing was transmitted.', true);
            return;
        }
        if (data.ack) {
            // "received", not "accepted": this library is LanAuth-only, so the
            // reply crossed a socket that terminates at the camera. That is all
            // it proves -- the body is empty and carries no status.
            wifiLog('the camera answered over this socket, so it received the request.'
                  + ' The reply is empty, so it says nothing about whether it joined.');
        } else {
            wifiLog(`no reply (${data.detail}). That is what a camera that left this`
                  + ' network immediately looks like, and also what one that never acted'
                  + ' looks like.');
        }
        wifiWatch = {want: f.ssid, was: wifiNow, since: Date.now(), dropped: false, done: false};
    }

    // The aftermath. When a lost camera is rediscovered the supervisor builds a
    // NEW Session whose setup re-reads NetInfo, so /state then carries the
    // camera's own fresh answer about which network it is on. That comparison
    // is the only evidence of a join that exists anywhere on this page.
    function wifiTick(s) {
        if (!wifiWatch || wifiWatch.done) return;
        // Note the deliberate lack of an early return while disconnected: the
        // "gone for two minutes" deadline below is only reachable in that
        // state, so returning here made it dead code.
        if (!s.connected) wifiWatch.dropped = true;
        const now = s.connected ? (s.network || {}).ssid : null;
        if (wifiWatch.dropped && now) {
            wifiWatch.done = true;
            if (wifiWatch.was === null) {
                // No "before" to compare against, so a matching name is not
                // evidence of anything: without this the null case fell through
                // to the join branch and claimed a join for a plain re-set.
                wifiLog(`the camera is back and reports "${now}", but it never said which`
                      + ' network it was on before, so this cannot tell a join from a name'
                      + ' that never changed.');
            } else if (now !== wifiWatch.want) {
                // Checked BEFORE the same-name case: with want === was, a
                // camera that came back on some third network would otherwise
                // have been described as "the name it was already on".
                wifiLog(`the camera is back but reports "${now}", not`
                      + ` ${JSON.stringify(wifiWatch.want)}. The join did not take.`, true);
            } else if (wifiWatch.want === wifiWatch.was) {
                // The camera was already on this name, so a match proves
                // nothing -- it would read the same if the command did nothing.
                wifiLog(`the camera is back and reports "${now}", the name it was already`
                      + ' on. That settles nothing about whether the command did anything.');
            } else {
                wifiLog(`the camera is back and its own NetInfo now reports "${now}".`
                      + ' That is the join, reported by the camera.');
            }
            return;
        }
        const secs = Math.round((Date.now() - wifiWatch.since) / 1000);
        if (!wifiWatch.dropped && secs >= 45) {
            wifiWatch.done = true;
            wifiLog('45s and this session was never interrupted, so the camera has not left'
                  + ' this network so far. It may still apply the change at its next reboot,'
                  + ' and a re-association shorter than the 15s link timeout would not be'
                  + ' visible from here.');
        } else if (wifiWatch.dropped && secs >= 120) {
            wifiWatch.done = true;
            if (s.connected) {
                // Reachable whenever the camera is back but never told us an
                // SSID. Saying "not rediscovered" here would contradict the
                // status line directly above, which reads "connected".
                wifiLog('the camera dropped and came back, but it has not reported which'
                      + ' network it is on, so there is nothing here to compare against.'
                      + ' The settings dump reads it live.');
            } else {
                wifiLog('two minutes and the camera has not been rediscovered. A successful'
                      + ' move to a network this host cannot see and a failed join are the'
                      + ' same silence from here.', true);
            }
        }
    }

    // --- low-latency audio ------------------------------------------------
    // Reads the WAV stream with fetch() and schedules the raw PCM through Web
    // Audio, ~100 ms behind live. Pointing an <audio> element at the same URL
    // buffers for many seconds first and looks like it has hung.
    let audioCtx = null, audioAbort = null;

    function stopListen() {
        if (audioAbort) { audioAbort.abort(); audioAbort = null; }
        if (audioCtx) { audioCtx.close(); audioCtx = null; }
        document.getElementById('listen-btn').textContent = 'listen';
        setStatus('listen: stopped', false);
    }

    async function toggleListen() {
        if (audioCtx) { stopListen(); return; }
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        audioAbort = new AbortController();
        document.getElementById('listen-btn').textContent = 'stop listening';
        const rate = audioCtx.sampleRate;  // resample: 8 kHz is below the
        let playhead = 0;                  // minimum some browsers will accept
        let pending = new Uint8Array(0);
        let skip = 44;                     // the WAV header
        try {
            const resp = await fetch(`/${DEV}/audio`, {signal: audioAbort.signal});
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const reader = resp.body.getReader();
            setStatus('listen: playing', false);
            while (audioCtx) {
                const {done, value} = await reader.read();
                if (done) break;
                let buf = value;
                if (skip > 0) {
                    const n = Math.min(skip, buf.length);
                    buf = buf.subarray(n);
                    skip -= n;
                    if (!buf.length) continue;
                }
                const merged = new Uint8Array(pending.length + buf.length);
                merged.set(pending);
                merged.set(buf, pending.length);
                // A chunk can split a 16-bit sample; carry the odd byte over.
                const even = merged.length & ~1;
                pending = merged.slice(even);
                if (!even) continue;
                const samples = new Int16Array(merged.buffer, 0, even / 2);
                const ratio = rate / 8000;
                const out = audioCtx.createBuffer(1, Math.round(samples.length * ratio), rate);
                const ch = out.getChannelData(0);
                for (let i = 0; i < ch.length; i++) {
                    ch[i] = samples[Math.min(samples.length - 1, (i / ratio) | 0)] / 32768;
                }
                const src = audioCtx.createBufferSource();
                src.buffer = out;
                src.connect(audioCtx.destination);
                const now = audioCtx.currentTime;
                if (playhead < now + 0.1) playhead = now + 0.1;  // fell behind: jump
                src.start(playhead);
                playhead += out.duration;
            }
        } catch (e) {
            if (e.name !== 'AbortError') {
                setStatus(`listen: ${e}`, true);
                stopListen();
            }
        }
    }

    // The MJPEG response survives a camera reboot server-side, so this only
    // fires if the HTTP connection itself broke.
    function videoBroken(img) {
        setTimeout(() => { img.src = `/${DEV}/v?t=${Date.now()}`; }, 2000);
    }

    window.addEventListener('load', () => {
        refreshState();
        readClock();
        setInterval(refreshState, 2000);
    });
    </script>
    '''
    return f'''<!doctype html><html><head><meta charset="utf-8">
<title>{dev_id}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 1rem; background: #f6f6f6; }}
 img {{ max-width: 100%; background: #111; }}
 button {{ min-width: 3rem; padding: .4rem .6rem; margin: .1rem; }}
 fieldset {{ margin: .6rem 0; }}
 pre {{ background: #fff; padding: .6rem; overflow-x: auto; }}
 .pad {{ display: inline-grid; grid-template-columns: repeat(3, 3.4rem); gap: .1rem; }}
 .pad .mid {{ grid-column: 2; }}
 #state, #clock {{ color: #444; font-size: .9rem; margin: .2rem 0; }}
 .note {{ color: #666; font-size: .85rem; }}
 #wifi-echo, #wifi-log {{ display: block; white-space: pre-line; margin: .3rem 0; }}
</style>{js}</head><body>
<h1>{dev_id} <small>{session.dev.addr}</small></h1>
<p id="status">ready</p>
<p id="state">&hellip;</p>
<p id="clock">&hellip; <button onclick="readClock()">re-read</button>
 <button onclick="sendCommand('sync-time', {{}}).then(readClock)">set from this host</button></p>
<img src="/{dev_id}/v" alt="live stream" onerror="videoBroken(this)">
<fieldset><legend>Pan / tilt (hold to move)</legend>
 <div class="pad">
  <span></span>
  <button class="mid" onmousedown="hold('up')" onmouseup="release()"
          onmouseleave="release()">&uarr;</button>
  <span></span>
  <button onmousedown="hold('left')" onmouseup="release()" onmouseleave="release()">&larr;</button>
  <button onclick="sendCommand('ptz-stop', {{}})">stop</button>
  <button onmousedown="hold('right')" onmouseup="release()" onmouseleave="release()">&rarr;</button>
  <span></span>
  <button class="mid" onmousedown="hold('down')" onmouseup="release()"
          onmouseleave="release()">&darr;</button>
 </div>
</fieldset>
<!--
  Preset research summary (unsuccessful):
  - FT04 firmware does not implement the new SDK's Psp* (BEGIN/END/CALL/DELETE_CRUISE_SET)
    or Timedcruise* commands. cmdid sweep of 2665-2700 and 2721-2722 with 5 body shapes
    each: all returned either non-zero codes, empty bodies, or timeouts. The camera's
    skill.json reports mconf.psp:0, cruise:0, confirming the firmware has no preset
    storage. cmdids for PspAdd/Call/Del/List live in the gomobile binary as raw ARM64
    integers and were not extractable from static analysis. Workaround: see the
    "Recorded preset" section below.
-->
<fieldset><legend>Recorded preset (position-less workaround)</legend>
 <p class="note">The FT04 has no preset storage. The head always boots to (0, 0)
  (full-left, full-up). To return to a saved vantage point: <b>1)</b> drive
  to (0, 0) with "go home", then <b>2)</b> replay a recorded sequence of
  timed moves. Each sequence is a list of <code>[direction, seconds]</code>
  pairs stored server-side (per camera) and in <code>localStorage</code>.</p>
 <fieldset style="border: 1px solid #ccc; margin: .4rem 0;">
  <legend>1. drive to (0, 0)</legend>
  <label>seconds per direction:
   <input id="home-traverse" type="number" min="1" max="60"
          value="18" size="4"> (default 18 s; full sweep ~12-15 s)</label>
  <button onclick="homeGo()">go home</button>
  <span id="rec-current" class="note">idle</span>
 </fieldset>
 <fieldset style="border: 1px solid #ccc; margin: .4rem 0;">
  <legend>2. record (use Pan / tilt buttons above)</legend>
  <button onclick="recStart()">start recording</button>
  <button onclick="recStop()">stop recording</button>
  <button onclick="recClear()">clear</button>
  <span class="note">Head must be at (0, 0) before recording. Each PTZ hold/release
   becomes one <code>[dir, seconds]</code> entry. Reloaded from localStorage
   on page paint.</span>
 </fieldset>
 <fieldset style="border: 1px solid #ccc; margin: .4rem 0;">
  <legend>3. save / load / replay</legend>
  <button onclick="recSave()">save (server + browser)</button>
  <button onclick="recLoad()">load from server</button>
  <label>settle:
   <input id="rec-settle" type="number" min="0" max="2"
          step="0.1" value="0.4" size="4"> s</label>
  <button onclick="recReplay()">replay</button>
  <span class="note">Server holds one sequence per camera; browser one per localStorage key.
   Replay assumes head is at (0, 0).</span>
 </fieldset>
 <pre id="rec-log" style="max-height: 6rem; overflow-y: auto; font-size: .8rem; background: #f9f9f9; padding: .3rem;">no preset actions yet</pre>
</fieldset>
<fieldset><legend>Settings</legend>{selects}</fieldset>
<fieldset><legend>Light</legend>
 <button onclick="flashlight()">toggle flashlight</button>
 <span class="note" id="fl-log">datapoint 201 &mdash; press it and watch the picture</span>
 <br>
 {lights}
 <p class="note"><b>Flashlight</b> (dp 201) is a momentary <b>toggle</b>, not an
  on/off: the camera flips the light for any message sent to that datapoint and
  ignores the payload. It has <b>no readable state</b> &mdash; a "read" of dp 201
  flips it too, which is why there is no read-state button here and no getter in
  the library. The only way to know whether it is on is to look at the picture.
  <br><b>Night light mode</b> (dp 588) is separate, and is the one light setting
  the camera reads back (dp 589). The mode <i>names</i> come from the order the
  vendor app's buttons were pressed during the capture, not from the bytes, so
  treat "Full Color = white LED on" as unverified &mdash; that is the same kind of
  evidence that once had motion Low and Medium swapped.
  <br>The app's dp 23116 ("night light on/off") is not on this page: it is written
  once by the app when its night screen opens, no capture ever writes the off
  direction, nothing reads it back, and neither direction did anything observable
  here. It survives in the library as <code>enable_night_light()</code>, a probe.</p>
</fieldset>
<fieldset><legend>Stream</legend>
 <button onclick="sendCommand('start-video', {{}})">start video</button>
 <button onclick="sendCommand('stop-video', {{}})">stop video</button>
 <button id="listen-btn" onclick="toggleListen()">listen</button>
 <button onclick="sendCommand('start-audio', {{}})">start audio</button>
 <button onclick="sendCommand('stop-audio', {{}})">stop audio</button>
 <button onclick="sendCommand('talk-test', {{}})">talk test tone</button>
 <a href="/{dev_id}/snapshot" target="_blank"><button>snapshot</button></a>
 <p class="note"><b>listen</b> plays through Web Audio at about 100&nbsp;ms latency;
  it starts the camera's audio stream itself. <b>start/stop audio</b> only drive the
  protocol commands &mdash; watch the frame counter above to see them take effect.
  The raw WAV stream is at <code>/{dev_id}/audio</code>, but a browser's own player
  buffers an endless WAV for a long time before making a sound.</p>
</fieldset>
<fieldset><legend>System</legend>
 <button onclick="sendCommand('sync-time', {{}})">sync clock</button>
 <button onclick="if (confirm('Reboot the camera?')) sendCommand('reboot', {{}})">reboot</button>
 <button onclick="load('info', 'device info')">info</button>
 <button onclick="load('params', 'live settings')">settings</button>
 <button onclick="load('wifi-scan', 'wifi scan')">wifi scan</button>
 <button onclick="load('state', 'live stats')">stats</button>
</fieldset>
<fieldset><legend>Wi-Fi &mdash; the one control that can strand the camera</legend>
 <span class="note" id="wifi-now">&hellip;</span>
 <br>
 <label>SSID <input id="wifi-ssid" size="20" autocomplete="off" spellcheck="false"
        oninput="wifiEcho()"></label>
 <button onclick="wifiUseCurrent()">use current</button>
 <label>password <input id="wifi-pass" type="password" size="20" autocomplete="off"
        oninput="wifiEcho()"></label>
 <label><input type="checkbox" onchange="wifiShow(this)"> show it</label>
 <br>
 <!-- Its own line: next to the "show" checkbox this read as one control
      called "show field 5", which is neither of the two things it is. -->
 <label>field 5 <input id="wifi-field5" size="6" value="{WIFI_SET_FIELD5}"
        oninput="wifiEcho()"></label>
 <span class="note" id="wifi-echo">nothing typed yet</span>
 <label><input type="checkbox" id="wifi-arm" onchange="wifiArm()">
  I accept that the camera may become unreachable</label>
 <button id="wifi-btn" disabled onclick="setWifi()">send WifiSet</button>
 <span class="note" id="wifi-log">nothing sent this session.</span>
 <p class="note"><b>WifiSet</b> (2602) sends the SSID, the password and one more
  string as field&nbsp;5. It is the only control on this page that can make the
  camera unreachable: a wrong name or password can leave it joined to nothing,
  and there is no local way back &mdash; the recovery is physical. The button is
  labelled <i>send</i>, not <i>join</i>, because sending is the only part of this
  that the page ever performs.
  <br>Nothing here can tell you whether a join worked. The camera answers WifiSet
  with an empty <code>{{}}</code> and there is no read-back that could reach us
  afterwards, so a reply proves only that the request arrived &mdash; and a
  <i>missing</i> reply proves nothing at all, because a camera that obeyed
  instantly and a camera that ignored the command both fall silent. A timeout
  here is therefore <b>not</b> reported as a failure. What the log above does
  instead is watch this session: whether it drops, whether the camera is
  rediscovered, and what SSID its fresh NetInfo reports if it is. That last one
  is the only evidence of a join that exists.
  <br>Field&nbsp;5 carried the literal <code>359</code> in the one WifiSet ever
  captured and is replayed verbatim as the only value ever seen on that field.
  It is most likely <b>the account's country dialling code</b>: +359 is Bulgaria,
  where this device's owner lives, every capture sets the clock to Europe/Sofia,
  and the same <code>359</code> prefixes the account's cloud notify topic
  (<code>/xc/2u/notify/359-&lt;32 hex&gt;</code>) whose hex half is <i>not</i>
  derived from the device &mdash; so that topic reads as
  <code>&lt;country&gt;-&lt;account&nbsp;hash&gt;</code>. That makes it an
  <i>account</i> attribute rather than a radio one: don't change it hoping to
  affect channels or transmit power, not least because real Wi-Fi country codes
  are <code>BG</code>/<code>100</code>, never the dialling code. The camera never
  sends the value back, so whether it reads the field at all is still unknown
  &mdash; and the reference doc's old claim that it is &ldquo;the leading digits
  of the device id&rdquo; is refuted outright by this camera's id
  <code>PPFT04049C9EEFF92B</code>. Emptying the box drops the field from the
  packet altogether, a shape never seen on the wire.
  <br>The one captured WifiSet re-set the network the camera was <i>already</i>
  on, and it went over the vendor's cloud relay rather than a LAN session.
  Joining a <i>different</i> network has never been captured on any wire.
  Re-sending the current SSID is the only part of this control with evidence
  behind it and the safe way to exercise it &mdash; that is what <b>use
  current</b> is for, though a match afterwards proves nothing precisely because
  the name never changed.</p>
</fieldset>
<pre id="dump"></pre>
</body></html>'''


async def camera_page(request):
    session, err = _get_session(request)
    if err:
        return err
    return web.Response(text=_camera_page_html(request.match_info['dev_id'], session),
                        headers={'content-type': 'text/html'})


async def handle_commands(request):
    session, err = _get_session(request)
    if err:
        return err
    cmd = request.match_info['cmd']
    try:
        params = await request.json()
    except ValueError:
        params = {}

    async def talk_test(**kwargs):
        # 1 s tone, one camera-sized frame at a time, paced in real time. Frame
        # size and cadence come from SpeakStart rather than being assumed --
        # sending double-sized frames here is what rebooted the camera.
        spec = await session.start_talk()
        frame = spec['frame_bytes']
        try:
            for offset in range(0, len(_TONE_PCM), frame):
                await session.send_audio(_TONE_PCM[offset:offset + frame])
                await asyncio.sleep(spec['frame_ms'] / 1000)
        finally:
            await session.stop_talk()

    async def ptz(direction='up', duration=None, **kwargs):
        if duration:
            await session.ptz_step(direction, float(duration))
        else:
            await session.ptz(direction)

    async def setting(method, value, **kwargs):
        await method(int(value))

    async def toggle_flashlight(**kwargs):
        # No parameters on purpose: dp 201 ignores its payload, so offering a
        # value here would be four controls that all do the same thing.
        await session.toggle_flashlight()

    async def set_wifi(ssid='', password='', field5=WIFI_SET_FIELD5, **kwargs):
        """WifiSet, with the three outcomes kept apart.

        The bare ``getattr(session, 'set_wifi')`` this replaces was wrong in
        three ways. Session.set_wifi takes no **kwargs, so a stray JSON key was
        a TypeError 500 that read like a camera fault; pb_string calls
        .encode(), so a JSON number was an AttributeError 500; and worst, it
        awaits a reply, so the *expected* outcome of the command working --
        the camera leaving this network before it can answer -- surfaced as a
        red 500 saying the command failed.

        So: 500 only when the camera was never told anything (nothing sent) or
        actively refused (a real response code). A missing reply is reported as
        ack=False, which the page renders as "sent, outcome unknown".
        """
        # A JSON null is the encoding of "no value", not a value: str() would
        # turn it into the literal four-character SSID "None", which is legal
        # and sails straight past the empty check below. Numbers are still
        # coerced on purpose -- {"field5": 359} is a value someone supplied.
        # (field5=None is fine and means "omit field 5", per Session.set_wifi.)
        if ssid is None or password is None:
            raise ValueError('refusing to send: ssid and password must be strings, not null')
        ssid = str(ssid)
        if not ssid.strip():
            raise ValueError('refusing to send: the SSID is empty')
        # The only durable record that this was ever sent: the page's log is a
        # <span> that a reload wipes. Never the password itself.
        logger.warning('WifiSet requested for %s: ssid=%r field5=%r password=%d chars',
                       request.match_info['dev_id'], ssid, field5, len(str(password)))
        try:
            await session.set_wifi(ssid, password, field5)
        except (CommandError, NotConnectedError) as err:
            # A camera that obeyed instantly cannot answer -- but neither can
            # one that was never reached, and both used to be NotConnectedError.
            # PPRPCError.sent is what separates them.
            if not getattr(err, 'sent', False):
                raise
            if isinstance(err, CommandError) and err.code is not None:
                raise  # the camera answered with a code: a real refusal
            return {'ack': False, 'detail': f'{type(err).__name__}: {err}'}
        return {'ack': True}

    async def home(traverse_secs=18, **kwargs):
        """Drive the PTZ head to the (0, 0) corner via timed moves."""
        t = max(1.0, float(traverse_secs))
        await session.home(t)

    async def recall_preset(sequence, settle_secs=0.4, **kwargs):
        """Replay a list of (direction, duration) moves from home.

        ``sequence`` arrives from the page as a JSON string like
        ``[["right",1.2],["down",0.4]]``. Decoding happens here.
        """
        import json as _json
        if isinstance(sequence, str):
            sequence = _json.loads(sequence)
        moves = [(str(d), float(t)) for d, t in sequence]
        await session.recall_preset(moves, settle_secs=float(settle_secs))

    # Per-camera state for the recorded preset. The page loads/saves it as
    # a JSON blob; the server stores it keyed on dev_id and reuses it for
    # the next replay. Nothing fancier needed -- it's a single slot.
    # NOTE: defined at module level (below) so it persists across requests.

    async def preset_get(**kwargs):
        dev_id = request.match_info['dev_id']
        return {'sequence': _preset_storage.get(dev_id, [])}

    async def preset_save(sequence, **kwargs):
        """Save a recorded move sequence for the camera."""
        import json as _json
        if isinstance(sequence, str):
            sequence = _json.loads(sequence)
        moves = [(str(d), float(t)) for d, t in sequence]
        dev_id = request.match_info['dev_id']
        _preset_storage[dev_id] = moves
        return {'saved': len(moves), 'sequence': moves}

    async def stop_video(**kwargs):
        VIDEO_OFF.add(request.match_info['dev_id'])
        await session.stop_video()

    async def start_video(**kwargs):
        VIDEO_OFF.discard(request.match_info['dev_id'])
        await session.start_video()

    web2cmd = {
        'ptz': ptz if hasattr(session, 'ptz') else None,
        'ptz-stop': getattr(session, 'ptz_stop', None),
        'home': home if hasattr(session, 'home') else None,
        'recall-preset': recall_preset if hasattr(session, 'recall_preset') else None,
        'preset-get': preset_get,
        'preset-save': preset_save,
        'start-video': start_video,
        'stop-video': stop_video,
        'start-audio': getattr(session, 'start_audio', None),
        'stop-audio': getattr(session, 'stop_audio', None),
        'talk-test': talk_test if hasattr(session, 'start_talk') else None,
        'sync-time': getattr(session, 'set_time', None),
        'reboot': getattr(session, 'reboot', None),
        'set-flip': partial(setting, session.set_flip) if hasattr(session, 'set_flip') else None,
        'set-ircut': partial(setting, session.set_ircut) if hasattr(session, 'set_ircut') else None,
        'set-night-light': (partial(setting, session.set_night_light)
                            if hasattr(session, 'set_night_light') else None),
        'toggle-flashlight': (toggle_flashlight
                              if hasattr(session, 'toggle_flashlight') else None),
        'set-status-led': (partial(setting, session.set_status_led)
                           if hasattr(session, 'set_status_led') else None),
        'set-motion-sensitivity': (partial(setting, session.set_motion_sensitivity)
                                   if hasattr(session, 'set_motion_sensitivity') else None),
        'set-motion-schedule': getattr(session, 'set_motion_schedule', None),
        'set-wifi': set_wifi if hasattr(session, 'set_wifi') else None,
    }

    if cmd not in web2cmd:
        return _json_error('unknown command', 404)
    handler = web2cmd[cmd]
    if handler is None:
        return _json_error('command not supported by this device', 501)
    try:
        detail = await handler(**params)
    except Exception as err:
        # Surface it: a silent 500 makes a server-side bug look like the camera
        # ignoring the command.
        logger.exception('Command %s failed for %s', cmd, request.match_info['dev_id'])
        return _json_error(f'{type(err).__name__}: {err}', 500)
    await _refresh_setting(session, cmd)
    # Most handlers return None and the body stays {'status': 'ok'}. set-wifi
    # needs to say more than ok/not-ok, because "sent, no reply" is a third
    # outcome and folding it into either of the other two would be a lie.
    # 'status' is spread last so no handler can overwrite the verdict.
    return web.json_response({**(detail if isinstance(detail, dict) else {}), 'status': 'ok'})


async def _refresh_setting(session, cmd):
    """Re-read what the camera now reports for the setting just written.

    The camera is the authority on whether a write stuck, and several of these
    settings have been seen to read back something other than what was sent.
    Best effort: a failed read-back must not turn a successful set into a 500,
    so the cached value is dropped rather than left showing the old reading.
    """
    entry = SETTING_REFRESH.get(cmd)
    if entry is None:
        return
    prop, getter = entry
    try:
        value = await getattr(session, getter)()
    except Exception as err:
        session.dev_properties.pop(prop, None)
        logger.debug('Could not re-read %s after %s: %s', prop, cmd, err)
        return
    if value is not None:
        session.dev_properties[prop] = int(value)


async def get_state(request):
    """Cheap liveness poll for the page. Deliberately never 404s.

    A camera that is rebooting has no Session at all, and the page still has to
    be able to say so -- answering 404 there would make the readout go blank at
    exactly the moment it is most informative.
    """
    dev_id = request.match_info['dev_id']
    session = SESSIONS.get(dev_id)
    if session is None:
        return web.json_response({
            'status': 'ok', 'connected': False, 'state': 'GONE',
            'video': False, 'audio': False, 'talking': False,
            'video_frames': 0, 'audio_frames': 0,
        })
    return web.json_response({
        'status': 'ok',
        'connected': session.state.name == 'CONNECTED',
        'state': session.state.name,
        'video': session.is_video_requested,
        'audio': session.is_audio_requested,
        'talking': session.is_talking,
        'video_frames': session.video_frames,
        'audio_frames': session.audio_frames,
        # Non-zero means a listener could not keep up and sound was lost --
        # worth seeing on the page rather than silently degrading.
        'audio_dropped': session.audio_buffer.dropped,
        # Everything needed to tell "the camera stopped sending" from "we
        # stopped parsing" without opening the log.
        'media': session.media_stats(),
        # Cached, but not stale in the way that matters: a lost camera is
        # rediscovered as a NEW Session whose setup re-reads NetInfo, so after
        # a WifiSet this is the camera's own fresh answer about which network
        # it is on -- the only evidence a join ever happened.
        'network': {'ssid': session.dev_properties.get('ssid'),
                    'ip': session.dev_properties.get('ip')},
        # Cached readings only -- no camera round trip, so this stays a cheap
        # poll. handle_commands refreshes the cache after each successful set.
        'settings': {
            prop: _describe_setting(session, enum_cls, prop)
            for _cmd, _label, enum_cls, prop in SETTING_SELECTS + LIGHT_SELECTS
            if prop
        },
    })


async def get_clock(request):
    """Read the camera clock live, rendered for a human."""
    session, err = _get_session(request)
    if err:
        return err
    try:
        clock = await session.get_time()
    except Exception as err:
        return _json_error(f'{type(err).__name__}: {err}', 500)
    if not clock.get('local'):
        # The camera answered without a usable timestamp; say that rather than
        # rendering the epoch as 1970.
        return _json_error(f'camera returned no clock: {clock}', 502)
    return web.json_response({'status': 'ok', **clock})


async def get_info(request):
    """Everything discovery and setup already know -- no camera round trip."""
    session, err = _get_session(request)
    if err:
        return err
    return web.json_response({
        'status': 'ok',
        'dev_id': session.dev.dev_id,
        'address': f'{session.dev.addr}:{session.dev.port}',
        'sdk': session.dev.sdk,
        'session_key': session.session_key,
        'state': session.state.name,
        'video_frames': session.video_frames,
        'audio_frames': session.audio_frames,
        'properties': {k: v for k, v in session.dev_properties.items()},
        'supported': describe_supported(session.dev.skill.get('support_cmds')),
        'skill': session.dev.skill,
    })


async def get_params(request):
    """Read the live settings back off the camera."""
    session, err = _get_session(request)
    if err:
        return err

    async def attempt(name, coro):
        try:
            value = await coro
            return name, (int(value) if isinstance(value, int) else value)
        except Exception as err:
            return name, {'error': f'{type(err).__name__}: {err}'}

    result = {}
    # Sequential on purpose: keeps the camera's command queue shallow.
    for name, coro in (
        ('flip', session.get_flip()),
        ('ircut', session.get_ircut()),
        ('night_light', session.get_night_light()),
        ('motion_sensitivity', session.get_motion_sensitivity()),
        ('motion_schedule', session.get_motion_schedule()),
        ('time', session.get_time()),
        ('network', session.get_network_info()),
        ('wifi', session.get_wifi_status()),
    ):
        key, value = await attempt(name, coro)
        result[key] = value
    # Name the enum values as well as printing them -- "2" alone is unreadable,
    # and this is the exact spot where low/medium being swapped went unnoticed.
    for key, enum_cls in (('flip', FlipMode), ('ircut', IRCutMode),
                          ('night_light', NightLightMode),
                          ('motion_sensitivity', MotionSensitivity)):
        if isinstance(result.get(key), int):
            try:
                result[key] = f'{result[key]} ({enum_cls(result[key]).name})'
            except ValueError:
                result[key] = f'{result[key]} (unknown for {enum_cls.__name__})'
    return web.json_response({'status': 'ok', 'params': result})


async def wifi_scan(request):
    session, err = _get_session(request)
    if err:
        return err
    try:
        return web.json_response({'status': 'ok', 'access_points': await session.scan_wifi()})
    except Exception as err:
        return _json_error(f'{type(err).__name__}: {err}', 500)


async def get_snapshot(request):
    """The latest decoded frame. There is no snapshot command on this hardware,
    so the video stream is started if it is not already running."""
    session, err = _get_session(request)
    if err:
        return err
    if not session.is_video_requested:
        await session.start_video()
    frame = session.frame_buffer.latest_frame
    if frame is None:
        try:
            frame = await asyncio.wait_for(session.get_video_frame(), timeout=5)
        except asyncio.TimeoutError:
            return _json_error('no video frame yet -- is the vendor app still connected?', 504)
    return web.Response(body=frame.data, headers={
        'content-type': 'image/jpeg',
        'cache-control': 'no-store',
    })


def _wav_header(sample_rate=8000):
    # Unknown-length stream: RIFF/data sizes are 0xFFFFFFFF, which browsers
    # accept for live playback.
    byte_rate = sample_rate * 2
    return (b'RIFF' + struct.pack('<I', 0xFFFFFFFF) + b'WAVE'
            + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, sample_rate, byte_rate, 2, 16)
            + b'data' + struct.pack('<I', 0xFFFFFFFF))


#: Live /audio listeners per device. Only the last one out stops the camera --
#: a plain boolean raced when a new listener attached while the previous one
#: was still tearing down.
_AUDIO_LISTENERS = {}


async def stream_audio(request):
    """Live audio as an endless WAV.

    Survives a camera reboot: the session is re-looked-up each iteration, and
    the gap is filled with real silence so the listener's timeline stays aligned
    with the wall clock instead of splicing out the missing seconds.
    """
    dev_id = request.match_info['dev_id']
    if dev_id not in SESSIONS:
        return _json_error('unknown device', 404)

    response = web.StreamResponse()
    response.content_type = 'audio/wav'
    await response.prepare(request)

    _AUDIO_LISTENERS[dev_id] = _AUDIO_LISTENERS.get(dev_id, 0) + 1
    started_on = None
    sample_rate = 8000
    try:
        await response.write(_wav_header(sample_rate))
        while True:
            session = SESSIONS.get(dev_id)
            if session is None:
                await asyncio.sleep(STREAM_POLL)
                await response.write(bytes(int(sample_rate * 2 * STREAM_POLL)))
                continue
            if session is not started_on:
                # A new session after a reconnect: it has no audio running, and
                # anything still queued belongs to the connection that died.
                started_on = session
                session.audio_buffer.clear()
                sample_rate = session.audio_params.get('sample_rate', 8000)
            if not session.is_audio_requested:
                try:
                    await session.start_audio()
                except (NotConnectedError, CommandError, OSError) as err:
                    # A session exists from the moment discovery finds the camera,
                    # which is before LanAuth finishes. Wait for it rather than
                    # failing the response the browser is already playing.
                    logger.debug('audio not startable yet: %s', err)
                    started_on = None
                    await asyncio.sleep(STREAM_POLL)
                    await response.write(bytes(int(sample_rate * 2 * STREAM_POLL)))
                    continue
            try:
                frame = await asyncio.wait_for(session.get_audio_frame(), STREAM_POLL)
            except asyncio.TimeoutError:
                # Nothing arrived. Emit the silence that really did occur --
                # this doubles as the write that detects a gone client.
                await response.write(bytes(int(sample_rate * 2 * STREAM_POLL)))
                continue
            await response.write(frame.data)
    except ConnectionResetError:
        pass
    # CancelledError is deliberately not caught: a handler that swallows its own
    # cancellation leaves aiohttp finishing a request it already tore down.
    finally:
        _AUDIO_LISTENERS[dev_id] = _AUDIO_LISTENERS.get(dev_id, 1) - 1
        if _AUDIO_LISTENERS[dev_id] <= 0:
            _AUDIO_LISTENERS.pop(dev_id, None)
            session = SESSIONS.get(dev_id)
            if session is not None:
                try:
                    await session.stop_audio()
                except Exception:
                    logger.debug('stop_audio on disconnect failed', exc_info=True)
    return response


async def stream_video(request):
    """Live MJPEG.

    Survives a camera reboot without a page reload: the session is re-resolved
    every iteration, and while it is missing the last frame is re-sent as a
    keepalive. Re-sending is what lets a gone client be noticed -- a handler
    that only ever waits never learns the browser went away.
    """
    dev_id = request.match_info['dev_id']
    if dev_id not in SESSIONS:
        return _json_error('unknown device', 404)

    response = web.StreamResponse()
    boundary = 'frame' + uuid.uuid4().hex
    response.content_type = f'multipart/x-mixed-replace; boundary={boundary}'
    await response.prepare(request)

    async def send(data):
        header = (f'--{boundary}\r\n'
                  f'Content-Type: image/jpeg\r\n'
                  f'Content-Length: {len(data)}\r\n\r\n').encode()
        await response.write(header + data + b'\r\n')

    last = None
    try:
        while True:
            session = SESSIONS.get(dev_id)
            if session is None:
                if last is not None:
                    await send(last)
                await asyncio.sleep(STREAM_POLL)
                continue
            if dev_id in VIDEO_OFF:
                # Deliberately off: hold the response open on the last frame
                # rather than restarting the stream behind the operator's back.
                if last is not None:
                    await send(last)
                await asyncio.sleep(STREAM_POLL)
                continue
            if not session.is_video_requested:
                try:
                    await session.start_video()
                except (NotConnectedError, CommandError, OSError) as err:
                    # Discovery registers the session before LanAuth completes,
                    # so right after a reboot this is simply "not yet".
                    logger.debug('video not startable yet: %s', err)
                    if last is not None:
                        await send(last)
                    await asyncio.sleep(STREAM_POLL)
                    continue
            try:
                frame = await asyncio.wait_for(session.frame_buffer.get(), STREAM_POLL)
            except asyncio.TimeoutError:
                if last is not None:
                    await send(last)
                continue
            if not frame or not frame.data:
                continue
            last = frame.data
            await send(last)
    except ConnectionResetError:
        # CancelledError is deliberately not caught: a handler that swallows
        # its own cancellation leaves aiohttp finishing a torn-down request.
        logger.debug('Video client disconnected')
    return response


def make_app():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/camera/{dev_id}', camera_page)
    app.router.add_get('/{dev_id}/v', stream_video)
    app.router.add_get('/{dev_id}/snapshot', get_snapshot)
    app.router.add_get('/{dev_id}/audio', stream_audio)
    app.router.add_get('/{dev_id}/info', get_info)
    app.router.add_get('/{dev_id}/state', get_state)
    app.router.add_get('/{dev_id}/time', get_clock)
    app.router.add_get('/{dev_id}/params', get_params)
    app.router.add_get('/{dev_id}/wifi-scan', wifi_scan)
    app.router.add_post('/{dev_id}/c/{cmd}', handle_commands)
    return app


async def start_web_server(port=4000):
    runner = web.AppRunner(make_app(), handle_signals=True)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    try:
        logger.info('Starting web server on port %d', port)
        await site.start()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
    finally:
        logger.info('Shutting down web server')
        await runner.cleanup()
