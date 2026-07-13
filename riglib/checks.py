"""Health checks — the source of truth for both preflight and monitor.

Each check returns a Result. Three levels:
  ok    green  — present / running as expected
  warn  yellow — optional thing missing (e.g. keyboard unplugged); not blocking
  fail  red    — required thing missing; the rig is not gig-ready

The checks are cheap (pgrep, an in-process MIDI port enumeration) except audio,
which shells out to system_profiler (~1s) and is therefore rate-limited by the
monitor loop rather than run every cycle.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from dataclasses import dataclass

import mido

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


@dataclass
class Result:
    key: str        # stable id for state tracking (e.g. "app:Ableton")
    label: str      # human label
    status: str     # ok | warn | fail
    detail: str = ""

    @property
    def icon(self) -> str:
        return _ICON[self.status]

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label,
                "status": self.status, "detail": self.detail}


def _pgrep(pattern: str) -> bool:
    # -f matches the full command line; pattern is an extended regex.
    return subprocess.run(
        ["pgrep", "-f", pattern],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def check_apps(cfg: dict) -> list[Result]:
    out = []
    for label, pattern in cfg["checks"]["apps"].items():
        running = _pgrep(pattern)
        out.append(Result(
            key=f"app:{label}", label=f"{label} lancé",
            status=OK if running else FAIL,
            detail="" if running else "process introuvable",
        ))
    return out


def _midi_inputs() -> list[str]:
    from .midi_lock import MIDI_LOCK
    try:
        with MIDI_LOCK:
            return list(mido.get_input_names())
    except Exception as exc:  # pragma: no cover - backend init failure
        return [f"__error__:{exc}"]


def check_midi(cfg: dict) -> list[Result]:
    ports = _midi_inputs()
    if ports and ports[0].startswith("__error__:"):
        return [Result("midi:backend", "Backend MIDI", FAIL, ports[0].split(":", 1)[1])]

    def present(name: str) -> bool:
        return any(name.lower() in p.lower() for p in ports)

    out = []
    for name in cfg["checks"]["midi_required"]:
        hit = present(name)
        out.append(Result(
            key=f"midi:{name}", label=f"Port MIDI « {name} »",
            status=OK if hit else FAIL,
            detail="" if hit else "absent",
        ))
    return out


def _present(substr: str) -> str | None:
    """Return the first MIDI input name containing substr (case-insensitive), else None."""
    for p in _midi_inputs():
        if not p.startswith("__error__:") and substr.lower() in p.lower():
            return p
    return None


def check_keyboard(cfg: dict, mode: str) -> Result:
    """Three tiers: a preferred keyboard (keyboard_ok, e.g. Digital Piano / P-225) →
    green; only a fallback (keyboard_warn, e.g. microKey Air) → yellow; none → red.
    So in the studio microKey alone works but warns; the P-225 always satisfies."""
    m = cfg["modes"][mode]
    ok_list = m.get("keyboard_ok", m.get("keyboard", []))
    warn_list = m.get("keyboard_warn", [])
    found_ok = next((_present(a) for a in ok_list if _present(a)), None)
    if found_ok:
        return Result("kbd:keyboard", "Clavier", OK, f"détecté : {found_ok}")
    found_warn = next((_present(a) for a in warn_list if _present(a)), None)
    if found_warn:
        return Result("kbd:keyboard", "Clavier", WARN,
                      f"{found_warn} (clavier principal absent)")
    return Result("kbd:keyboard", "Clavier", FAIL, "aucun clavier branché")


def check_breath(cfg: dict, mode: str) -> Result:
    """Breath controller. Severity depends on mode: part of the live rig (fail),
    optional at the desk (warn)."""
    sev = cfg["modes"][mode].get("breath_severity", "warn")
    found = _present(cfg["checks"].get("breath_port", "Breath Controller"))
    if found:
        return Result("kbd:breath", "Breath controller", OK, f"détecté : {found}")
    return Result("kbd:breath", "Breath controller", sev,
                  "absent" if sev == FAIL else "absent (optionnel en studio)")


def check_amphetamine(cfg: dict) -> Result:
    """Amphetamine must be running AND holding an active anti-sleep session. A live
    session shows up as an '(Amphetamine)' power assertion in `pmset -g assertions`."""
    if not _pgrep("Amphetamine.app/Contents/MacOS/Amphetamine"):
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", FAIL, "pas lancé")
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", WARN, f"pmset: {exc}")
    active = "(Amphetamine)" in out
    return Result("sys:amphetamine", "Amphetamine (anti-veille)",
                  OK if active else FAIL,
                  "session active" if active else "lancé, aucune session active")


def _tail(path: str, nbytes: int = 200_000) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
        return fh.read().decode("utf-8", "ignore")


def check_live_output(cfg: dict, mode: str) -> Result:
    """Ableton Live's audio output device, read from its Log.txt. Acceptable devices
    depend on mode: the P-225 on stage; the macOS output or the RME in the studio."""
    wants = cfg["modes"][mode]["live_output"]
    label = "Sortie de Live → " + " / ".join(wants)
    logs = sorted(
        glob.glob(os.path.expanduser("~/Library/Preferences/Ableton/Live*/Log.txt")),
        key=lambda p: os.path.getmtime(p), reverse=True,
    )
    if not logs:
        return Result("audio:live", label, WARN, "log Ableton introuvable")
    dev = None
    try:
        for line in _tail(logs[0]).splitlines():
            if "Audio In Out: Output Device:" in line:
                dev = line.split("Output Device:", 1)[1].strip()
    except Exception as exc:
        return Result("audio:live", label, WARN, f"lecture log: {exc}")
    if dev is None:
        return Result("audio:live", label, WARN, "indéterminée")
    short = dev.split(" (")[0]
    ok = any(w.lower() in dev.lower() for w in wants)
    return Result("audio:live", label, OK if ok else FAIL,
                  short if ok else f"actuellement : {short}")


def _ping(host: str, timeout_s: int = 1) -> bool:
    try:
        return subprocess.run(["ping", "-c1", f"-t{timeout_s}", host],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout_s + 2).returncode == 0
    except Exception:
        return False


def resolve_mode(cfg: dict, requested: str = "auto") -> str:
    """live | studio | auto → concrete mode. Auto = studio when the studio router
    (192.168.1.1) answers, else live. So the tool boots live and flips to studio
    only once it sees the home network."""
    if requested in ("live", "studio"):
        return requested
    return "studio" if _ping(cfg["checks"].get("studio_router", "192.168.1.1")) else "live"


def check_stage_network(cfg: dict) -> Result:
    prefix = cfg["checks"].get("stage_network", "192.168.1")
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("net:stage", f"Mac sur le réseau {prefix}.x", WARN, f"ifconfig: {exc}")
    ip = next((w for line in out.splitlines() if f"inet {prefix}." in line
               for w in line.split() if w.startswith(f"{prefix}.")), None)
    return Result("net:stage", f"Mac sur le réseau {prefix}.x",
                  OK if ip else FAIL, ip or "pas d'IP sur ce subnet")


def check_modem(cfg: dict) -> Result:
    host = cfg["checks"].get("modem_host", "192.168.1.1")
    up = _ping(host)
    return Result("net:modem", f"Modem « {host} »", OK if up else FAIL,
                  "répond" if up else "pas de réponse")


def check_streamdeck(cfg: dict) -> list[Result]:
    """Each Stream Deck must be present on USB (via ioreg — SPUSBDataType is empty on
    this Mac). 'Asleep' (dimmed screen) is an app-internal state we can't read."""
    decks = cfg["checks"].get("streamdecks", {"XL": "Stream Deck XL", "Plus": "Stream Deck Plus"})
    try:
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception as exc:
        return [Result("usb:streamdeck", "Stream Deck (USB)", WARN, f"ioreg: {exc}")]
    res = []
    for label, product in decks.items():
        present = product in out
        res.append(Result(f"usb:{label}", f"Stream Deck {label}",
                          OK if present else FAIL,
                          "branché" if present else "non détecté en USB"))
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
    res = []
    for lamp in cfg["checks"].get("lamps", []):
        name = lamp.get("name", "?")
        ip = _ip_for_mac(lamp.get("mac", ""))
        if ip and _ping(ip):
            res.append(Result(f"lamp:{name}", f"Lampe {name}", OK, f"connectée ({ip})"))
        elif ip:
            res.append(Result(f"lamp:{name}", f"Lampe {name}", sev, f"vue ({ip}) mais ne répond pas"))
        else:
            res.append(Result(f"lamp:{name}", f"Lampe {name}", sev, "introuvable (éteinte ?)"))
    return res


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
    the battery is draining (load > adapter) → always FAIL ('se vide même en charge')."""
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


def check_iphone_charge(cfg: dict, mode: str, acked: bool = False) -> Result:
    """iPhone charging — NOT detectable from the Mac (the iPhone charges on a separate
    charger and talks to Bome over Wi-Fi, so it never appears here). Manual confirm:
    tick it before playing. Unconfirmed → fail live / warn studio."""
    sev = cfg["modes"][mode].get("iphone_power_severity", "warn")
    if acked:
        return Result("sys:iphonecharge", "iPhone en charge", OK, "confirmé manuellement")
    return Result("sys:iphonecharge", "iPhone en charge", sev, "à confirmer (non détectable)")


def check_bome_iphone(cfg: dict) -> Result:
    """Detect the Bome Network ↔ iPhone link via an ESTABLISHED TCP connection on
    Bome Network's port (37000). The iPhone runs Bome Network and connects here."""
    port = cfg["checks"].get("bome_network_port", 37000)
    host = str(cfg["checks"].get("iphone_host", "")).strip()
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=6,
        ).stdout
    except Exception as exc:
        return Result("net:iphone", "Bome Network ↔ iPhone", FAIL, f"lsof: {exc}")

    lines = [l for l in out.splitlines() if "ESTABLISHED" in l]
    if host:
        lines = [l for l in lines if host in l]
    if not lines:
        return Result("net:iphone", "Bome Network ↔ iPhone", FAIL,
                      "aucune connexion (iPhone déconnecté ?)")
    # NAME column looks like "192.168.1.10:37000->192.168.1.20:52345 (ESTABLISHED)"
    peer = ""
    for tok in lines[0].split():
        if "->" in tok:
            peer = tok.split("->", 1)[1]
            break
    return Result("net:iphone", "Bome Network ↔ iPhone", OK,
                  f"connecté{f' ({peer})' if peer else ''}")


# system_profiler is slow (~1s); cache its JSON so audio + default-output checks
# (and a polling dashboard) share one call instead of shelling out repeatedly.
_profile_cache: dict = {"ts": 0.0, "data": None}
_PROFILE_TTL = 10.0


def _audio_items() -> list[dict]:
    now = time.time()
    if _profile_cache["data"] is None or now - _profile_cache["ts"] > _PROFILE_TTL:
        try:
            out = subprocess.run(
                ["system_profiler", "SPAudioDataType", "-json"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            _profile_cache["data"] = json.loads(out)
        except Exception:
            _profile_cache["data"] = {}
        _profile_cache["ts"] = now
    data = _profile_cache["data"] or {}
    return [it for top in data.get("SPAudioDataType", []) for it in top.get("_items", [])]


def check_audio(cfg: dict, mode: str = "live") -> Result:
    want = cfg["checks"]["audio_interface"]
    sev = cfg["modes"].get(mode, {}).get("interface_severity", "fail")
    names = [it.get("_name", "") for it in _audio_items()]
    hit = any(want.lower() in n.lower() for n in names)
    return Result(
        key="audio", label=f"Interface audio « {want} »",
        status=OK if hit else sev,
        detail="" if hit else ("non détectée" if sev == FAIL else "non détectée (OK en studio)"),
    )


def check_default_output(cfg: dict) -> Result:
    """The macOS default sound output must be the Mac itself (built-in), not an
    external / AirPlay / conferencing device."""
    want = cfg["checks"].get("default_output_match", "MacBook")
    name = None
    for it in _audio_items():
        if it.get("coreaudio_default_audio_output_device") == "spaudio_yes":
            name = it.get("_name", "")
            break
    if name is None:
        return Result("sys:output", "Sortie son par défaut (Mac)", WARN, "indéterminée")
    ok = want.lower() in name.lower()
    return Result(
        key="sys:output", label="Sortie son par défaut (Mac)",
        status=OK if ok else FAIL,
        detail=f"actuellement : {name}" if not ok else name,
    )


# PPP:Modem entries here are serial gadgets (ToneX pedal, Seeed boards), not VPNs —
# a VPN is a *connected* service that isn't one of those serial modems.
def check_vpn(cfg: dict) -> Result:
    try:
        out = subprocess.run(["scutil", "--nc", "list"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("sys:vpn", "VPN inactif", WARN, f"scutil: {exc}")
    active = [l for l in out.splitlines()
              if "(Connected)" in l and "[PPP:Modem]" not in l]
    if not active:
        return Result("sys:vpn", "VPN inactif", OK, "")
    name = ""
    if '"' in active[0]:
        name = active[0].split('"')[1]
    return Result("sys:vpn", "VPN inactif", FAIL, f"VPN actif : {name}".rstrip(" :"))


def run_all(cfg: dict, mode: str = "live", with_audio: bool = True,
            manual: dict | None = None) -> list[Result]:
    manual = manual or {}
    m = cfg["modes"][mode]
    results = check_apps(cfg) + check_streamdeck(cfg) + check_midi(cfg)
    results += [check_keyboard(cfg, mode), check_breath(cfg, mode)]
    results += [check_stage_network(cfg), check_modem(cfg)]
    results += check_lamps(cfg, mode)
    results += [check_bome_iphone(cfg), check_vpn(cfg)]
    results += [check_mac_power(cfg, mode),
                check_iphone_charge(cfg, mode, acked=bool(manual.get("iphone_charge")))]
    if m.get("require_amphetamine", True):
        results.append(check_amphetamine(cfg))
    if with_audio:
        results += [check_default_output(cfg), check_audio(cfg, mode),
                    check_live_output(cfg, mode)]
    return results


def worst(results: list[Result]) -> str:
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status == WARN for r in results):
        return WARN
    return OK
