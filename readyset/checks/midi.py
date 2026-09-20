"""Checks on MIDI PORTS — the keyboard, the breath controller, and the required ports.

All of them come down to one question: is this name present among the CoreMIDI inputs?
The enumeration is done once per call rather than once per candidate — see _present.
"""

from __future__ import annotations

import mido

from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint


def _midi_inputs() -> list[str]:
    from ..midi_lock import MIDI_LOCK
    try:
        with MIDI_LOCK:
            names = list(mido.get_input_names())
    except Exception as exc:  # pragma: no cover - backend init failure
        return [f"__error__:{exc}"]
    # CoreMIDI can hand back an endpoint whose name reads as None: a device that
    # vanished while the process kept its client open leaves a nameless ghost behind.
    # A fresh process never sees it, which is why `readyset check` stayed green while the
    # long-lived dashboard crashed on EVERY request for 18 h (2026-08-18, after the
    # Dell dock dropped the whole USB chain). Filtering here fixes every caller at
    # once — several of them do `p.lower()` and would raise the same way.
    return [n for n in names if isinstance(n, str) and n]


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
    """Three tiers: a preferred keyboard (keyboard_ok, e.g. Digital Piano / the stage
    keyboard) → green; only a fallback (keyboard_warn, e.g. microKey Air) → yellow;
    none → red. So in the studio microKey alone works but warns; the stage keyboard
    always satisfies."""
    m = cfg["modes"][mode]
    ok_list = m.get("keyboard_ok", m.get("keyboard", []))
    warn_list = m.get("keyboard_warn", [])
    # One _present() per candidate, not two: each call re-enumerates CoreMIDI under
    # the MIDI lock, so the double evaluation doubled the cost of the check.
    found_ok = next((hit for a in ok_list if (hit := _present(a))), None)
    if found_ok:
        return Result("kbd:keyboard", "Clavier", OK, f"détecté : {found_ok}")
    found_warn = next((hit for a in warn_list if (hit := _present(a))), None)
    if found_warn:
        return Result("kbd:keyboard", "Clavier", WARN,
                      _hint(f"{found_warn} (clavier principal absent)",
                            "allumer le clavier principal et le brancher en USB"))
    return Result("kbd:keyboard", "Clavier", FAIL,
                  _hint("aucun clavier branché",
                        "l'allumer et le brancher en USB au Mac"))


def check_breath(cfg: dict, mode: str) -> Result | None:
    """Breath controller. Severity depends on mode: part of the live rig (fail),
    optional at the desk (warn)."""
    sev = cfg["modes"][mode].get("breath_severity", "warn")
    if sev == OFF:
        return None
    found = _present(cfg["checks"].get("breath_port", "Breath Controller"))
    if found:
        return Result("kbd:breath", "Breath controller", OK, f"détecté : {found}")
    return Result("kbd:breath", "Breath controller", sev,
                  "absent" if sev == FAIL else "absent (optionnel en studio)")
