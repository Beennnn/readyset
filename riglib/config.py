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
        "amphetamine_session": True,   # start an anti-sleep session during bring-up
    },
    "server": {
        # Interface d'écoute du dashboard. "127.0.0.1" = cette machine seulement.
        # "0.0.0.0" l'ouvre au réseau LOCAL — c'est ce qui permet au téléphone de
        # publier son état (POST /api/phone) et de consulter la page depuis la scène.
        # À ne faire QUE sur un réseau de confiance : le serveur expose aussi /api/fix,
        # /api/quit-apps, /api/windows et /api/preflight, qui LANCENT et FERMENT des
        # choses sur le Mac. Il n'y a pas d'authentification — le pare-feu du réseau
        # est la seule barrière.
        "host": "127.0.0.1",
        # Au-delà, le dernier rapport du téléphone est considéré périmé et la ligne
        # revient à « à confirmer ». Une heure, pas cinq minutes : le téléphone publie
        # sur ÉVÉNEMENT (branché / débranché), pas en battement régulier — « branché il
        # y a 20 min » reste vrai, alors qu'une fenêtre courte l'aurait déclaré périmé
        # sans que rien n'ait changé. L'âge est affiché sur la ligne : c'est lui qui
        # permet de juger, pas un seuil.
        # Si le téléphone se met à battre régulièrement (une publication toutes les N
        # minutes plutôt qu'au seul branchement), descendre cette valeur juste au-dessus
        # de N : le silence devient alors un signal en soi — « le téléphone n'a rien dit
        # depuis… », qui désigne le téléphone et non la prise. Rien d'autre à changer :
        # toute publication est un battement.
        "phone_stale_seconds": 3600,
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
        # Apps ouvertes dont le rig n'a pas besoin (warn en live, info en studio). `allow`
        # = jamais proposées à la fermeture. Le Finder y est d'office : macOS le relance.
        "unexpected_apps": {"allow": ["Finder"]},
    },
    # Window policy per app — everything RUNS, only Ableton is SEEN. See riglib/windows.py.
    "windows": {
        "default": "hide",          # hide | minimize | keep
        "apps": {"Ableton": "keep"},
        "launch_hidden": True,      # bring apps up already hidden (`open -g -j`)
        "after_preflight": True,    # tidy the screen at the end of a bring-up
    },
    # Two rigs, one tool. Start "live"; when the studio router is reachable, "auto"
    # resolves to "studio". The dashboard tirette forces it; CLI: --mode live|studio|auto.
    "mode": {"default": "auto"},
    "modes": {
        "live": {
            # keyboard_ok present → green; only keyboard_warn present → yellow; none → red.
            "keyboard_ok": ["Piano"],              # your main keyboard's MIDI port name
            "keyboard_warn": [],
            "live_output": ["Piano"],              # Ableton's audio output device on stage
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
            "require_amphetamine": False,
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
