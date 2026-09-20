"""Checks on the HARDWARE — decks, lamps, and the Mac's own power.

What these share is that nothing on the machine can fix them: a deck that is not plugged
in stays not plugged in. So they report precisely, and they never offer a button.

The drawings that illustrate them are not here — they are a catalogue, in
readyset/devices/.
"""

from __future__ import annotations

import json
import subprocess

from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint
# A lamp is reached by ping like anything else on the LAN — the primitive lives with the
# network checks rather than being written twice.
from .network import _ping


def _streamdeck_specs(cfg: dict) -> list[dict]:
    """Normalise both config shapes into [{name, serial, products}].

    Legacy shape (still accepted): streamdecks = { XL = "Stream Deck XL", … } — one
    product-name substring per deck. Rich shape: a list of [[checks.streamdecks]]
    tables carrying a serial AND one or more product names.
    """
    raw = cfg["checks"].get("streamdecks", {"XL": "Stream Deck XL", "Plus": "Stream Deck +"})
    if isinstance(raw, dict):
        return [{"name": k, "serial": "", "products": [v]} for k, v in raw.items()]
    specs = []
    for deck in raw:
        products = deck.get("product", [])
        if isinstance(products, str):
            products = [products]
        specs.append({"name": deck.get("name", "?"),
                      "serial": deck.get("serial", ""),
                      "products": products})
    return specs


def check_streamdeck(cfg: dict) -> list[Result]:
    """Each Stream Deck must be present on USB (via ioreg — SPUSBDataType is empty on
    this Mac). 'Asleep' (dimmed screen) is an app-internal state we can't read.

    A deck matches on its USB **serial** first, product name second, and passes if
    EITHER hits. Why not product name alone (the pre-2026-08-17 criterion): the
    marketing string ioreg exposes isn't stable — the Plus enumerates as "Stream Deck +"
    on some firmwares, so a lone "Stream Deck Plus" substring reports a genuinely
    plugged deck as absent. The serial never changes; read it off the Stream Deck app's
    own device id in any profile manifest — `"UUID": "@(1)[vid/pid/SERIAL]"`.

    Never match the bare string "Stream Deck": the app opens every USB device on the
    bus, leaving AppleUSBHostDeviceUserClient nodes *named* "Stream Deck" hanging off
    unrelated hardware (the Dell dock, hubs…). They're there with no deck plugged at
    all — matching them would turn this check into a permanent green light. Quoting the
    needle (`"Stream Deck XL"`) is what keeps us on the `= "…"` property values.
    """
    try:
        # -l so idVendor/idProduct/kUSBSerialNumberString are printed, not just names.
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception as exc:
        return [Result("usb:streamdeck", "Stream Deck (USB)", WARN, f"ioreg: {exc}")]
    res = []
    for spec in _streamdeck_specs(cfg):
        label, serial = spec["name"], spec["serial"]
        hit = f"n° série {serial}" if serial and f'"{serial}"' in out else ""
        if not hit:
            hit = next((p for p in spec["products"] if f'"{p}"' in out), "")
        res.append(Result(f"usb:{label}", f"Stream Deck {label}",
                          OK if hit else FAIL,
                          f"branché ({hit})" if hit else
                          _hint("non détecté en USB",
                                "le brancher en USB ; s'il passe par le dock, "
                                "vérifier que le dock est alimenté")))
    return res


def _ip_for_mac(mac: str) -> str | None:
    """Resolve a device's current IP from its MAC via the ARP table (the lamps get
    reserved-but-variable DHCP addresses, so MAC is the stable key)."""
    target = mac.lower().replace("-", ":")
    target = ":".join(p.zfill(2) for p in target.split(":"))
    try:
        out = subprocess.run(["arp", "-a", "-n"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "(" not in line or ")" not in line:
            continue
        ip = line.split("(", 1)[1].split(")", 1)[0]
        m = line.split(" at ", 1)[1].split()[0] if " at " in line else ""
        norm = ":".join(p.zfill(2) for p in m.lower().split(":")) if m else ""
        if norm == target:
            return ip
    return None


def check_lamps(cfg: dict, mode: str) -> list[Result]:
    """Stage lamps L1/L2 (Tuya). Found by MAC in the ARP table, then pinged. Warn-level
    (ambiance, not sound-critical); ARP only sees them once they've talked on the net."""
    sev = cfg["checks"].get("lamp_severity", WARN)
    if sev == OFF:
        return []
    # A single line for all the lamps: they come on together, go off together and are
    # fixed with the same gesture. One line per lamp repeated the same fact twice — and
    # with four lamps the table would talk about nothing else. The detail NAMES them all
    # the same: the name is what is missing when only one of them drops.
    up, silent, missing = [], [], []
    for lamp in cfg["checks"].get("lamps", []):
        name = lamp.get("name", "?")
        ip = _ip_for_mac(lamp.get("mac", ""))
        if ip and _ping(ip):
            up.append(f"{name} ({ip})")
        elif ip:
            silent.append(f"{name} ({ip})")
        else:
            missing.append(name)
    if not (up or silent or missing):
        return []
    label = "Lampes de scène"
    if not silent and not missing:
        return [Result("lamp:all", label, OK, "connectées : " + ", ".join(up))]
    constat = []
    if up:
        constat.append("connectée(s) : " + ", ".join(up))
    if silent:
        constat.append("adresse prise mais muette(s) : " + ", ".join(silent))
    if missing:
        constat.append("introuvable(s) : " + ", ".join(missing))
    return [Result("lamp:all", label, sev,
                   _hint(" · ".join(constat),
                         "les allumer et vérifier qu'elles sont visibles sur le réseau"))]


def _instant_amperage() -> int | None:
    try:
        out = subprocess.run(["ioreg", "-rn", "AppleSmartBattery"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if '"InstantAmperage"' in line:
            try:
                v = int(line.split("=")[1].strip())
                if v > 2**63:          # some Macs report unsigned; fold to signed
                    v -= 2**64
                return v
            except Exception:
                return None
    return None


def check_mac_power(cfg: dict, mode: str) -> Result:
    """Mac must be on AC. Unplugged → mode-based (fail live / warn studio). Plugged BUT
    the battery is draining (load > adapter) → always FAIL ('draining even while charging')."""
    sev = cfg["modes"][mode].get("mac_power_severity", "warn")
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("sys:macpower", "Alimentation Mac", WARN, f"pmset: {exc}")
    import re
    mp = re.search(r"(\d+%)", out)
    pct = mp.group(1) if mp else "?"
    plugged = "'AC Power'" in out
    draining = "discharging" in out.lower()
    if plugged and not draining:
        amp = _instant_amperage()
        if amp is not None and amp < 0:
            draining = True
    if not plugged:
        return Result("sys:macpower", "Alimentation Mac", sev, f"sur batterie ({pct})")
    if draining:
        return Result("sys:macpower", "Alimentation Mac", FAIL, f"branché mais se décharge ! ({pct})")
    return Result("sys:macpower", "Alimentation Mac", OK, f"branché ({pct})")
