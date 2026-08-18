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

from . import apps, launch, vpn


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

    # Bome Network ↔ iPhone. Le lien a deux bouts et un seul est actionnable d'ici.
    #
    # Si Bome Network TOURNE déjà sur le Mac, le relancer ne peut rien réparer : le
    # côté muet est le téléphone, qu'aucun bouton du Mac n'atteint. Pire, le bouton
    # coupe une app qui marche — et sur scène, il serait cliqué en premier justement
    # parce qu'il est là. Donc pas de correctif : le check dit d'ouvrir Bome Network
    # sur l'iPhone (règle posée par Benoît le 2026-08-18).
    #
    # S'il est éteint sur le Mac, en revanche, c'est bien ici que ça se répare.
    if key == "net:iphone":
        from .checks import _pgrep
        if _pgrep(cfg["checks"]["apps"].get("Bome Network", "Bome Network")):
            return None
        net = _app_path_for(cfg, "Bome Network")
        if net:
            return Remedy("Lancer Bome Network (éteint sur le Mac)",
                          lambda dry: _launch_app(net, dry))
        return None

    # Amphetamine: launch it if needed, then start an anti-sleep session.
    if key == "sys:amphetamine":
        return Remedy("Démarrer session Amphetamine", _amphetamine_session)

    # App en trop → la fermer. Le clic sur le bouton EST la confirmation pour une app
    # isolée ; la fermeture en lot passe par le panneau dédié du dashboard, qui liste les
    # icônes et demande une confirmation explicite avant de tout fermer d'un coup.
    if key.startswith("xapp:"):
        name = key.split(":", 1)[1]
        for a in apps.unexpected(cfg):
            if a["name"] == name:
                path = a["path"]
                return Remedy(f"Quitter {name}", lambda dry: apps.quit_app(path, dry_run=dry))
        return None

    # VPN actif → le couper. Pas un simple `scutil stop` : voir riglib/vpn.py (l'on-demand
    # reforme le tunnel dans la demi-seconde ; il faut éteindre le service réseau).
    # Coupure PERSISTANTE, d'où le libellé explicite et le `rig vpn on` pour l'inverse.
    if key == "sys:vpn":
        return Remedy("Couper le VPN", lambda dry: vpn.turn_off(cfg, dry_run=dry))

    # Audio interface = hardware, and Live's output device is set inside Ableton —
    # nothing to relaunch here.
    return None


def _amphetamine_session(dry: bool) -> tuple[bool, str]:
    if dry:
        return True, "[dry-run] démarrerait une session Amphetamine"
    subprocess.run(["open", "-a", "/Applications/Amphetamine.app"], capture_output=True)
    r = subprocess.run(
        ["osascript", "-e", 'tell application "Amphetamine" to start new session'],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return True, "session Amphetamine démarrée"
    return False, r.stderr.strip() or "échec (autorisation Automation ?)"
