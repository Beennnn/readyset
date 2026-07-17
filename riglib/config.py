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
        "app": "",                         # app that opens the project (set in rig.toml)
        "project": "",                     # your gig .als — set in rig.toml
        "open_after_launch": True,
    },
    "launch": {
        # Standard app locations; override the list in rig.toml for your own rig.
        "apps": [],   # apps to launch (paths) — set in rig.toml
        "settle_seconds": 2,   # grace after an app launches before polling readiness
        # Shell commands run after launching apps (e.g. start an anti-sleep session).
        # Each: a string, or {cmd, label}.
        "post_cmds": [],
    },
    "checks": {
        # label -> regex matched against the full process command line (pgrep -f).
        "apps": {},
        # MIDI input ports that MUST be present (substring match).
        "midi_required": [],
        "breath_port": "Breath Controller",     # breath controller's MIDI input name
        "default_output_match": "MacBook",      # macOS default output should be the Mac
        # Subnet the Mac must hold an IP on (example — set your own in rig.toml).
        "stage_network": "192.168.1",
        # USB devices that must be plugged: {label = product-name substring} (ioreg).
        "usb_devices": {},
        # Named hosts that must respond: {name, ip OR mac, severity?, icon?}.
        "hosts": [],
        # Remote links = an ESTABLISHED TCP connection on a port: {name, port, host?,
        # severity?, icon?}. (e.g. a remote device connecting to a network app.)
        "links": [],
        # Arbitrary command checks — the domain-open escape hatch. Each:
        # {name, cmd, expect_exit?=0, expect_match?, severity?, icon?, timeout?}.
        "commands": [],
        # Things the Mac can't detect → a human ticks them before playing. Each:
        # {name, icon?, severity? ("warn"/"fail" or {profile=sev})}. Unconfirmed = severity.
        "manual_confirms": [],
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
            "live_output": ["Piano"],              # the DAW's audio output device on stage
            "audio_interface": "Piano",            # macOS default OUTPUT must be this on stage (else error); omit → not checked
            "require_awake": True,
            "breath_severity": "fail",
            "mac_power_severity": "fail",
        },
        "studio": {
            "keyboard_ok": ["Piano"],
            "keyboard_warn": ["microKey"],
            "live_output": ["MacBook", "USB Audio"],
            "require_awake": False,
            "breath_severity": "warn",
            "mac_power_severity": "warn",
        },
    },
    "monitor": {
        "interval": 5,        # seconds between fast checks (apps + MIDI)
        "audio_every": 6,     # run the slow audio check once every N cycles
        "alerts": ["macos"],  # active backends: macos, push, midi
        "recovery_alerts": True,
    },
    # Optional automatic audio-level probe. A separate helper (see audiolevel/) that holds
    # the OS audio permission publishes "<rms> <epoch>" to `file`; the engine only READS it
    # — no platform-specific code here, any meter that writes that format works. When `file`
    # is empty or stale, the soundcheck falls back to the manual "I hear sound" confirm.
    "audiolevel": {
        "file": "",           # path the probe writes to (set in rig.toml to enable); empty = disabled
        "threshold": 0.003,   # RMS above this = sound is flowing
        "max_age": 6,         # seconds; a reading older than this is considered stale (probe down)
    },
    "alerts": {
        "push": {
            "server": "https://ntfy.sh",
            "topic": "",       # set a PRIVATE topic, e.g. "my-rig-9d3f", to enable
            "priority": "high",
        },
        "midi": {
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
        "app": "📦", "usb": "🎛️", "host": "📡", "link": "🔗", "cmd": "⚙️", "manual": "✋",
        "kbd:breath": "🌬️", "kbd": "🎹",
        "net:stage": "🌐", "net": "📱",
        "sys:vpn": "🔒", "sys:output": "💻", "sys:keepawake": "☕", "sys:macpower": "🔌",
        "audio:probe": "🎚️", "audio": "🔊", "kbd": "🎹", "midi": "🔌",
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
