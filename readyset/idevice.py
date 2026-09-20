"""Read the iPhone's battery FROM the Mac, through libimobiledevice.

The Mac knows nothing about the phone by itself — measured on 2026-08-19: Bluetooth only
exposes its address and its signal strength (no battery field, unlike the AirPods),
`pmset -g accps` only knows about the internal battery, and the Bome link proves the
phone is REACHABLE, not that it is plugged in — an iPhone on Wi-Fi and unplugged holds
that link while it drains.

`ideviceinfo` is the only path that gives the level AND the power state without asking
the phone for anything. That is what makes it precious: it can BEAT regularly, where an
iOS automation only fires on plug-in. The beat is what keeps the battery slope alive
(readyset/checks/), hence the only possible rebuttal of a "charging" that has become
false.

Prerequisites, once and for all: iPhone plugged in over USB, unlocked, "Trust", then
"Show this iPhone when on Wi-Fi" in the Finder. Without that everything here returns
None — this is a SUPPLEMENT, never a prerequisite: the phone can always publish its own
state itself (POST /api/phone), and both sources feed the same history.
"""

from __future__ import annotations

import shutil
import subprocess

BATTERY_DOMAIN = "com.apple.mobile.battery"

# Looked up explicitly, and not only in the PATH: under launchd the latter is limited to
# /usr/bin:/bin:/usr/sbin:/sbin, where Homebrew is not. A `which` would fail there while
# the command works perfectly in a terminal — and the dashboard would stay blind without
# ever saying why.
_CANDIDATES = ("/opt/homebrew/bin/ideviceinfo", "/usr/local/bin/ideviceinfo")


def binary(cfg: dict) -> str | None:
    explicit = str(cfg.get("server", {}).get("idevice_bin", "")).strip()
    if explicit:
        return explicit if shutil.which(explicit) or explicit.startswith("/") else None
    return shutil.which("ideviceinfo") or next((p for p in _CANDIDATES if shutil.which(p)), None)


def _run(exe: str, args: list[str]) -> dict[str, str] | None:
    """One read of the battery domain → {key: value}, or None if the device is absent."""
    try:
        p = subprocess.run([exe, *args, "-q", BATTERY_DOMAIN],
                           capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    out = {}
    for line in p.stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out or None


def read(cfg: dict) -> dict | None:
    """{"battery": int, "charging": bool, "via": "usb"|"wifi"} — None if nothing to read.

    USB first, network second: when the cable is there, it always answers, whereas the
    network read is reported as intermittent upstream (libimobiledevice#947). We do not
    want a reliable path pushed aside by an uncertain one.

    "Plugged in" is read from ExternalConnected, not from BatteryIsCharging: at 100 % a
    phone on mains power stops charging, and taking the second one would make the line
    blink red precisely when everything is fine.
    """
    exe = binary(cfg)
    if not exe:
        return None
    for via, args in (("usb", []), ("wifi", ["-n"])):
        d = _run(exe, args)
        if not d:
            continue
        try:
            level = int(d.get("BatteryCurrentCapacity", ""))
        except ValueError:
            continue
        plugged = d.get("ExternalConnected", d.get("BatteryIsCharging", "")).lower() == "true"
        return {"battery": max(0, min(100, level)), "charging": plugged, "via": via}
    return None
