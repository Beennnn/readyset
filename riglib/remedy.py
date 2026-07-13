"""Remediation — for a given failed check, what action brings it back.

Maps a check's key to a concrete fix so the dashboard can offer a per-item
button. Not every failure is auto-fixable (a missing audio interface is
hardware) — those return None and the UI just shows the detail.

Every remedy honours dry_run: it reports what it *would* do without doing it,
so the dashboard's dry-run toggle is real end to end.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import launch


@dataclass
class Remedy:
    label: str                                  # button text, e.g. "Relancer Bome"
    run: Callable[[bool], tuple[bool, str]]     # run(dry_run) -> (ok, message)


def _launch_app(path: str, dry: bool) -> tuple[bool, str]:
    name = Path(path).stem
    if not Path(path).exists():
        return False, f"{name} introuvable : {path}"
    if dry:
        return True, f"[dry-run] lancerait {name}"
    r = subprocess.run(["open", "-a", path], capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"{name} : {r.stderr.strip() or 'open a échoué'}"
    return True, f"{name} relancé"


def _open_set(cfg: dict, dry: bool) -> tuple[bool, str]:
    logs: list[str] = []
    # force open_after_launch for this explicit action even if config disables it
    forced = dict(cfg)
    forced["set"] = {**cfg["set"], "open_after_launch": True}
    launch.open_set(forced, log=logs.append, dry_run=dry)
    return True, "\n".join(logs)


def _app_path_for(cfg: dict, label: str) -> str | None:
    for app in cfg["launch"]["apps"]:
        if label.lower() in Path(app).stem.lower():
            return app
    return None


def _bome_path(cfg: dict) -> str | None:
    return _app_path_for(cfg, "Bome")


def resolve(cfg: dict, result) -> Remedy | None:
    """Return the remedy for a failed/warned check, or None if not actionable."""
    return resolve_key(cfg, result.key)


def resolve_key(cfg: dict, key: str) -> Remedy | None:
    """Same as resolve() but keyed by string — used by the dashboard fix endpoint."""
    if key.startswith("app:"):
        label = key.split(":", 1)[1]
        if "ableton" in label.lower():
            return Remedy("Ouvrir le set (relance Ableton)",
                          lambda dry: _open_set(cfg, dry))
        path = _app_path_for(cfg, label)
        if path:
            return Remedy(f"Relancer {label}", lambda dry: _launch_app(path, dry))
        return None

    # Required MIDI ports only (optional ones use the "midi?:" prefix → no remedy).
    if key.startswith("midi:") and not key.startswith("midi?:"):
        name = key.split(":", 1)[1]
        if "ableton loopback" in name.lower():
            return Remedy("Rouvrir le set Ableton", lambda dry: _open_set(cfg, dry))
        bome = _bome_path(cfg)
        if bome:
            return Remedy("Relancer Bome (routing MIDI)",
                          lambda dry: _launch_app(bome, dry))
        return None

    # Command checks: run the configured fix_cmd (generic, config-driven).
    if key.startswith("cmd:"):
        name = key.split(":", 1)[1]
        for c in cfg["checks"].get("commands", []):
            if c.get("name") == name and c.get("fix_cmd"):
                return Remedy(c.get("fix_label", "Réparer"),
                              lambda dry, cmd=c["fix_cmd"]: _run_cmd(cmd, dry))
        return None

    # Keep-awake: run its configured start command (e.g. start an anti-sleep session).
    if key == "sys:keepawake":
        ka = cfg["checks"].get("keepawake", {})
        if ka.get("start_cmd"):
            return Remedy(ka.get("start_label", "Démarrer la session"),
                          lambda dry, cmd=ka["start_cmd"]: _run_cmd(cmd, dry))
        return None

    # Audio interface = hardware, output device set inside the app — nothing to relaunch.
    return None


def _run_cmd(cmd: str, dry: bool) -> tuple[bool, str]:
    if dry:
        return True, f"[dry-run] exécuterait : {cmd}"
    r = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True)
    if r.returncode == 0:
        return True, "ok"
    return False, (r.stderr.strip() or f"exit {r.returncode}")[:200]
