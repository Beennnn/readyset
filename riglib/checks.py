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
    glyph: str = ""  # optional per-item icon (e.g. from config); "" = dashboard derives it

    @property
    def icon(self) -> str:
        return _ICON[self.status]

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "status": self.status,
                "detail": self.detail, "glyph": self.glyph}


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
    """Master keyboard, three tiers: a preferred keyboard (keyboard_ok) present → green;
    only a fallback (keyboard_warn) present → yellow; none → red. Both lists are MIDI
    input-port name substrings, per mode."""
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


def check_keepawake(cfg: dict) -> Result | None:
    """A configurable keep-awake app holding a power assertion (an anti-sleep utility).
    Config `keepawake`: {process, owner, label, icon}. `owner` is the name that shows
    in `pmset -g assertions`. Returns None if not configured. Domain-agnostic."""
    ka = cfg["checks"].get("keepawake")
    if not ka:
        return None
    label = ka.get("label", "Anti-veille")
    icon = ka.get("icon", "")
    if ka.get("process") and not _pgrep(ka["process"]):
        return Result("sys:keepawake", label, FAIL, "pas lancé", icon)
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("sys:keepawake", label, WARN, f"pmset: {exc}", icon)
    owner = ka.get("owner", "")
    active = bool(owner) and f"({owner})" in out
    return Result("sys:keepawake", label, OK if active else FAIL,
                  "session active" if active else "lancé, aucune session active", icon)


def _tail(path: str, nbytes: int = 200_000) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
        return fh.read().decode("utf-8", "ignore")


def check_output_probe(cfg: dict, mode: str) -> Result | None:
    """Read the current value of something from an app's log and check it's in the
    mode's allowed set. Generic 'value from a log' probe — config `output_probe`:
    {log_glob, pattern (1 capture group), label, icon}; allowed = mode.live_output.
    Returns None if not configured."""
    op = cfg["checks"].get("output_probe")
    if not op:
        return None
    import re as _re
    allowed = cfg["modes"][mode].get("live_output", [])
    label = op.get("label", "Sortie") + " → " + " / ".join(allowed)
    icon = op.get("icon", "")
    logs = sorted(glob.glob(os.path.expanduser(op.get("log_glob", ""))),
                  key=lambda p: os.path.getmtime(p), reverse=True)
    if not logs:
        return Result("audio:probe", label, WARN, "log introuvable", icon)
    pat = _re.compile(op.get("pattern", "(.+)"))
    val = None
    try:
        for line in _tail(logs[0]).splitlines():
            m = pat.search(line)
            if m:
                val = m.group(1).strip()
    except Exception as exc:
        return Result("audio:probe", label, WARN, f"lecture log: {exc}", icon)
    if val is None:
        return Result("audio:probe", label, WARN, "indéterminée", icon)
    short = val.split(" (")[0]
    ok = any(a.lower() in val.lower() for a in allowed)
    return Result("audio:probe", label, OK if ok else FAIL,
                  short if ok else f"actuellement : {short}", icon)


def _ping(host: str, timeout_s: int = 1) -> bool:
    try:
        return subprocess.run(["ping", "-c1", f"-t{timeout_s}", host],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout_s + 2).returncode == 0
    except Exception:
        return False


def _criterion_holds(rule: dict) -> bool:
    """One environment-detection criterion. Generic: reachable host (ping), the Mac
    holding an IP on a subnet (interface), or an arbitrary command exiting 0 (cmd)."""
    if rule.get("ping"):
        return _ping(rule["ping"])
    if rule.get("interface"):
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            return f"inet {rule['interface']}." in out
        except Exception:
            return False
    if rule.get("cmd"):
        try:
            return subprocess.run(["/bin/bash", "-lc", rule["cmd"]],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=rule.get("timeout", 6)).returncode == 0
        except Exception:
            return False
    return False


def resolve_mode(cfg: dict, requested: str = "auto") -> str:
    """Pick the active profile. An explicit request wins. Otherwise the environment is
    detected: config [mode].detect is an ordered list of {profile, <criterion>}; the
    first rule whose criterion holds wins, else [mode].fallback (else the first mode).
    Fully config-driven — no hardcoded network or profile names."""
    modes = cfg.get("modes", {})
    if requested in modes:
        return requested
    md = cfg.get("mode", {})
    for rule in md.get("detect", []):
        prof = rule.get("profile")
        if prof in modes and _criterion_holds(rule):
            return prof
    fb = md.get("fallback")
    return fb if fb in modes else next(iter(modes), "live")


def check_stage_network(cfg: dict, mode: str = "") -> Result:
    # Per-mode override: modes.<mode>.stage_network wins over the global checks.stage_network.
    # Lets "live" test the stage subnet (192.168.8.x) and "studio" the home one (192.168.1.x).
    m = cfg.get("modes", {}).get(mode, {})
    prefix = m.get("stage_network") or cfg["checks"].get("stage_network", "192.168.1")
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("net:stage", f"Mac sur le réseau {prefix}.x", WARN, f"ifconfig: {exc}")
    ip = next((w for line in out.splitlines() if f"inet {prefix}." in line
               for w in line.split() if w.startswith(f"{prefix}.")), None)
    return Result("net:stage", f"Mac sur le réseau {prefix}.x",
                  OK if ip else FAIL, ip or "pas d'IP sur ce subnet")


def check_usb(cfg: dict) -> list[Result]:
    """USB devices that must be plugged, matched by ioreg product-name substring
    (SPUSBDataType is empty on some Macs). Domain-agnostic: config `usb_devices` maps
    a label to a product substring."""
    devices = cfg["checks"].get("usb_devices", {})
    if not devices:
        return []
    try:
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception as exc:
        return [Result("usb:_backend", "USB", WARN, f"ioreg: {exc}")]
    res = []
    for label, product in devices.items():
        present = product in out
        res.append(Result(f"usb:{label}", label, OK if present else FAIL,
                          "branché" if present else "non détecté en USB"))
    return res


def _ip_for_mac(mac: str) -> str | None:
    """Resolve a host's current IP from its MAC via the ARP table (for devices on
    reserved-but-variable DHCP addresses, the MAC is the stable key)."""
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


def check_hosts(hosts: list) -> list[Result]:
    """Named network hosts that must respond. The engine is domain-agnostic — a host
    is just a thing that answers on the network; whether it's a lamp, a modem or a
    mixer lives only in the name. Each host has an `ip` (pinged directly) OR a `mac`
    (resolved via ARP then pinged), an optional `severity` (default warn) and an
    optional `icon` (emoji shown in the dashboard). `hosts` is passed explicitly so the
    caller can mix global hosts (checks.hosts) with per-mode ones (modes.<mode>.hosts)."""
    res = []
    for h in hosts:
        name = h.get("name", "?")
        sev = h.get("severity", WARN)
        glyph = h.get("icon", "")
        by_ip = bool(h.get("ip"))
        ip = h.get("ip") or _ip_for_mac(h.get("mac", ""))
        key = f"host:{name}"
        if ip and _ping(ip):
            res.append(Result(key, name, OK, f"répond ({ip})", glyph))
        elif ip:
            res.append(Result(key, name, sev, f"vu ({ip}) mais ne répond pas", glyph))
        else:
            res.append(Result(key, name, sev,
                              "pas de réponse" if by_ip else "introuvable (ARP)", glyph))
    return res


def check_commands(cfg: dict) -> list[Result]:
    """Arbitrary user-defined checks — run a command, pass if it exits 0 (or if its
    stdout matches `expect_match`). This is what makes the engine domain-open: any
    condition becomes a config entry, no code. Config `commands` = list of
    {name, cmd, expect_exit?=0, expect_match?, severity?, icon?, timeout?}."""
    import re as _re
    res = []
    for c in cfg["checks"].get("commands", []):
        name = c.get("name", "?")
        icon = c.get("icon", "")
        sev = c.get("severity", FAIL)
        try:
            p = subprocess.run(["/bin/bash", "-lc", c.get("cmd", "")],
                               capture_output=True, text=True, timeout=c.get("timeout", 10))
        except Exception as exc:
            res.append(Result(f"cmd:{name}", name, WARN, f"erreur: {exc}", icon))
            continue
        if "expect_match" in c:
            ok = bool(_re.search(c["expect_match"], p.stdout))
            detail = "" if ok else "sortie inattendue"
        else:
            ok = p.returncode == c.get("expect_exit", 0)
            detail = "" if ok else f"exit {p.returncode}"
        res.append(Result(f"cmd:{name}", name, OK if ok else sev, detail, icon))
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


def check_manual_confirms(cfg: dict, mode: str, acks: dict) -> list[Result]:
    """Things software can't detect → a human ticks them before playing. Config
    `manual_confirms` = [{name, icon?, severity?}] where severity is "warn"/"fail" or a
    {profile: severity} map. Unconfirmed → the severity; confirmed → green."""
    res = []
    for mc in cfg["checks"].get("manual_confirms", []):
        name = mc.get("name", "?")
        icon = mc.get("icon", "")
        sev = mc.get("severity", "warn")
        if isinstance(sev, dict):
            sev = sev.get(mode, "warn")
        if acks.get(name):
            res.append(Result(f"manual:{name}", name, OK, "confirmé manuellement", icon))
        else:
            res.append(Result(f"manual:{name}", name, sev, "à confirmer", icon))
    return res


def check_links(cfg: dict) -> list[Result]:
    """Named remote links = an ESTABLISHED TCP connection on a port (e.g. a remote
    device connecting to a network app). Domain-agnostic: config `links` = list of
    {name, port, host? (peer substring filter), severity?, icon?}."""
    res = []
    for lk in cfg["checks"].get("links", []):
        name = lk.get("name", "?")
        port = lk.get("port")
        icon = lk.get("icon", "")
        sev = lk.get("severity", FAIL)
        host = str(lk.get("host", "")).strip()
        try:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
                                 capture_output=True, text=True, timeout=6).stdout
        except Exception as exc:
            res.append(Result(f"link:{name}", name, WARN, f"lsof: {exc}", icon))
            continue
        lines = [l for l in out.splitlines() if "ESTABLISHED" in l]
        if host:
            lines = [l for l in lines if host in l]
        if not lines:
            res.append(Result(f"link:{name}", name, sev, "aucune connexion", icon))
            continue
        peer = next((tok.split("->", 1)[1] for tok in lines[0].split() if "->" in tok), "")
        res.append(Result(f"link:{name}", name, OK,
                          f"connecté{f' ({peer})' if peer else ''}", icon))
    return res


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


def _default_output_name() -> str | None:
    """Name of the macOS default sound OUTPUT device right now, or None if undetermined."""
    for it in _audio_items():
        if it.get("coreaudio_default_audio_output_device") == "spaudio_yes":
            return it.get("_name", "")
    return None


def check_audio(cfg: dict, mode: str = "live") -> Result | None:
    """Verify the audio OUTPUT is on the mode's expected device. Per-mode:
    modes.<mode>.audio_interface = the device the sound must go out on (e.g. the P-225 in
    live). Absent for a mode → nothing to verify (returns None; e.g. studio). Mismatch is a
    hard error — on stage, sound on the wrong device means silence to the PA."""
    want = cfg["modes"].get(mode, {}).get("audio_interface")
    if not want:
        return None
    name = _default_output_name()
    ok = bool(name) and want.lower() in name.lower()
    return Result(
        key="audio", label=f"Sortie audio sur {want}",
        status=OK if ok else FAIL,
        detail="" if ok else f"sortie actuelle : {name or 'indéterminée'} (attendu : {want})",
    )


def check_default_output(cfg: dict) -> Result:
    """The macOS default sound output must be the Mac itself (built-in), not an
    external / AirPlay / conferencing device."""
    want = cfg["checks"].get("default_output_match", "MacBook")
    name = _default_output_name()
    if name is None:
        return Result("sys:output", "Sortie son par défaut (Mac)", WARN, "indéterminée")
    ok = want.lower() in name.lower()
    return Result(
        key="sys:output", label="Sortie son par défaut (Mac)",
        status=OK if ok else FAIL,
        detail=f"actuellement : {name}" if not ok else name,
    )


def read_audiolevel(cfg: dict) -> dict | None:
    """Read the optional audio-level probe file (see the `[audiolevel]` config + the
    audiolevel/ helper). The file holds one line "<rms> <epoch>". Returns None when the
    probe is not configured; otherwise {enabled, rms, age, fresh, ok, threshold}. `ok` is
    True only when the reading is fresh AND above threshold — i.e. sound is really flowing
    right now. The soundcheck uses this to auto-confirm audio instead of asking the human."""
    al = cfg.get("audiolevel", {})
    path = os.path.expanduser(al.get("file", "") or "")
    if not path:
        return None
    thr = float(al.get("threshold", 0.003))
    max_age = float(al.get("max_age", 6))
    try:
        raw = open(path).read().split()
        rms = float(raw[0])
        ts = float(raw[1]) if len(raw) > 1 else 0.0
    except Exception:
        # Configured but unreadable (probe never started / file missing) → enabled but not ok.
        return {"enabled": True, "rms": 0.0, "age": None, "fresh": False, "ok": False, "threshold": thr}
    age = max(0.0, time.time() - ts)
    fresh = age <= max_age
    return {"enabled": True, "rms": rms, "age": round(age, 1),
            "fresh": fresh, "ok": bool(fresh and rms > thr), "threshold": thr}


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
    results = check_apps(cfg) + check_usb(cfg) + check_midi(cfg)
    results += [check_keyboard(cfg, mode), check_breath(cfg, mode)]
    results += [check_stage_network(cfg, mode)]
    results += check_hosts(cfg["checks"].get("hosts", []))   # global hosts (e.g. lamps)
    results += check_hosts(m.get("hosts", []))               # per-mode hosts (e.g. the modem)
    results += check_links(cfg)
    results += check_commands(cfg)
    results += [check_vpn(cfg)]
    results += [check_mac_power(cfg, mode)]
    results += check_manual_confirms(cfg, mode, manual)
    if m.get("require_awake", False):
        r = check_keepawake(cfg)
        if r:
            results.append(r)
    if with_audio:
        results.append(check_default_output(cfg))
        a = check_audio(cfg, mode)       # None when the mode has nothing to verify (e.g. studio)
        if a:
            results.append(a)
        op = check_output_probe(cfg, mode)
        if op:
            results.append(op)
    return results


def worst(results: list[Result]) -> str:
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status == WARN for r in results):
        return WARN
    return OK
