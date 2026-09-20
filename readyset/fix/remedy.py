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
    # Does this remedy FIX the problem, or does it merely open the door to a human
    # gesture? "Ouvrir le réglage Accessibilité" always succeeds — it opens a pane — and
    # the check stays red behind it: only the rig's owner can tick the box. Without that
    # distinction, a surface counting the remedies announces "1 fixable here" for
    # something it does not know how to fix. It is the same family of error as on
    # 2026-08-22: counting the attempt as the success.
    hands_on: bool = False


def _launch_app(path: str, dry: bool) -> tuple[bool, str]:
    name = Path(path).stem
    if not Path(path).exists():
        return False, f"{name} introuvable : {path}"
    if dry:
        return True, f"[dry-run] lancerait {name}"
    # A fix that fixes nothing must not be counted as applied: if the app is already
    # running, it is the CHECK that has a problem (bad installation, duplicate), not the
    # launching that is missing one. Say so rather than relaunch blindly.
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
    """The resolved mode — the remedy must aim at the same output as the check that fired it."""
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

    # Ableton's audio output: the only check whose fix touches the INSIDE of an app,
    # without relaunching it. Relaunching Live would be the most brutal repair of the lot
    # — the loaded set would be lost — whereas the setting can be changed in the open app.
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

    # Bome Network ↔ iPhone. The link has two ends and only one is actionable from here.
    #
    # If Bome Network is ALREADY RUNNING on the Mac, relaunching it can repair nothing:
    # the mute side is the phone, which no button on the Mac reaches. Worse, the button
    # takes down an app that works — and on stage, it would be clicked first precisely
    # because it is there. So no fix: the check says to open Bome Network on the iPhone
    # (a rule set on 2026-08-18).
    #
    # If it is off on the Mac, on the other hand, this is indeed where it gets repaired.
    if key == "net:iphone":
        from ..checks import _pgrep
        if _pgrep(cfg["checks"]["apps"].get("Bome Network", "Bome Network")):
            return None
        net = _app_path_for(cfg, "Bome Network")
        if net:
            return Remedy("Lancer Bome Network (éteint sur le Mac)",
                          lambda dry: _launch_app(net, dry))
        return None

    # Live's modal dialog: the only fix that unblocks ALL the others. It applies only to
    # single-button dialogs — `liveaudio.dismiss_dialog` itself refuses the ones offering
    # a choice, rather than relying on the caller for that.
    if key == "audio:live-dialog":
        return Remedy("Congédier la fenêtre (OK)", _dismiss_live_dialog)

    # Accessibility permission: nobody can grant it in the human's place — macOS demands
    # the click inside System Settings, that is the very point of the protection. So the
    # fix opens the RIGHT page (two levels of submenu, otherwise hunted for from memory)
    # and states the gesture that remains. It is the only remedy of the lot that repairs
    # nothing by itself, and it still earns its place: what it saves is not having to
    # hunt for where to click five minutes before playing.
    if key == "sys:accessibility":
        # hands_on: the permission is granted in System Settings, by hand, and only
        # takes effect on the RESTART of the service — two gestures no fix can perform
        # in the user's place.
        return Remedy("Ouvrir le réglage Accessibilité", _open_accessibility_pane, hands_on=True)

    # Amphetamine: launch it if needed, then start an anti-sleep session.
    if key == "sys:amphetamine":
        return Remedy("Démarrer session Amphetamine", _amphetamine_session)

    # Extra app → close it. Clicking the button IS the confirmation for a single app;
    # closing in batch goes through the dashboard's dedicated panel, which lists the
    # icons and asks for an explicit confirmation before closing everything at once.
    if key.startswith("xapp:"):
        name = key.split(":", 1)[1]
        for a in apps.unexpected(cfg):
            if a["name"] == name:
                path = a["path"]
                return Remedy(f"Quitter {name}", lambda dry: apps.quit_app(path, dry_run=dry))
        return None

    # Active VPN → cut it. Not a plain `scutil stop`: see readyset/vpn.py (on-demand
    # re-forms the tunnel within half a second; the network service has to be switched
    # off). A PERSISTENT cut, hence the explicit label and the `readyset vpn on` to undo.
    if key == "sys:vpn":
        return Remedy("Couper le VPN", lambda dry: vpn.turn_off(cfg, dry_run=dry))

    # coreaudiod frozen → restart it. It runs as root: killing it requires sudo. Two
    # paths, from the most discreet to the noisiest — see _restart_coreaudiod.
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
    """Kill coreaudiod; launchd restarts it in ~1 s and the sound comes back, no reboot.

    The process belongs to root, so `killall` alone is refused. Order of attempts:
    1. `sudo -n` — without a password IF the sudoers rule installed by
       bin/install-coreaudiod-sudoers.sh is there (scope: that single command). That is
       the stage path: one click, zero dialogs.
    2. otherwise `osascript … with administrator privileges` — macOS opens its password
       dialog. It works on the first try without installing anything, but it assumes
       somebody in front of the screen; the message says how to stop having it.
    What it breaks: the apps that were holding a device (Ableton, Stage Traxx) lose their
    output and have to re-select it — hence the warning that is returned.
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
