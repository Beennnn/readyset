"""Bring-up sequence — launch the rig apps in order, then open the gig set.

Poll-for-readiness rather than fixed sleeps: after launching Bome we wait until
its virtual MIDI ports actually appear before opening the Ableton set, so the set
binds to live ports instead of racing an app that is still booting.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import mido

from . import windows


def _open_app(app_path: str, hidden: bool = False) -> tuple[bool, str]:
    if not Path(app_path).exists():
        return False, f"introuvable : {app_path}"
    # -g : ne pas passer au premier plan. -j : démarrer masquée. Les deux se règlent au
    # LANCEMENT, donc sans autorisation Accessibilité — c'est le moyen le plus propre de
    # ne jamais voir clignoter la fenêtre d'une app qui n'a rien à faire à l'écran.
    cmd = ["open", "-a", app_path] + (["-g", "-j"] if hidden else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or "open a échoué"
    return True, "lancé (masqué)" if hidden else "lancé"


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
        hidden = windows.launch_hidden(cfg, app)
        if dry_run:
            exists = "" if Path(app).exists() else "  (introuvable !)"
            log(f"  [dry-run] lancerait {name}{' masquée' if hidden else ''}{exists}")
            continue
        ok, msg = _open_app(app, hidden=hidden)
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
            log(f"  ⚠️  « {anchor} » toujours absent après 15s (Bome pas prêt ?)")


def open_set(cfg: dict, log=print, dry_run: bool = False) -> None:
    if not cfg["set"].get("open_after_launch", True):
        log("  (ouverture du set désactivée : [set].open_after_launch = false)")
        return
    project = cfg["set"]["project"]
    # Deux noms de clé pour la même chose — voir windows.rig_apps() pour l'histoire.
    # Ne lire que `ableton_app` faisait retomber sur le défaut codé en dur, qui pointe
    # vers une install d'Ableton absente de cette machine : le set ne s'ouvrait pas.
    app = cfg["set"].get("app") or cfg["set"]["ableton_app"]
    if dry_run:
        pe = "" if Path(project).exists() else "  (set introuvable !)"
        ae = "" if Path(app).exists() else "  (Ableton introuvable !)"
        log(f"  [dry-run] ouvrirait « {Path(project).name} »{pe}")
        log(f"  [dry-run]   dans {Path(app).stem}{ae}")
        return
    if not Path(project).exists():
        log(f"  ✖ set introuvable : {project}")
        log(f"    → corrige [set].project dans rig.toml")
        return
    if not Path(app).exists():
        log(f"  ✖ Ableton introuvable : {app}")
        return
    r = subprocess.run(["open", "-a", app, project], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  ✖ ouverture du set : {r.stderr.strip()}")
        return
    log(f"  ▶ ouverture de « {Path(project).name} » dans {Path(app).stem}")
    log("  … attente du port « Ableton Loopback »")
    if _wait_for(lambda: _midi_port_present("Ableton Loopback"), timeout=45, interval=1):
        log("  ✔ Ableton en ligne")
    else:
        log("  ⚠️  Ableton pas encore prêt après 45s (gros set / plugins qui chargent)")


def ensure_amphetamine_session(cfg: dict, log=print, dry_run: bool = False) -> None:
    if not cfg["launch"].get("amphetamine_session", True):
        return
    if dry_run:
        log("  [dry-run] démarrerait une session Amphetamine (anti-veille)")
        return
    # Amphetamine exposes an AppleScript command; a bare session runs indefinitely.
    r = subprocess.run(
        ["osascript", "-e", 'tell application "Amphetamine" to start new session'],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        log("  ☕ session Amphetamine démarrée")
    else:
        log(f"  ⚠️  Amphetamine : {r.stderr.strip() or 'session non démarrée (autorisation ?)'}")


def tidy_windows(cfg: dict, log=print, dry_run: bool = False) -> None:
    """Range les fenêtres en fin de bring-up (voir riglib/windows.py).

    Après le lancement, même masquées au démarrage, des apps déjà ouvertes avant le
    préflight peuvent traîner à l'écran — et Ableton, lui, vient de passer devant en
    ouvrant le set. Ce passage final laisse donc l'écran dans l'état de scène : le set
    devant, le reste rangé.
    """
    if not cfg.get("windows", {}).get("after_preflight", True):
        return
    log("  🪟 rangement des fenêtres…")
    windows.tidy(cfg, log=log, dry_run=dry_run)


def bring_up(cfg: dict, log=print, dry_run: bool = False) -> None:
    log("Lancement des apps du rig…" if not dry_run else "Séquence de mise en place (dry-run) :")
    launch_apps(cfg, log=log, dry_run=dry_run)
    ensure_amphetamine_session(cfg, log=log, dry_run=dry_run)
    open_set(cfg, log=log, dry_run=dry_run)
    tidy_windows(cfg, log=log, dry_run=dry_run)
