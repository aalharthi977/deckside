#!/usr/bin/env python3
"""
Deckside - turn a spare iPad (or any phone/tablet browser) into a control panel and Stream Deck-style
deck for this Mac: now playing, volume, mic, sound devices, battery, app launcher, keyboard shortcuts,
Shortcuts and multi-step actions, with buttons that follow the app in front.

Standard library only. Serves the panel and its API on the local network, guarded by a token.
Optional helpers (installed by install.sh): media-control (now playing + play/pause in any app on
macOS 15.4+), SwitchAudioSource (sound device switching).

Run:  python3 server.py
Env:  DECKSIDE_PORT (default 8765), DECKSIDE_TOKEN (default: generated, saved in token.txt)
"""
import base64
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0.0"
PORT = int(os.environ.get("DECKSIDE_PORT") or "8765")
# Settings live outside the app folder so updates never touch them (install.sh: ~/.deckside).
DATA = os.path.expanduser(os.environ.get("DECKSIDE_DATA") or HERE)
os.makedirs(DATA, exist_ok=True)
TOKEN_FILE = os.path.join(DATA, "token.txt")
PANEL_DIR = os.path.join(HERE, "panel")
INDEX_FILE = os.path.join(PANEL_DIR, "index.html")
CONFIG_FILE = os.path.join(DATA, "config.json")   # optional, re-read on every request - no restart needed
MIC_FILE = os.path.join(DATA, "mic_level")        # level to restore when the mic is unmuted
DEFAULT_CONFIG = {
    # Devices whose name contains any of these are hidden from the lists (virtual / app-owned devices).
    # The device currently in use is always shown, so you can see it and switch away.
    "hide_devices": ["BlackHole", "Microsoft Teams Audio", "ZoomAudioDevice", "Teams Audio"],
}
# The panel is served from this same address, so no cross-origin access is needed. Set DECKSIDE_ALLOW_ORIGIN
# (e.g. http://my-dashboard.local:8080) only if a page elsewhere should call this API; the token is still required.
ALLOW_ORIGIN = os.environ.get("DECKSIDE_ALLOW_ORIGIN") or ""


# --------------------------------------------------------------------------- auth
def load_token():
    env = (os.environ.get("DECKSIDE_TOKEN") or "").strip()
    if env:
        return env
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(9)
    with open(TOKEN_FILE, "w") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return tok


TOKEN = load_token()


# --------------------------------------------------------------------------- helpers
def run(args, timeout=5):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "command failed: %s" % args[0])
    return r.stdout.strip()


def osa(script):
    return run(["osascript", "-e", script])


def jxa(script):
    return run(["osascript", "-l", "JavaScript", "-e", script])


def config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


# --------------------------------------------------------------------------- volume + microphone
def _int_or_none(v):
    return None if v in ("missing value", "") else int(v)


def get_levels():
    """(output volume, output muted, input volume) in one osascript call."""
    out = osa(
        'set s to get volume settings\n'
        'return (output volume of s as text) & "|" & (output muted of s as text) & "|" & (input volume of s as text)'
    )
    vol, muted, mic = (out.split("|") + ["", "", ""])[:3]
    # Some devices (HDMI, some USB DACs/mics) have no software level -> "missing value"
    return _int_or_none(vol), muted == "true", _int_or_none(mic)


def get_volume():
    vol, muted, _ = get_levels()
    return vol, muted


def set_input_volume(level):
    level = max(0, min(100, int(level)))
    osa("set volume input volume %d" % level)
    if level > 0:
        with open(MIC_FILE, "w") as f:
            f.write(str(level))


def toggle_mic(muted=None):
    """Mute = input volume 0 (works in every app: Teams, Zoom, Webex...). Unmute restores the last level."""
    _, _, mic = get_levels()
    if mic is None:
        raise ValueError("this microphone has no software level - mute it in the meeting app")
    if muted is None:
        muted = mic > 0
    if muted:
        if mic > 0:
            with open(MIC_FILE, "w") as f:
                f.write(str(mic))
        osa("set volume input volume 0")
    else:
        try:
            with open(MIC_FILE) as f:
                level = int(f.read().strip() or 75)
        except (OSError, ValueError):
            level = 75
        osa("set volume input volume %d" % max(level, 10))


def set_volume(level):
    level = max(0, min(100, int(level)))
    osa("set volume output volume %d" % level)
    if level > 0:
        osa("set volume without output muted")


def set_muted(muted):
    osa("set volume %s output muted" % ("with" if muted else "without"))


# --------------------------------------------------------------------------- media
# Players controlled directly via AppleScript when they are the one playing.
PLAYERS = ("Spotify", "Music")
APP_CMDS = {"playpause": "playpause", "next": "next track", "previous": "previous track"}
# NX_KEYTYPE_* codes for the hardware media keys (work for any app: browsers, VLC, IINA...)
MEDIA_KEYS = {"playpause": 16, "next": 17, "previous": 18}
SPOTIFY_STATES = {"kPSP": "playing", "kPSp": "paused", "kPSS": "stopped"}

MEDIA_KEY_JXA = r"""
ObjC.import('AppKit');
ObjC.import('CoreGraphics');
function tap(k) {
  [true, false].forEach(function (down) {
    var ev = $.NSEvent.otherEventWithTypeLocationModifierFlagsTimestampWindowNumberContextSubtypeData1Data2(
      14, {x: 0, y: 0}, down ? 0xa00 : 0xb00, 0, 0, null, 8,
      (k << 16) | ((down ? 0xa : 0xb) << 8), -1);
    $.CGEventPost(0, ev.CGEvent);
  });
}
tap(%d);
"""


def is_running(proc):
    return subprocess.run(["pgrep", "-xq", proc]).returncode == 0


def player_info(app):
    out = osa(
        'tell application "%s"\n'
        '  set st to player state as text\n'
        '  if st is "stopped" or st is "kPSS" then return st\n'
        '  try\n'
        '    return st & linefeed & (name of current track) & linefeed & (artist of current track)\n'
        '  on error\n'
        '    return st\n'
        '  end try\n'
        'end tell' % app
    )
    parts = out.split("\n")
    state = SPOTIFY_STATES.get(parts[0], parts[0])
    return {
        "app": app,
        "state": state,
        "title": parts[1] if len(parts) > 1 else "",
        "artist": parts[2] if len(parts) > 2 else "",
    }


# media-control (brew install media-control) talks to macOS "Now Playing" via MediaRemote,
# which works on macOS 15.4+ / Tahoe where synthetic media keys no longer do. Covers every app
# (YouTube in any browser, Spotify, Music, VLC, IINA...).
MC_CMDS = {"playpause": ["toggle-play-pause"],
           "next": ["next-track", "next"],
           "previous": ["previous-track", "previous"]}
APP_NAMES = {"com.spotify.client": "Spotify", "com.apple.Music": "Music", "com.apple.Safari": "Safari",
             "com.google.Chrome": "Chrome", "company.thebrowser.Browser": "Arc", "com.brave.Browser": "Brave",
             "org.mozilla.firefox": "Firefox", "com.microsoft.edgemac": "Edge", "org.videolan.vlc": "VLC",
             "com.colliderli.iina": "IINA", "com.apple.podcasts": "Podcasts", "com.apple.TV": "TV"}


def media_control():
    for p in ("/opt/homebrew/bin/media-control", "/usr/local/bin/media-control"):
        if os.path.exists(p):
            return p
    return shutil.which("media-control")


ART = {"id": None, "mime": None, "data": None}   # latest artwork, served by /api/artwork


def _epoch(ts):
    """media-control timestamps: epoch seconds/ms or ISO-8601 -> epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return ts / 1000.0 if ts > 1e12 else float(ts)
    try:
        t = re.sub(r"(\.\d{6})\d+", r"\1", str(ts)).replace("Z", "+00:00")
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mc_now_playing(mc):
    out = run([mc, "get"], timeout=6)
    data = json.loads(out) if out and out != "null" else None
    if not data or not (data.get("title") or data.get("bundleIdentifier")):
        return None
    bid = data.get("bundleIdentifier") or data.get("parentApplicationBundleIdentifier") or ""
    app = APP_NAMES.get(bid) or (bid.rsplit(".", 1)[-1].capitalize() if bid else "")
    playing = bool(data.get("playing"))
    rate = _num(data.get("playbackRate")) or 1.0
    duration = _num(data.get("duration"))
    elapsed = _num(data.get("elapsedTimeNow"))
    if elapsed is None:
        elapsed = _num(data.get("elapsedTime"))
        ts = _epoch(data.get("timestamp"))
        if elapsed is not None and ts and playing:
            elapsed += max(0.0, time.time() - ts) * rate
    if elapsed is not None and duration:
        elapsed = max(0.0, min(elapsed, duration))

    art_id = None
    b64 = data.get("artworkData")
    if b64:
        art_id = hashlib.sha1(b64[:4096].encode() + str(len(b64)).encode()).hexdigest()[:16]
        if art_id != ART["id"]:
            try:
                ART.update(id=art_id, mime=data.get("artworkMimeType") or "image/jpeg", data=base64.b64decode(b64))
            except ValueError:
                art_id = None
    return {"app": app, "state": "playing" if playing else "paused",
            "title": data.get("title") or "", "artist": data.get("artist") or "", "album": data.get("album") or "",
            "duration": duration, "elapsed": elapsed, "rate": rate if playing else 0, "artwork_id": art_id,
            "can_seek": bool(duration)}


def seek(position=None, delta=None):
    mc = media_control()
    if not mc:
        raise ValueError("seeking needs media-control (brew install media-control)")
    np = mc_now_playing(mc)
    if not np or not np.get("duration"):
        raise ValueError("nothing seekable is playing")
    if position is None:
        position = (np.get("elapsed") or 0) + float(delta or 0)
    position = max(0.0, min(float(position), np["duration"] - 0.5))
    run([mc, "seek", "%.1f" % position], timeout=6)


def scripted_now_playing():
    """Fallback without media-control: Spotify / Music via AppleScript."""
    paused = None
    for app in PLAYERS:
        if not is_running(app):  # never launch an app just by asking
            continue
        try:
            info = player_info(app)
        except Exception:
            continue
        if info["state"] == "playing":
            return info
        if paused is None and info["state"] == "paused":
            paused = info
    return paused


def now_playing():
    mc = media_control()
    if mc:
        try:
            return mc_now_playing(mc)
        except Exception:
            pass
    return scripted_now_playing()


def media(action):
    mc = media_control()
    if mc:
        last = None
        for sub in MC_CMDS[action]:
            try:
                run([mc, sub], timeout=6)
                return
            except Exception as e:
                last = e
        sys.stderr.write("media-control %s failed: %s\n" % (action, last))
    np = scripted_now_playing()
    if np:
        osa('tell application "%s" to %s' % (np["app"], APP_CMDS[action]))
        return
    jxa(MEDIA_KEY_JXA % MEDIA_KEYS[action])  # last resort (older macOS)


# --------------------------------------------------------------------------- devices (output + input)
def switcher():
    for p in ("/opt/homebrew/bin/SwitchAudioSource", "/usr/local/bin/SwitchAudioSource"):
        if os.path.exists(p):
            return p
    return shutil.which("SwitchAudioSource")


def get_devices(kind):
    """(visible device names, current device) for kind 'output' or 'input'."""
    sw = switcher()
    if not sw:
        return None, None
    names = [l.strip() for l in run([sw, "-a", "-t", kind]).splitlines() if l.strip()]
    current = run([sw, "-c", "-t", kind])
    hide = [h.lower() for h in config().get("hide_devices", [])]
    visible = [n for n in names if n == current or not any(h in n.lower() for h in hide)]
    return visible, current


def get_outputs():
    return get_devices("output")


def set_device(kind, name):
    sw = switcher()
    if not sw:
        raise ValueError("SwitchAudioSource not installed (brew install switchaudio-osx)")
    names = [l.strip() for l in run([sw, "-a", "-t", kind]).splitlines() if l.strip()]
    if name not in names:
        raise ValueError("unknown %s device: %s" % (kind, name))
    run([sw, "-s", name, "-t", kind])


def set_output(name):
    set_device("output", name)


# --------------------------------------------------------------------------- battery
def battery():
    """{"pct", "state", "source", "remaining"} from pmset, or None on Macs without a battery."""
    out = run(["pmset", "-g", "batt"])
    m = re.search(r"(\d+)%;\s*([^;]+);?\s*(.*)", out)
    if not m or "InternalBattery" not in out:
        return None
    rem = re.search(r"(\d+:\d+) remaining", m.group(3))
    st = m.group(2).strip().lower()
    return {"pct": int(m.group(1)),
            "state": st,                               # charging / discharging / charged / ac attached / finishing charge
            "source": "ac" if "AC Power" in out else "battery",
            "remaining": rem.group(1) if rem and rem.group(1) != "0:00" else None}


# --------------------------------------------------------------------------- state
def state():
    s = {"host": socket.gethostname()}
    try:
        s["volume"], s["muted"], s["input_volume"] = get_levels()
    except Exception as e:
        s["volume"], s["muted"], s["input_volume"], s["volume_error"] = None, False, None, str(e)
    s["mic_muted"] = s["input_volume"] == 0
    try:
        s["now_playing"] = now_playing()
    except Exception:
        s["now_playing"] = None
    try:
        s["outputs"], s["current_output"] = get_outputs()
    except Exception as e:
        s["outputs"], s["current_output"], s["output_error"] = None, None, str(e)
    try:
        s["inputs"], s["current_input"] = get_devices("input")
    except Exception as e:
        s["inputs"], s["current_input"], s["input_error"] = None, None, str(e)
    try:
        s["battery"] = battery()
    except Exception:
        s["battery"] = None
    return s


# --------------------------------------------------------------------------- deck (Stream Deck-style buttons)
# The iPad can only PRESS buttons that are saved in deck.json, by id. It never sends a command, path or
# script to run: apps are launched by bundle id, Shortcuts by name, links must be http(s).
DECK_FILE = os.path.join(DATA, "deck.json")
ICON_DIR = os.path.join(DATA, "icons")
APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications",
            "/System/Applications/Utilities", os.path.expanduser("~/Applications")]
EXTRA_APPS = ["/System/Library/CoreServices/Finder.app"]
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{1,200}$")
ID_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
SEED_APPS = ["com.apple.finder", "com.apple.Safari", "company.thebrowser.dia", "com.google.Chrome",
             "com.microsoft.teams2", "us.zoom.xos", "com.apple.mail", "com.apple.iCal", "com.apple.Notes",
             "com.spotify.client", "com.apple.Music", "com.apple.systempreferences"]
_cache = {"apps": (0, None), "shortcuts": (0, None)}


def _plist(path):
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def installed_apps():
    """{bundle_id: {"bundle", "name", "path"}} for apps in the usual folders (one level of sub-folders)."""
    t, apps = _cache["apps"]
    if apps is not None and time.time() - t < 300:
        return apps
    found = {}

    def add(path):
        info = _plist(os.path.join(path, "Contents", "Info.plist"))
        bid = info.get("CFBundleIdentifier")
        if not bid or bid in found or not BUNDLE_RE.match(bid):
            return
        if info.get("LSBackgroundOnly") or info.get("LSUIElement") in (True, "1", 1):
            return  # menu-bar helpers / agents - not useful as deck buttons
        found[bid] = {"bundle": bid, "name": os.path.basename(path)[:-4], "path": path}

    for d in APP_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name.endswith(".app"):
                add(p)
            elif os.path.isdir(p) and not name.startswith("."):
                try:  # e.g. /Applications/Adobe Photoshop 2026/Adobe Photoshop 2026.app
                    for sub in os.listdir(p):
                        if sub.endswith(".app"):
                            add(os.path.join(p, sub))
                except OSError:
                    pass
    for p in EXTRA_APPS:
        if os.path.isdir(p):
            add(p)
    _cache["apps"] = (time.time(), found)
    return found


def shortcut_names():
    t, names = _cache["shortcuts"]
    if names is not None and time.time() - t < 60:
        return names
    try:
        names = [l.strip() for l in run(["shortcuts", "list"], timeout=15).splitlines() if l.strip()]
    except Exception:
        names = []
    _cache["shortcuts"] = (time.time(), names)
    return names


# ---- keyboard shortcuts: parsed into a fixed vocabulary, never passed through as text
KEY_CODES = {"return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
             "forwarddelete": 117, "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
             "pageup": 116, "pagedown": 121, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
             "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111}
MODS = {"cmd": "command", "command": "command", "shift": "shift", "opt": "option", "option": "option",
        "alt": "option", "ctrl": "control", "control": "control"}
MOD_ORDER = ["control", "option", "shift", "command"]
MOD_SHORT = {"control": "ctrl", "option": "opt", "shift": "shift", "command": "cmd"}
KEY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-=[];',./`\\"


def parse_combo(text):
    """'cmd+shift+4' -> (["command","shift"], ("char","4"), "shift+cmd+4"). Raises ValueError."""
    parts = [p for p in str(text or "").lower().replace(" ", "").split("+") if p]
    if not parts or len(parts) > 5:
        raise ValueError("shortcut looks like cmd+shift+4")
    key, mods = parts[-1], []
    for m in parts[:-1]:
        if m not in MODS:
            raise ValueError("unknown modifier: %s (use cmd, shift, opt, ctrl)" % m)
        if MODS[m] not in mods:
            mods.append(MODS[m])
    mods.sort(key=MOD_ORDER.index)
    if key in KEY_CODES:
        k = ("code", KEY_CODES[key])
    elif len(key) == 1 and key in KEY_CHARS:
        k = ("char", key)
    else:
        raise ValueError("unknown key: %s" % key)
    return mods, k, "+".join([MOD_SHORT[m] for m in mods] + [key])


def send_keys(combo, app=None):
    mods, k, _ = parse_combo(combo)
    using = (" using {%s}" % ", ".join(m + " down" for m in mods)) if mods else ""
    if k[0] == "code":
        action = "key code %d%s" % (k[1], using)
    else:
        ch = k[1].replace("\\", "\\\\").replace('"', '\\"')
        action = 'keystroke "%s"%s' % (ch, using)
    script = ""
    if app:
        if not BUNDLE_RE.match(app):
            raise ValueError("bad app id")
        script += 'tell application id "%s" to activate\ndelay 0.35\n' % app
    script += 'tell application "System Events" to %s' % action
    try:
        osa(script)
    except RuntimeError as e:
        msg = str(e)
        if "not allowed" in msg or "1002" in msg or "assistive" in msg:
            raise ValueError("macOS blocked the keystroke: allow python3 in System Settings > Privacy & Security > "
                             "Accessibility (and Automation > System Events)")
        raise


STEP_TYPES = ("app", "shortcut", "url", "keys", "wait", "volume", "mic")


def validate_step(st):
    t = st.get("type")
    if t == "app":
        b = str(st.get("bundle") or "")
        if not BUNDLE_RE.match(b):
            raise ValueError("step: bad app id")
        return {"type": "app", "bundle": b}
    if t == "shortcut":
        n = str(st.get("name") or "").strip()[:100]
        if not n:
            raise ValueError("step: shortcut needs a name")
        return {"type": "shortcut", "name": n}
    if t == "url":
        u = str(st.get("url") or "").strip()[:500]
        if not re.match(r"^https?://[^\s\"'`]+$", u):
            raise ValueError("step: links must start with http:// or https://")
        return {"type": "url", "url": u}
    if t == "keys":
        out = {"type": "keys", "combo": parse_combo(st.get("combo"))[2]}
        if st.get("app"):
            if not BUNDLE_RE.match(str(st["app"])):
                raise ValueError("step: bad app id")
            out["app"] = str(st["app"])
        return out
    if t == "wait":
        return {"type": "wait", "ms": max(100, min(10000, int(st.get("ms") or 500)))}
    if t == "volume":
        return {"type": "volume", "level": max(0, min(100, int(st.get("level") or 0)))}
    if t == "mic":
        return {"type": "mic", "muted": bool(st.get("muted"))}
    raise ValueError("unknown step type: %s" % t)


def run_step(st, wait_shortcut=True):
    t = st["type"]
    if t == "app":
        run(["open", "-b", st["bundle"]], timeout=15)
    elif t == "url":
        run(["open", st["url"]], timeout=15)
    elif t == "shortcut":
        if st["name"] not in shortcut_names():
            raise ValueError('no Shortcut named "%s" on this Mac' % st["name"])
        if wait_shortcut:
            run(["shortcuts", "run", st["name"]], timeout=120)
        else:
            subprocess.Popen(["shortcuts", "run", st["name"]], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    elif t == "keys":
        send_keys(st["combo"], st.get("app"))
    elif t == "wait":
        time.sleep(st["ms"] / 1000.0)
    elif t == "volume":
        set_volume(st["level"])
    elif t == "mic":
        toggle_mic(st["muted"])


def run_multi(label, steps):
    for i, st in enumerate(steps):
        try:
            run_step(st)
        except Exception as e:
            sys.stderr.write("%s multi \"%s\" stopped at step %d (%s): %s\n" % (time.strftime("%F %T"), label, i + 1, st["type"], e))
            return


def _new_id():
    return uuid.uuid4().hex[:10]


def starter_profiles(apps):
    """'In front' buttons for installed apps, from starter_profiles.json (first run only)."""
    try:
        with open(os.path.join(HERE, "starter_profiles.json")) as f:
            starters = json.load(f).get("profiles", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for key, prof in starters.items():
        for bundle in apps:
            if bundle == key or bundle.startswith(key + "."):
                out[bundle] = {"buttons": [{"id": _new_id(), "type": "keys", "combo": b["combo"], "label": b["label"]}
                                           for b in prof.get("buttons", [])]}
    return out


def default_deck():
    apps = installed_apps()
    buttons = [{"id": _new_id(), "type": "app", "bundle": b, "label": apps[b]["name"]}
               for b in SEED_APPS if b in apps][:10]
    return {"version": 1, "pages": [{"name": "Main", "buttons": buttons}], "profiles": starter_profiles(apps)}


def validate_button(b):
    typ, bid = b.get("type"), str(b.get("id") or "")
    if not ID_RE.match(bid):
        bid = _new_id()
    label = str(b.get("label") or "").strip()[:32]
    if typ == "app":
        bundle = str(b.get("bundle") or "")
        if not BUNDLE_RE.match(bundle):
            raise ValueError("bad app id: %s" % bundle)
        return {"id": bid, "type": "app", "bundle": bundle, "label": label or bundle}
    if typ == "shortcut":
        name_ = str(b.get("name") or "").strip()[:100]
        if not name_:
            raise ValueError("shortcut needs a name")
        return {"id": bid, "type": "shortcut", "name": name_, "label": label or name_}
    if typ == "url":
        url = str(b.get("url") or "").strip()[:500]
        if not re.match(r"^https?://[^\s\"'`]+$", url):
            raise ValueError("links must start with http:// or https://")
        return {"id": bid, "type": "url", "url": url, "label": label or url}
    if typ == "keys":
        k = validate_step({"type": "keys", "combo": b.get("combo"), "app": b.get("app")})
        k.update(id=bid, label=label or k["combo"])
        return k
    if typ == "multi":
        steps = b.get("steps") or []
        if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
            raise ValueError("a multi-action needs 1-10 steps")
        return {"id": bid, "type": "multi", "label": label or "Multi-action", "steps": [validate_step(st) for st in steps]}
    raise ValueError("unknown button type: %s" % typ)


def validate_deck(d):
    if not isinstance(d, dict) or not isinstance(d.get("pages"), list) or not 1 <= len(d["pages"]) <= 10:
        raise ValueError("deck needs 1-10 pages")
    pages = []
    for pg in d["pages"]:
        name = str(pg.get("name") or "Page").strip()[:24] or "Page"
        btns = pg.get("buttons") or []
        if not isinstance(btns, list) or len(btns) > 30:
            raise ValueError("a page holds at most 30 buttons")
        pages.append({"name": name, "buttons": [validate_button(b) for b in btns]})
    # per-app profiles: shown automatically while that app is in front
    profiles = {}
    raw = d.get("profiles") or {}
    if not isinstance(raw, dict) or len(raw) > 50:
        raise ValueError("at most 50 app profiles")
    for bundle, pr in raw.items():
        if not BUNDLE_RE.match(str(bundle)):
            raise ValueError("bad app id in profiles: %s" % bundle)
        btns = (pr or {}).get("buttons") or []
        if not isinstance(btns, list) or len(btns) > 12:
            raise ValueError("an app profile holds at most 12 buttons")
        if btns:
            profiles[str(bundle)] = {"buttons": [validate_button(b) for b in btns]}
    return {"version": 1, "pages": pages, "profiles": profiles}


def load_deck():
    try:
        with open(DECK_FILE) as f:
            return validate_deck(json.load(f))
    except (OSError, ValueError):
        d = default_deck()
        save_deck(d)
        return d


def save_deck(d):
    tmp = DECK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, DECK_FILE)


def running_apps():
    """([(bundle, name)] foreground apps in launch order, frontmost bundle) via lsappinfo - no permissions needed."""
    running, front = [], None
    try:
        out = run(["lsappinfo", "list"], timeout=4)
        for block in re.split(r"\n\s*\d+\) \"", "\n" + out):
            m = re.search(r'bundleID="([^"]+)"', block)
            if m and re.search(r'type="?Foreground"?', block):
                running.append((m.group(1), block.split('"', 1)[0].strip()))
    except Exception:
        pass
    try:
        asn = run(["lsappinfo", "front"], timeout=3).strip()
        m = re.search(r'=\s*"([^"]+)"', run(["lsappinfo", "info", "-only", "bundleid", asn], timeout=3))
        front = m.group(1) if m else None
    except Exception:
        pass
    return running, front


def deck_state():
    running, front = running_apps()
    apps = installed_apps()
    named = [{"bundle": b, "name": (apps.get(b) or {}).get("name") or n or b} for b, n in running]
    front_name = next((a["name"] for a in named if a["bundle"] == front), None)
    if front and not front_name:
        front_name = (apps.get(front) or {}).get("name") or front
    return {"deck": load_deck(), "running": sorted(b for b, _ in running), "running_apps": named,
            "front": front, "front_name": front_name}


def switch_to(bundle):
    """Bring a RUNNING app to the front - only apps lsappinfo reports as running are accepted."""
    running, _ = running_apps()
    if not BUNDLE_RE.match(bundle or "") or bundle not in [b for b, _ in running]:
        raise ValueError("that app isn't running anymore")
    run(["open", "-b", bundle], timeout=15)


def press(page, button_id, profile=None):
    deck = load_deck()
    try:
        source = deck["profiles"][profile]["buttons"] if profile else deck["pages"][int(page)]["buttons"]
        btn = next(b for b in source if b["id"] == button_id)
    except (KeyError, IndexError, StopIteration, ValueError, TypeError):
        raise ValueError("button not found - the deck may have changed, reload")
    if btn["type"] == "app":
        run(["open", "-b", btn["bundle"]], timeout=15)          # launches, or brings to front
    elif btn["type"] == "url":
        run(["open", btn["url"]], timeout=15)
    elif btn["type"] == "shortcut":
        # Shortcuts can take a while - fire and forget, don't hold the request
        run_step(btn, wait_shortcut=False)
    elif btn["type"] == "keys":
        send_keys(btn["combo"], btn.get("app"))
    elif btn["type"] == "multi":
        # check what can be checked now, so the iPad sees the error; then run in the background
        names = None
        for st in btn["steps"]:
            if st["type"] == "shortcut":
                names = names if names is not None else shortcut_names()
                if st["name"] not in names:
                    raise ValueError('step uses a Shortcut that no longer exists: "%s"' % st["name"])
        threading.Thread(target=run_multi, args=(btn["label"], btn["steps"]), daemon=True).start()


ICON_JXA = r"""
ObjC.import('AppKit');
var ws = $.NSWorkspace.sharedWorkspace;
var url = ws.URLForApplicationWithBundleIdentifier(%s);
if (!url || url.isNil()) { throw new Error('not installed'); }
var img = ws.iconForFile(url.path);
img.setSize({width: 256, height: 256});
var rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
var png = rep.representationUsingTypeProperties(4, $({}));
png.writeToFileAtomically(%s, true);
"""


def app_icon(bundle):
    if not BUNDLE_RE.match(bundle or ""):
        raise ValueError("bad app id")
    os.makedirs(ICON_DIR, exist_ok=True)
    out = os.path.join(ICON_DIR, bundle + ".png")
    if not os.path.exists(out):
        try:
            jxa(ICON_JXA % (json.dumps(bundle), json.dumps(out)))
        except Exception:
            # fallback: convert the bundle's .icns with sips (older-style apps)
            app = installed_apps().get(bundle)
            if not app:
                return None
            icon = _plist(os.path.join(app["path"], "Contents", "Info.plist")).get("CFBundleIconFile")
            if not icon:
                return None
            icns = os.path.join(app["path"], "Contents", "Resources", icon if icon.endswith(".icns") else icon + ".icns")
            try:
                run(["sips", "-s", "format", "png", "-Z", "256", icns, "--out", out], timeout=10)
            except Exception:
                return None
    try:
        with open(out, "rb") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "Deckside/" + VERSION

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/api/state" in line or "/api/icon" in line or "GET /api/deck" in line:
            return  # don't flood the log with polling
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        if not ALLOW_ORIGIN:
            return  # same-origin only
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "X-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self):
        q = parse_qs(urlparse(self.path).query)
        tok = self.headers.get("X-Token") or (q.get("token") or [""])[0]
        return secrets.compare_digest(tok.encode("utf-8"), TOKEN.encode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(INDEX_FILE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path in ("/apple-touch-icon.png", "/icon.png"):
            try:
                with open(os.path.join(PANEL_DIR, "icon.png"), "rb") as f:
                    return self._send(200, f.read(), "image/png", cache="public, max-age=86400")
            except OSError:
                return self._send(404, {"error": "no icon"})
        if path == "/healthz":  # no token: used by the installer and `deckside status`
            return self._send(200, {"ok": True, "app": "deckside", "version": VERSION})
        if path == "/api/config":
            if not self._authed():
                return self._send(401, {"error": "bad token"})
            # same-origin: the panel calls this address; "" means "the page's own host"
            return self._send(200, {"mac_urls": [""], "mac_token": TOKEN, "app": "deckside", "version": VERSION})
        if path == "/api/state":
            if not self._authed():
                return self._send(401, {"error": "bad token"})
            return self._send(200, state())
        if path == "/api/artwork":  # <img src> can't send headers, so the token comes in the query
            if not self._authed():
                return self._send(401, {"error": "bad token"})
            if not ART["data"]:
                return self._send(404, {"error": "no artwork"})
            # URL carries the artwork id, so a changed image is a new URL - safe to cache
            return self._send(200, ART["data"], ART["mime"], cache="private, max-age=86400")
        if path in ("/api/deck", "/api/apps", "/api/shortcuts", "/api/icon"):
            if not self._authed():
                return self._send(401, {"error": "bad token"})
            try:
                if path == "/api/deck":
                    return self._send(200, deck_state())
                if path == "/api/apps":
                    apps = sorted(installed_apps().values(), key=lambda a: a["name"].lower())
                    return self._send(200, {"apps": [{"bundle": a["bundle"], "name": a["name"]} for a in apps]})
                if path == "/api/shortcuts":
                    _cache["shortcuts"] = (0, None)  # always fresh when the picker opens
                    return self._send(200, {"shortcuts": shortcut_names()})
                q = parse_qs(urlparse(self.path).query)
                png = app_icon((q.get("bundle") or [""])[0])
                if not png:
                    return self._send(404, {"error": "no icon"})
                return self._send(200, png, "image/png", cache="private, max-age=604800")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if path in ("/api/deck", "/api/deck/press", "/api/deck/switch"):
            try:
                if path == "/api/deck":
                    save_deck(validate_deck(data.get("deck")))
                elif path == "/api/deck/switch":
                    switch_to(str(data.get("bundle", "")))
                else:
                    press(data.get("page", 0), str(data.get("id", "")), data.get("profile") or None)
                return self._send(200, deck_state())
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        try:
            if path == "/api/volume":
                set_volume(data["level"])
            elif path == "/api/volume/step":
                vol, _ = get_volume()
                set_volume((50 if vol is None else vol) + int(data.get("delta", 5)))
            elif path == "/api/mute":
                m = data.get("muted", "toggle")
                if m == "toggle":
                    m = not get_volume()[1]
                set_muted(bool(m))
            elif path == "/api/media":
                action = data.get("action")
                if action not in APP_CMDS:
                    raise ValueError("action must be one of %s" % ", ".join(APP_CMDS))
                media(action)
            elif path == "/api/output":
                set_device("output", data["name"])
            elif path == "/api/input":
                set_device("input", data["name"])
            elif path == "/api/mic":
                m = data.get("muted", "toggle")
                toggle_mic(None if m == "toggle" else bool(m))
            elif path == "/api/mic/volume":
                set_input_volume(data["level"])
            elif path == "/api/seek":
                seek(data.get("position"), data.get("delta"))
            else:
                return self._send(404, {"error": "not found"})
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self._send(200, state())


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def local_hostname():
    try:
        name = run(["scutil", "--get", "LocalHostName"])
        if name:
            return name + ".local"
    except Exception:
        pass
    return lan_ip()


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Deckside %s running." % VERSION)
    print("Open on the iPad:  http://%s:%d/?token=%s" % (local_hostname(), PORT, TOKEN))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
