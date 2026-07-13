"""Load the rig configuration.

Resolution order for the config file:
  1. $RIG_CONF if set
  2. bin/rig.toml           (your real config — git-ignored, may hold a private ntfy topic)
  3. bin/rig.example.toml   (committed template — used as-is if you never copy it)

Whatever file is found is merged over DEFAULTS below, so a partial rig.toml
only needs to override what differs from the template. Python 3.11+ reads TOML
natively via tomllib (no dependency).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent

# Generic skeleton so the engine runs out of the box — NOTHING here is personal.
# Your real rig (gig .als path, network IPs, device names, lamp MACs, ntfy topic)
# lives in rig.toml, which is git-ignored and deep-merged over these defaults.
DEFAULTS: dict = {
    "set": {
        "ableton_app": "/Applications/Ableton Live 12 Suite.app",
        "project": "",                     # your gig .als — set in rig.toml
        "open_after_launch": True,
    },
    "launch": {
        # Standard app locations; override the list in rig.toml for your own rig.
        "apps": [
            "/Applications/Bome MIDI Translator Pro.app",
            "/Applications/Bome Network.app",
            "/Applications/Elgato Stream Deck.app",
        ],
        "settle_seconds": 2,   # grace after an app launches before polling readiness
        # Shell commands run after launching apps (e.g. start an anti-sleep session).
        # Each: a string, or {cmd, label}.
        "post_cmds": [],
    },
    "checks": {
        # label -> regex matched against the full process command line (pgrep -f).
        "apps": {
            "Ableton": "Ableton Live.*/MacOS/Live",
            "Stream Deck": "Elgato Stream Deck.app/Contents/MacOS/Stream Deck",
            "Bome MIDI Translator": "MIDITranslatorPro",
            "Bome Network": "Bome Network.app/Contents/MacOS/MT Player",
        },
        # MIDI input ports that MUST be present (substring match).
        "midi_required": ["Ableton Loopback"],
        "breath_port": "Breath Controller",     # breath controller's MIDI input name
        "audio_interface": "USB Audio",         # your interface's name — set in rig.toml
        "default_output_match": "MacBook",      # macOS default output should be the Mac
        # Subnet the Mac must hold an IP on (example — set your own in rig.toml).
        "stage_network": "192.168.1",
        # USB devices that must be plugged: {label = product-name substring} (ioreg).
        "usb_devices": {},
        # Named hosts that must respond: {name, ip OR mac, severity?, icon?}.
        "hosts": [],
        # Remote links = an ESTABLISHED TCP connection on a port: {name, port, host?,
        # severity?, icon?}. (e.g. an iPhone connecting to Bome Network.)
        "links": [],
        # Arbitrary command checks — the domain-open escape hatch. Each:
        # {name, cmd, expect_exit?=0, expect_match?, severity?, icon?, timeout?}.
        "commands": [],
        # Keep-awake app holding a power assertion (optional): {process, owner, label,
        # icon}. `owner` = name shown in `pmset -g assertions`. Absent = not checked.
        # "keepawake": {"process": "...", "owner": "...", "label": "...", "icon": "☕"},
        # Value probed from an app log (optional): {log_glob, pattern, label, icon}.
        # Compared against the mode's `live_output`. Absent = not checked.
        # "output_probe": {"log_glob": "...", "pattern": "...(.+)", "label": "...", "icon": "🎚️"},
    },
    # Two rigs, one tool. Start "live"; when the studio router is reachable, "auto"
    # resolves to "studio". The dashboard tirette forces it; CLI: --mode live|studio|auto.
    # Profile auto-detection. `detect` = ordered [{profile, <criterion>}]; first whose
    # criterion holds wins (criteria: ping / interface / cmd), else `fallback`.
    "mode": {"default": "auto", "fallback": "live", "detect": []},
    "modes": {
        "live": {
            # keyboard_ok present → green; only keyboard_warn present → yellow; none → red.
            "keyboard_ok": ["Piano"],              # your main keyboard's MIDI port name
            "keyboard_warn": [],
            "live_output": ["Piano"],              # Ableton's audio output device on stage
            "require_awake": True,
            "breath_severity": "fail",
            "interface_severity": "fail",
            "mac_power_severity": "fail",
            "iphone_power_severity": "fail",
        },
        "studio": {
            "keyboard_ok": ["Piano"],
            "keyboard_warn": ["microKey"],
            "live_output": ["MacBook", "USB Audio"],
            "require_awake": False,
            "breath_severity": "warn",
            "interface_severity": "warn",
            "mac_power_severity": "warn",
            "iphone_power_severity": "warn",
        },
    },
    "monitor": {
        "interval": 5,        # seconds between fast checks (apps + MIDI)
        "audio_every": 6,     # run the slow audio check once every N cycles
        "alerts": ["macos", "push", "streamdeck"],  # backends: macos, push, streamdeck
        "recovery_alerts": True,
    },
    "alerts": {
        "push": {
            "server": "https://ntfy.sh",
            "topic": "",       # set a PRIVATE topic, e.g. "my-rig-9d3f", to enable
            "priority": "high",
        },
        "streamdeck": {
            # Dedicated dead-end IAC port so the alert note never hits the live routing.
            # Create it: Audio MIDI Setup → IAC Driver → "+" → rename to "rig-alert".
            "port": "rig-alert",
            "channel": 15,     # 1-16 (kept off the musical channels)
            "note": 60,
        },
    },
    # Signal-flow diagram topology (config-driven). nodes: {id,label,icon,keys,x,y};
    # edges: [[fromId,toId],…]. Empty → diagram hidden. Define yours in rig.toml.
    "diagram": {"nodes": [], "edges": []},
    # Dashboard icons, as data (no emoji hardcoded in the engine). Keyed by a check
    # key or a prefix; resolution = exact key, else the longest matching prefix, else
    # "•". A host's own `icon` (from [[checks.hosts]]) always wins. Override in rig.toml.
    "icons": {
        "app:Ableton": "🎵", "app:Stream": "🎛️", "app:Bome": "🔀", "app:Stage": "▶️",
        "usb": "🎛️", "host": "📡",
        "kbd:breath": "🌬️", "kbd": "🎹",
        "net:stage": "🌐", "net": "📱",
        "sys:vpn": "🔒", "sys:output": "💻", "sys:keepawake": "☕",
        "sys:macpower": "🔌", "sys:iphonecharge": "🔋",
        "audio:probe": "🎚️", "audio": "🔊",
        "midi?": "🎹", "midi": "🔌", "cmd": "⚙️",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path() -> Path | None:
    env = os.environ.get("RIG_CONF")
    if env:
        return Path(env)
    for name in ("rig.toml", "rig.example.toml"):
        p = BIN_DIR / name
        if p.exists():
            return p
    return None


def load() -> dict:
    path = config_path()
    if path and path.exists():
        with path.open("rb") as fh:
            user = tomllib.load(fh)
        return _deep_merge(DEFAULTS, user)
    return DEFAULTS
