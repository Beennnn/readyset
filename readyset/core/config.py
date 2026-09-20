"""Load the rig configuration.

Resolution order for the config file:
  1. $RIG_CONF if set
  2. rig.toml          (your real config — git-ignored, may hold a private ntfy topic)
  3. rig.example.toml  (committed template — used as-is if you never copy it)

Whatever file is found is merged over DEFAULTS below, so a partial rig.toml
only needs to override what differs from the template. Python 3.11+ reads TOML
natively via tomllib (no dependency).
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

# The repo root — three levels up from readyset/core/config.py. Exported because it is
# the anchor for every path the program resolves at runtime (rig.toml, logs/, the
# dashboard's page.html): computing `__file__.parent.parent…` in each module meant the
# count silently became wrong the day a module moved one directory deeper.
REPO_DIR = Path(__file__).resolve().parent.parent.parent

# Generic skeleton so the engine runs out of the box — NOTHING here is personal.
# Your real rig (gig .als path, network IPs, device names, lamp MACs, ntfy topic)
# lives in rig.toml, which is git-ignored and deep-merged over these defaults.
DEFAULTS: dict = {
    "set": {
        "ableton_app": "/Applications/Ableton Live 12 Suite.app",
        "project": "",                     # your gig .als — set in rig.toml
        "open_after_launch": True,
        # Fired once the set is loaded, to launch a scene. Empty port = disabled.
        # Two messages, in this order: a CC whose VALUE is the scene number, then a note
        # that triggers the selected scene. That pairing is not invented here — it is the
        # protocol the rig's own control surface already speaks, and reusing it means one
        # mapping in the DAW instead of two.
        "start_scene": {
            "port": "",
            "channel": 1,
            "select_cc": 2,      # its value carries the scene number
            "scene": 0,
            "trigger_note": 38,
            # The MIDI port appears when the DAW STARTS, not when the set has finished
            # loading — plugins and samples come after. Rather than guess a duration, we
            # WATCH: the DAW writes to its log while it loads and falls silent when done.
            # quiet_seconds of silence means ready; max_seconds caps the wait; and
            # delay_seconds is the fallback when no log can be found, where guessing is
            # all that is left.
            "log_glob": "",
            # Must EXCEED the interval of the chattiest periodic writer in the log,
            # otherwise the silence between two of its lines reads as the end of the
            # loading. Here a control script writes one every 5 s throughout the whole
            # start-up: 2 s would have fired inside a gap, on a set that was still
            # loading. Seven seconds leave the margin.
            "quiet_seconds": 7.0,
            "max_seconds": 25.0,
            "delay_seconds": 12,
        },
    },
    "launch": {
        # Standard app locations; override the list in rig.toml for your own rig.
        "apps": [
            "/Applications/Bome MIDI Translator Pro.app",
            "/Applications/Bome Network.app",
            "/Applications/Elgato Stream Deck.app",
        ],
        "settle_seconds": 2,   # grace after an app launches before polling readiness
        "amphetamine_session": True,   # start an anti-sleep session during bring-up
    },
    "server": {
        # The dashboard's listening interface. "127.0.0.1" = this machine only.
        # "0.0.0.0" opens it to the LOCAL network — that is what lets the phone publish
        # its state (POST /api/phone) and lets you consult the page from the stage.
        # ONLY do this on a trusted network: the server also exposes /api/fix,
        # /api/quit-apps, /api/windows and /api/preflight, which LAUNCH and CLOSE things
        # on the Mac. There is no authentication — the network's firewall is the only
        # barrier.
        "host": "127.0.0.1",
        # Past this, the phone's last report is considered stale and the line falls back
        # to « à confirmer ». One hour, not five minutes: the phone publishes on EVENT
        # (plugged in / unplugged), not on a regular heartbeat — « plugged in 20 min ago »
        # is still true, whereas a short window would have declared it stale without
        # anything having changed. The age is displayed on the line: that is what lets
        # you judge, not a threshold.
        # If the phone starts beating regularly (one publication every N minutes rather
        # than only on plug-in), lower this value to just above N: silence then becomes a
        # signal in itself — « the phone has said nothing since… », which points at the
        # phone and not at the socket. Nothing else to change: every publication is a
        # heartbeat.
        "phone_stale_seconds": 3600,
        # --- the battery's slope, which SERVES AS A REBUTTAL to the flag --------------
        # « charging » is an event: true at the instant of plugging in, and never
        # rechecked. A cable that gives out, a power strip switched off, a dead charger
        # change nothing about what was said — but the battery itself starts going
        # down. That is the only thing that can contradict the flag, and it does not
        # need a tight heartbeat: two points are enough to see a slope.
        "trend_window_seconds": 10800,     # we look no further back than 3 h
        # ONE per cent LOST is enough to conclude: a charging battery does not go back.
        # Hence a short minimum span — the time for one per cent to drop, a few minutes,
        # which is less than a soundcheck lasts. That is what makes the rebuttal usable
        # at the moment you need it.
        "trend_min_span_seconds": 180,
        # ESTIMATING THE RUNTIME LEFT takes far more hindsight than observing a drop: at
        # 1 % accuracy over 3 minutes, the slope is worth ±20 %/h and the estimate means
        # nothing. Below that span we say « it is going down », without quantifying how long.
        "trend_autonomy_span_seconds": 1800,
        # Below this, the phone will not last the evening: that is an error, not a
        # remark. Three hours = the time to arrive, to set up, to play.
        "autonomy_min_hours": 3.0,
        # Rate at which the Mac polls the phone itself (ideviceinfo).
        # 0 = do not poll. It is the only REGULAR heartbeat possible: an iOS automation
        # only fires on plug-in, whereas the battery's slope needs several points.
        # Without a pairing, the read returns None without a sound.
        "idevice_poll_seconds": 60,
        "idevice_bin": "",              # empty = looked up in PATH then in Homebrew
        # --- audio spectrum served on /api/audio/spectrum -----------------------------
        # The source is resolved by its NAME: avfoundation indices change from one
        # reboot to the next, and a frozen index would end up listening to the mic while
        # believing it listens to the mix. The capture starts on the first call and stops
        # by itself after `idle_stop` with no request — holding an audio device open
        # permanently on a stage machine is exactly what we want to avoid.
        "spectrum_source": "Wave Link Stream",
        "spectrum_bands": 16,
        "spectrum_idle_stop_seconds": 20,
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
        # Bome Network's TCP port; an ESTABLISHED connection on it = a remote (iPhone)
        # is connected. iphone_host, if set, requires the peer address to contain it.
        "bome_network_port": 37000,
        "iphone_host": "",
        # --- network (examples — set your own in rig.toml) ---
        "stage_network": "192.168.1",     # subnet the Mac must hold an IP on
        "modem_host": "192.168.1.1",      # stage router/modem to ping
        "studio_router": "192.168.1.1",   # if reachable → "auto" resolves to studio
        # Stream Decks by ioreg USB product name.
        "streamdecks": {"XL": "Stream Deck XL", "Plus": "Stream Deck Plus"},
        # Stage lamps (Tuya) — [{name="L1", mac="aa:bb:cc:dd:ee:ff"}, …] in rig.toml.
        "lamps": [],
        "lamp_severity": "warn",          # ambiance, not sound-critical
        # VPN: an active tunnel rewrites the machine's routing, so the Mac can stop
        # seeing the iPhone, the stage modem and the lamps. `ignore` lists VPNs you
        # accept (substring of the service name); `off_cmds` overrides how a given VPN
        # is cut (substring → shell command) when the generic method doesn't fit.
        "vpn": {"ignore": [], "off_cmds": {}},
        # Open apps the rig does not need (warn in live, info in studio). `allow` = never
        # offered for closing. The Finder is in there by default: macOS relaunches it.
        "unexpected_apps": {"allow": ["Finder"]},
    },
    # Window policy per app — everything RUNS, only Ableton is SEEN. See readyset/windows.py.
    "windows": {
        "default": "hide",          # hide | minimize | keep
        "apps": {"Ableton": "keep"},
        "launch_hidden": True,      # bring apps up already hidden (`open -g -j`)
        "after_preflight": True,    # tidy the screen at the end of a bring-up
    },
    # Two rigs, one tool. Start "live"; when the studio router is reachable, "auto"
    # resolves to "studio". The dashboard slider forces it; CLI: --mode live|studio|auto.
    "mode": {"default": "auto"},
    "modes": {
        "live": {
            # keyboard_ok present → green; only keyboard_warn present → yellow; none → red.
            "keyboard_ok": ["Piano"],              # your main keyboard's MIDI port name
            "keyboard_warn": [],
            "live_output": ["Piano"],              # Ableton's audio output device on stage
            # HISTORICAL setting, kept for the rig.toml files that only know this one: it
            # can only say « checked » or « not checked at all ». The current form is
            # `amphetamine_severity` (fail | warn | info | off) — see checks.py.
            "require_amphetamine": True,
            "breath_severity": "fail",
            "interface_severity": "fail",
            "mac_power_severity": "fail",
            "iphone_power_severity": "fail",
            "unexpected_apps_severity": "warn",   # on stage, every extra app is a risk
        },
        "studio": {
            "keyboard_ok": ["Piano"],
            "keyboard_warn": ["microKey"],
            "live_output": ["MacBook", "USB Audio"],
            "require_amphetamine": False,   # historical — cf. amphetamine_severity
            "breath_severity": "warn",
            "interface_severity": "warn",
            "mac_power_severity": "warn",
            "iphone_power_severity": "warn",
            "unexpected_apps_severity": "info",   # at the desk it's just a fact
        },
    },
    "monitor": {
        "interval": 5,        # seconds between fast checks (apps + MIDI)
        "audio_every": 6,     # run the slow audio check once every N cycles
        "alerts": ["macos", "push", "midi"],   # backends: macos, push, midi
        "recovery_alerts": True,
    },
    "alerts": {
        "push": {
            "server": "https://ntfy.sh",
            "topic": "",       # set a PRIVATE topic, e.g. "my-rig-9d3f", to enable
            "priority": "high",
            # Skip TLS verification — for a LAN that intercepts HTTPS with a self-signed
            # certificate. Off by default: the payload is only rig status, but turning
            # this on should be a decision, not an inheritance.
            "insecure": False,
        },
        "midi": {
            # Dedicated dead-end IAC port so the alert never hits the live routing.
            # Create it: Audio MIDI Setup → IAC Driver → "+" → rename to "rig-alert".
            "port": "rig-alert",
            "channel": 15,     # 1-16 (kept off the musical channels)
            "cc": 111,         # total on this CC, one family per CC above it
        },
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
        p = REPO_DIR / name
        if p.exists():
            return p
    return None


# Sections whose key set is FIXED: anything else in them is a typo or a stale name.
# Deliberately a short list — [checks].apps, [modes] and [diagram] hold user-invented
# keys, and flagging those would cry wolf until nobody reads the warnings any more.
CLOSED_SECTIONS = ("set", "launch", "server", "monitor")


def unknown_keys(user: dict) -> list[str]:
    """Keys the engine will silently ignore, as dotted paths.

    Exists because of a real, expensive bug: [set].ableton_app was written « app »
    in rig.toml, so nobody read it, the engine fell back to its default — another
    Ableton install — and the dashboard's fix launched a SECOND Live next to the
    one already running. A key that does nothing looks exactly like a key that
    works, which is why it has to be said out loud.
    """
    out = []
    for section in CLOSED_SECTIONS:
        for k in user.get(section, {}):
            if k not in DEFAULTS.get(section, {}):
                out.append(f"{section}.{k}")
    for sub in user.get("alerts", {}):
        if isinstance(user["alerts"].get(sub), dict) and sub in DEFAULTS["alerts"]:
            for k in user["alerts"][sub]:
                if k not in DEFAULTS["alerts"][sub]:
                    out.append(f"alerts.{sub}.{k}")
    return out


def load() -> dict:
    path = config_path()
    if path and path.exists():
        with path.open("rb") as fh:
            user = tomllib.load(fh)
        for k in unknown_keys(user):
            print(f"⚠️  rig.toml : « {k} » n'est lu par personne — faute de frappe ou "
                  f"nom périmé ? La valeur par défaut s'applique.", file=sys.stderr)
        return _deep_merge(DEFAULTS, user)
    return DEFAULTS
