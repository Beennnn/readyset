"""Bring-up sequence — launch the configured apps in order, then open the project.

Poll-for-readiness rather than fixed sleeps: after launching, wait until an expected
MIDI port appears before opening the project, so it binds to live ports instead of
racing an app that is still booting.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import mido


def _open_app(app_path: str) -> tuple[bool, str]:
    if not Path(app_path).exists():
        return False, f"introuvable : {app_path}"
    r = subprocess.run(["open", "-a", app_path], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or "open a échoué"
    return True, "lancé"


def _wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _midi_port_present(substr: str) -> bool:
    try:
        return any(substr.lower() in p.lower() for p in mido.get_input_names())
    except Exception:
        return False


def launch_apps(cfg: dict, log=print, dry_run: bool = False) -> None:
    settle = cfg["launch"].get("settle_seconds", 2)
    for app in cfg["launch"]["apps"]:
        name = Path(app).stem
        if dry_run:
            exists = "" if Path(app).exists() else "  (introuvable !)"
            log(f"  [dry-run] lancerait {name}{exists}")
            continue
        ok, msg = _open_app(app)
        log(f"  {'▶' if ok else '✖'} {name} — {msg}")
        if ok:
            time.sleep(settle)

    if dry_run:
        return

    # The virtual ports come from the macOS IAC Driver (already online), so they
    # normally exist immediately; this wait just guards the rare cold-boot race.
    required = cfg["checks"]["midi_required"]
    if required:
        anchor = required[0]
        log(f"  … attente du port MIDI « {anchor} »")
        if _wait_for(lambda: _midi_port_present(anchor), timeout=15):
            log(f"  ✔ ports MIDI virtuels présents")
        else:
            log(f"  ⚠️  « {anchor} » toujours absent après 15s (routeur MIDI pas prêt ?)")


def open_project(cfg: dict, log=print, dry_run: bool = False) -> None:
    if not cfg["set"].get("open_after_launch", True):
        log("  (ouverture du projet désactivée : [set].open_after_launch = false)")
        return
    project = cfg["set"]["project"]
    app = cfg["set"]["app"]
    if dry_run:
        pe = "" if Path(project).exists() else "  (projet introuvable !)"
        ae = "" if Path(app).exists() else "  (app introuvable !)"
        log(f"  [dry-run] ouvrirait « {Path(project).name} »{pe}")
        log(f"  [dry-run]   dans {Path(app).stem}{ae}")
        return
    if not Path(project).exists():
        log(f"  ✖ projet introuvable : {project}")
        log(f"    → corrige [set].project dans rig.toml")
        return
    if not Path(app).exists():
        log(f"  ✖ app introuvable : {app}")
        return
    r = subprocess.run(["open", "-a", app, project], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  ✖ ouverture du projet : {r.stderr.strip()}")
        return
    log(f"  ▶ ouverture de « {Path(project).name} » dans {Path(app).stem}")
    req = cfg["checks"].get("midi_required") or []
    if req:
        anchor = req[0]
        log(f"  … attente du port « {anchor} »")
        if _wait_for(lambda: _midi_port_present(anchor), timeout=45, interval=1):
            log("  ✔ projet en ligne")
        else:
            log("  ⚠️  pas encore prêt après 45s (gros projet / plugins qui chargent)")


def run_post_cmds(cfg: dict, log=print, dry_run: bool = False) -> None:
    """Generic post-launch shell commands (config launch.post_cmds), e.g. starting an
    anti-sleep session. Each entry: a string, or {cmd, label}."""
    for entry in cfg["launch"].get("post_cmds", []):
        cmd = entry.get("cmd") if isinstance(entry, dict) else entry
        label = entry.get("label", cmd) if isinstance(entry, dict) else cmd
        if not cmd:
            continue
        if dry_run:
            log(f"  [dry-run] exécuterait : {label}")
            continue
        r = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True)
        if r.returncode == 0:
            log(f"  ▶ {label}")
        else:
            log(f"  ⚠️  {label} : {r.stderr.strip() or f'exit {r.returncode}'}")


def bring_up(cfg: dict, log=print, dry_run: bool = False) -> None:
    log("Lancement des apps du rig…" if not dry_run else "Séquence de mise en place (dry-run) :")
    launch_apps(cfg, log=log, dry_run=dry_run)
    run_post_cmds(cfg, log=log, dry_run=dry_run)
    open_project(cfg, log=log, dry_run=dry_run)
