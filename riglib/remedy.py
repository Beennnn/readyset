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



@dataclass
class Remedy:
    label: str                                  # button text, e.g. "Relancer <app>"
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


def _app_path_for(cfg: dict, label: str) -> str | None:
    for app in cfg["launch"]["apps"]:
        if label.lower() in Path(app).stem.lower():
            return app
    return None


def _launch_app_doc(app: str, doc: str, dry: bool) -> tuple[bool, str]:
    """Open a document IN an app (e.g. the DAW on the set's project file)."""
    if not Path(app).exists():
        return False, f"app introuvable : {app}"
    if not Path(doc).exists():
        return False, f"fichier introuvable : {doc}"
    if dry:
        return True, f"[dry-run] ouvrirait {Path(doc).name} dans {Path(app).stem}"
    r = subprocess.run(["open", "-a", app, doc], capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"{Path(app).stem} : {r.stderr.strip() or 'open a échoué'}"
    return True, f"{Path(doc).name} ouvert dans {Path(app).stem}"


def resolve(cfg: dict, result) -> Remedy | None:
    """Return the remedy for a failed/warned check, or None if not actionable."""
    return resolve_key(cfg, result.key)


def resolve_key(cfg: dict, key: str) -> Remedy | None:
    """Same as resolve() but keyed by string — used by the dashboard fix endpoint.
    Remedies are generic: a config-driven fix map, relaunch a configured app, open the
    set on its project, run a command check's fix_cmd, or a keep-awake start command.
    Everything specific lives in config."""
    # Config-driven fix map wins for ANY key: [checks.fixes] maps key -> {label, cmd}.
    # This is how built-in checks (sys:vpn, sys:output, …) get a one-click fix without
    # hardcoding domain specifics in the engine.
    fixes = cfg["checks"].get("fixes", {})
    if key in fixes and fixes[key].get("cmd"):
        f = fixes[key]
        return Remedy(f.get("label", "Réparer"), lambda dry, cmd=f["cmd"]: _run_cmd(cmd, dry))

    # A launched app that's down → relaunch it. If it's the set's DAW, open it ON the
    # configured project file ([set].project = the file to launch, editable in config).
    if key.startswith("app:"):
        appname = key.split(":", 1)[1]
        st = cfg.get("set", {})
        app, proj = st.get("app"), st.get("project")
        if app and proj and appname.lower() in Path(app).stem.lower():
            return Remedy(st.get("fix_label", f"Ouvrir le projet ({Path(proj).stem})"),
                          lambda dry, a=app, p=proj: _launch_app_doc(a, p, dry))
        path = _app_path_for(cfg, appname)
        if path:
            return Remedy(f"Relancer {Path(path).stem}", lambda dry: _launch_app(path, dry))
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
