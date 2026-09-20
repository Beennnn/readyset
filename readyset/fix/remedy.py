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

from .. import apps, liveaudio, vpn
from . import launch


@dataclass
class Remedy:
    label: str                                  # button text, e.g. "Relancer Bome"
    run: Callable[[bool], tuple[bool, str]]     # run(dry_run) -> (ok, message)
    # Ce remède RÈGLE-t-il le problème, ou ouvre-t-il seulement la porte à un geste
    # humain ? « Ouvrir le réglage Accessibilité » réussit toujours — il ouvre un
    # panneau — et le check reste rouge derrière : seul Benoît peut cocher la case.
    # Sans cette distinction, une surface qui compte les remèdes annonce « 1 réglable
    # ici » pour quelque chose qu'elle ne sait pas régler. C'est la même famille
    # d'erreur que le 2026-08-22 : compter la tentative comme la réussite.
    hands_on: bool = False


def _launch_app(path: str, dry: bool) -> tuple[bool, str]:
    name = Path(path).stem
    if not Path(path).exists():
        return False, f"{name} introuvable : {path}"
    if dry:
        return True, f"[dry-run] lancerait {name}"
    # Un correctif qui ne corrige rien ne doit pas être compté comme appliqué : si l'app
    # tourne déjà, c'est le CHECK qui a un problème (mauvaise installation, doublon), pas
    # le lancement qui en manque un. Le dire plutôt que de relancer à l'aveugle.
    if launch.running_from(path):
        return False, f"{name} tourne déjà — la relancer ne réglerait rien"
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


def checks_mode(cfg: dict) -> str:
    """Le mode résolu — le remède doit viser la même sortie que le check qui l'a déclenché."""
    from .. import checks
    return checks.resolve_mode(cfg, cfg.get("mode", {}).get("default", "auto"))


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

    # La sortie audio d'Ableton : le seul check dont le correctif touche l'INTÉRIEUR d'une
    # app, sans la relancer. Relancer Live serait la réparation la plus brutale du lot —
    # on perdrait le set chargé — alors que le réglage se change dans l'app ouverte.
    if key == "audio:live":
        mode = checks_mode(cfg)
        want = liveaudio.wanted(cfg, mode)
        if not want:
            return None
        return Remedy(f"Régler la sortie sur {want}",
                      lambda dry: liveaudio.apply(cfg, mode, dry))

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
        from ..checks import _pgrep
        if _pgrep(cfg["checks"]["apps"].get("Bome Network", "Bome Network")):
            return None
        net = _app_path_for(cfg, "Bome Network")
        if net:
            return Remedy("Lancer Bome Network (éteint sur le Mac)",
                          lambda dry: _launch_app(net, dry))
        return None

    # Fenêtre modale de Live : le seul correctif qui débloque TOUS les autres. Il ne
    # s'applique qu'aux fenêtres à bouton unique — `liveaudio.dismiss_dialog` refuse
    # elle-même celles qui proposent un choix, plutôt que de s'en remettre à l'appelant.
    if key == "audio:live-dialog":
        return Remedy("Congédier la fenêtre (OK)", _dismiss_live_dialog)

    # Autorisation d'accessibilité : personne ne peut la donner à la place de l'humain —
    # macOS exige le clic dans les Réglages Système, c'est le point même de la protection.
    # Le correctif ouvre donc la BONNE page (deux niveaux de sous-menu, cherchés de tête
    # sinon) et dit le geste qui reste. C'est le seul remède du lot qui ne répare rien
    # lui-même, et il gagne quand même sa place : ce qu'il fait gagner, c'est de ne pas
    # chercher où cliquer cinq minutes avant de jouer.
    if key == "sys:accessibility":
        # hands_on : l'autorisation se donne dans les Réglages Système, à la main, et
        # ne prend effet qu'au RELANCEMENT du service — deux gestes qu'aucun correctif
        # ne peut faire à la place de l'utilisateur.
        return Remedy("Ouvrir le réglage Accessibilité", _open_accessibility_pane, hands_on=True)

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

    # VPN actif → le couper. Pas un simple `scutil stop` : voir readyset/vpn.py (l'on-demand
    # reforme le tunnel dans la demi-seconde ; il faut éteindre le service réseau).
    # Coupure PERSISTANTE, d'où le libellé explicite et le `readyset vpn on` pour l'inverse.
    if key == "sys:vpn":
        return Remedy("Couper le VPN", lambda dry: vpn.turn_off(cfg, dry_run=dry))

    # coreaudiod figé → le relancer. Il tourne sous root : le tuer demande sudo. Deux
    # chemins, du plus discret au plus bruyant — voir _restart_coreaudiod.
    if key == "sys:coreaudio":
        return Remedy("Relancer le service audio (coreaudiod)", _restart_coreaudiod)

    # Audio interface = hardware, and Live's output device is set inside Ableton —
    # nothing to relaunch here.
    return None


def _dismiss_live_dialog(dry: bool) -> tuple[bool, str]:
    d = liveaudio.dialog()
    if not d:
        return True, "plus aucune fenêtre en attente"
    if dry:
        return True, f"[dry-run] cliquerait OK sur « {d[0]} »"
    return liveaudio.dismiss_dialog()


def _open_accessibility_pane(dry: bool) -> tuple[bool, str]:
    url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    todo = ("cocher le processus qui lance le dashboard, puis relancer l'agent : "
            "launchctl kickstart -k gui/$UID/com.readyset.dashboard")
    if dry:
        return True, f"[dry-run] ouvrirait Réglages › Accessibilité — {todo}"
    r = subprocess.run(["open", url], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or "ouverture des Réglages impossible"
    return True, f"Réglages ouverts — {todo}"


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


_COREAUDIOD_SUDOERS = "bin/install-coreaudiod-sudoers.sh"


def _restart_coreaudiod(dry: bool) -> tuple[bool, str]:
    """Tue coreaudiod ; launchd le relance en ~1 s et le son revient sans redémarrer.

    Le processus appartient à root, donc `killall` seul est refusé. Ordre d'essai :
    1. `sudo -n` — sans mot de passe SI la règle sudoers posée par
       bin/install-coreaudiod-sudoers.sh est là (scope : cette seule commande). C'est
       le chemin de scène : un clic, zéro dialogue.
    2. sinon `osascript … with administrator privileges` — macOS ouvre son dialogue de
       mot de passe. Ça marche du premier coup sans rien installer, mais ça suppose
       quelqu'un devant l'écran ; le message dit comment ne plus l'avoir.
    Ce que ça casse : les apps qui tenaient un périphérique (Ableton, Stage Traxx)
    perdent leur sortie et doivent la resélectionner — d'où l'avertissement rendu.
    """
    if dry:
        return True, "[dry-run] relancerait coreaudiod (killall ; launchd le ressuscite en ~1 s)"
    how = "règle sudoers"
    r = subprocess.run(["sudo", "-n", "/usr/bin/killall", "coreaudiod"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        how = "mot de passe saisi"
        r = subprocess.run(
            ["osascript", "-e",
             'do shell script "/usr/bin/killall coreaudiod" with administrator privileges'],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            err = r.stderr.strip()
            if "-128" in err:
                return False, (f"annulé — pour un relancement sans mot de passe, installer "
                               f"une fois : {_COREAUDIOD_SUDOERS}")
            return False, err or "killall coreaudiod a échoué"
    from .. import checks
    checks.audio_cache_reset()
    return True, (f"coreaudiod relancé ({how}) — le son revient dans les 2 s ; Ableton ou "
                  "Stage Traxx ouverts doivent resélectionner leur sortie audio")
