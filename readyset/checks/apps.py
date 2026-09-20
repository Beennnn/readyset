"""Checks on the MACHINE'S SOFTWARE — is what should be running, running?

Covers four questions that all reduce to "what state is an app in":
the rig's own apps (present, exactly once, the right install), the anti-sleep session,
the apps that are open and should not be, and the two macOS-side obstacles that make
every other fix fail — the missing Accessibility grant, and a modal window in the DAW.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .. import apps as app_inventory
from .. import liveaudio
from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint


def _pgrep(pattern: str) -> bool:
    # -f matches the full command line; pattern is an extended regex.
    return subprocess.run(
        ["pgrep", "-f", pattern],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def _pgrep_cmds(pattern: str) -> list[str]:
    """Command line of every process matching, not merely whether one does.

    Counting them is the point: two copies of the same app is a fault of its own,
    and « it is running » hides it completely.
    """
    r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True)
    return [l.split(" ", 1)[1] for l in r.stdout.splitlines() if " " in l]


def check_apps(cfg: dict) -> list[Result]:
    # L'app que [set] désigne est la SEULE installation d'Ableton à tourner. Deux Live
    # ouverts se disputent les interfaces audio et MIDI, et rien ne le disait : le check
    # ne demandait que « au moins un process ». Constaté le 2026-08-29, Suite lancé à
    # côté de Suite 3.
    want = cfg["set"].get("ableton_app", "")
    want_stem = Path(want).stem.lower() if want else ""
    out = []
    for label, pattern in cfg["checks"]["apps"].items():
        cmds = _pgrep_cmds(pattern)
        # Le libellé dit l'ÉTAT, pas l'attente. « Ableton lancé » écrit en rouge affirme
        # le contraire de ce qui se passe : on lit le texte avant la couleur, et il faut
        # une seconde pour comprendre qu'il faut le lire à l'envers. Sur scène cette
        # seconde-là coûte cher.
        if not cmds:
            status, titre, detail = FAIL, f"{label} non lancé", "process introuvable"
        elif len(cmds) > 1:
            status, titre = FAIL, f"{label} en double"
            detail = f"{len(cmds)} instances, il n'en faut qu'une : " + " · ".join(
                Path(c.split("/Contents/")[0]).name for c in cmds)
        elif want and label.lower() in want_stem and not cmds[0].startswith(want):
            status, titre = FAIL, f"{label} : mauvaise installation"
            detail = (f"{Path(cmds[0].split('/Contents/')[0]).name} "
                      f"— attendu {Path(want).name}")
        else:
            status, titre, detail = OK, f"{label} lancé", ""
        out.append(Result(key=f"app:{label}", label=titre, status=status, detail=detail))
    return out


def _amphetamine_severity(cfg: dict, mode: str) -> str:
    """Sévérité de l'anti-veille dans ce mode — "fail" sur scène, et ce n'est pas négociable.

    Un Mac qui s'endort au deuxième morceau, c'est le set qui s'arrête : en live, une
    session Amphetamine absente est une ERREUR, au même titre qu'un clavier débranché.
    La règle est écrite ici plutôt que déduite d'un booléen pour qu'elle se lise dans la
    config (`amphetamine_severity = "fail"`) au lieu de se deviner.

    `require_amphetamine` (booléen) reste lu pour les rig.toml qui ne connaissent que lui,
    mais il ne sait dire que « vérifié » ou « pas vérifié » — d'où le piège qu'il portait :
    le mettre à false en live ne baissait pas la sévérité, il FAISAIT DISPARAÎTRE la ligne.
    Un rig sans anti-veille ressemblait alors à un rig sans problème.
    """
    m = cfg["modes"].get(mode, {})
    if "amphetamine_severity" in m:
        return m["amphetamine_severity"]
    return FAIL if m.get("require_amphetamine", True) else OFF


def check_amphetamine(cfg: dict, mode: str = "live") -> Result | None:
    """Amphetamine must be running AND holding an active anti-sleep session. A live
    session shows up as an '(Amphetamine)' power assertion in `pmset -g assertions`."""
    sev = _amphetamine_severity(cfg, mode)
    if sev == OFF:
        return None
    if not _pgrep("Amphetamine.app/Contents/MacOS/Amphetamine"):
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", sev,
                      _hint("pas lancé", "le Mac s'endormira pendant le set — le lancer, "
                                         "puis démarrer une session (le bouton le fait)"))
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        # On ne SAIT pas : ni vert (rien n'a été observé), ni rouge (rien ne prouve la
        # panne). L'avertissement est le seul niveau honnête ici.
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", WARN, f"pmset: {exc}")
    active = "(Amphetamine)" in out
    return Result("sys:amphetamine", "Amphetamine (anti-veille)",
                  OK if active else sev,
                  "session active" if active
                  else _hint("lancé, aucune session active",
                             "l'app tourne mais ne retient rien : démarrer une session "
                             "anti-veille (le bouton le fait)"))


def _process_age(pattern: str) -> float | None:
    """Depuis combien de secondes ce process tourne — None s'il ne tourne pas.

    `ps -o etime=` plutôt que `lstart` : un temps écoulé n'a ni format de date ni nom de
    mois à interpréter, donc rien qui puisse changer avec la langue du système.
    """
    try:
        pid = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True,
                             timeout=5).stdout.split()
        if not pid:
            return None
        out = subprocess.run(["ps", "-o", "etime=", "-p", pid[0]], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    days, _, rest = out.partition("-")
    if not rest:
        days, rest = "0", days
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return int(days) * 86400 + h * 3600 + m * 60 + sec


# Le droit de piloter une interface se demande à macOS, se donne PAR PROCESSUS APPELANT,
# et peut disparaître à une mise à jour du binaire. On le MESURE donc, comme tout le reste :
# le sonder coûte ~80 ms, on le garde une minute — il ne change qu'à un clic humain dans
# les Réglages Système.
_AX_CACHE: dict = {"at": 0.0, "ok": None, "detail": ""}


_AX_TTL = 60.0


def _accessibility_probe() -> tuple[bool, str]:
    """Ce processus peut-il lire l'interface d'une AUTRE application ? Mesuré, pas supposé.

    Le geste sondé est volontairement le plus inoffensif qui exerce la même autorisation
    que les correctifs : compter les fenêtres du Finder. Aucun clic, aucune frappe, rien
    qui bouge à l'écran — ce check tourne dans la boucle d'état, y compris en plein set.

    ⚠️ macOS sépare la LECTURE d'interface (erreur -25211) de l'ENVOI DE FRAPPES
    (erreur 1002), et un processus peut tenir la première sans la seconde — observé le
    2026-08-22. Un vert ici prouve donc que le service est autorisé, pas que chaque geste
    passera ; c'est pour ça que `liveaudio` retraduit AUSSI le refus au moment de l'appel
    plutôt que de s'en remettre à ce check.
    """
    try:
        p = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to tell process "Finder" to count windows'],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "System Events n'a pas répondu"
    if p.returncode == 0:
        return True, ""
    lines = [l for l in ((p.stderr or "") + "\n" + (p.stdout or "")).splitlines() if l.strip()]
    return False, (lines[-1] if lines else f"code {p.returncode}")


def check_accessibility(cfg: dict) -> Result:
    """Le service a-t-il le droit de RÉPARER ? Sans lui, la moitié des correctifs mentent.

    Deux correctifs pilotent une interface : régler la sortie d'Ableton (`live-output`) et
    ranger les fenêtres. Tous deux passent par `osascript`, donc par l'autorisation
    d'accessibilité du processus qui les lance — le service launchd, pas le terminal où
    ça marchait à la main.

    Sans cette ligne, l'absence d'autorisation ne se découvrait qu'à l'instant du besoin,
    c'est-à-dire pendant la mise en place : la sortie d'Ableton restait sur « No Device »,
    le message d'erreur brut passait dans un journal que personne ne lit, et le rig se
    déclarait prêt. Vécu le 2026-08-22, à 16:47. Une capacité non observée est exactement
    ce que ce tableau existe pour refuser.
    """
    now = time.monotonic()
    if _AX_CACHE["ok"] is None or now - _AX_CACHE["at"] > _AX_TTL:
        ok, detail = _accessibility_probe()
        _AX_CACHE.update(at=now, ok=ok, detail=detail)
    label = "Autorisation de réparer (Accessibilité)"
    # « à ce processus » et pas « au service » : la ligne dit vrai depuis le dashboard
    # comme depuis un terminal, et le libellé rappelle au passage que la réponse peut
    # différer d'un appelant à l'autre — c'est tout le piège.
    if _AX_CACHE["ok"]:
        return Result("sys:accessibility", label, OK, "accordée à ce processus")
    return Result("sys:accessibility", label, FAIL,
                  _hint(f"refusée à ce processus ({_AX_CACHE['detail']})",
                        "sans elle, ni la sortie d'Ableton ni le rangement des fenêtres "
                        "ne peuvent être réglés automatiquement"))


_DLG_CACHE: dict = {"at": 0.0, "val": None}


_DLG_TTL = 10.0


def check_live_dialog(cfg: dict) -> Result | None:
    """Ableton attend-il qu'on clique dans une fenêtre ? Ligne ABSENTE quand tout va bien.

    Une fenêtre modale dans Live n'est pas un détail d'affichage : elle rend l'app sourde à
    tout le reste — au ⌘, du correctif de sortie, au rangement des fenêtres, et à n'importe
    quel geste que le rig voudrait faire. Le 2026-08-22, Ableton est resté planté sur « La
    section audio est désactivée » pendant que le tableau affichait 26 lignes sans jamais
    mentionner la seule chose qui bloquait tout.

    Le silence quand il n'y a rien (`None`) est délibéré : c'est un événement, pas un
    équipement. Une ligne verte « aucune fenêtre en attente » ajouterait du bruit permanent
    pour un état qui n'arrive presque jamais — même choix que les « applis en trop », qui
    n'apparaissent que lorsqu'il y en a.
    """
    if not _pgrep(cfg["checks"]["apps"].get("Ableton", "Ableton Live.*/MacOS/Live")):
        return None
    # Sans l'autorisation, on ne peut pas REGARDER : ne rien afficher plutôt qu'un faux
    # calme — le check d'accessibilité, lui, est déjà rouge et porte l'information.
    if _AX_CACHE["ok"] is False:
        return None
    now = time.monotonic()
    if now - _DLG_CACHE["at"] > _DLG_TTL:
        from .. import liveaudio
        _DLG_CACHE.update(at=now, val=liveaudio.dialog())
    d = _DLG_CACHE["val"]
    if not d:
        return None
    txt, btns = d
    label = "Ableton attend une réponse"
    single_ok = [b.upper() for b in btns] == ["OK"]
    return Result("audio:live-dialog", label, FAIL,
                  _hint(f"fenêtre ouverte : « {txt} »",
                        "tant qu'elle est là, Ableton ignore tout le reste"
                        if single_ok else
                        f"elle propose un choix ({', '.join(btns)}) — à traiter à la main, "
                        "le rig ne clique jamais dans une fenêtre qui peut faire perdre un set"))


# Une ligne PAR app en trop, et pas un unique « 4 apps ouvertes » : chacune se juge
# séparément (WhatsApp sur scène n'est pas Audio MIDI Setup), chacune a son bouton
# « Quitter », et l'historique du monitor sait dire laquelle est apparue en cours de route.
def check_unexpected_apps(cfg: dict, mode: str) -> list[Result]:
    """Apps ouvertes dont le rig n'a pas besoin — warn en live, info en studio.

    Jamais FAIL : une app en trop ne rend pas le rig injouable, elle le rend fragile.
    En faire un bloquant apprendrait surtout à ignorer les rouges.
    """
    default = WARN if mode == "live" else INFO
    sev = cfg["modes"][mode].get("unexpected_apps_severity", default)
    if sev == OFF:
        return []
    return [Result(key=f"xapp:{a['name']}", label=a["name"], status=sev,
                   detail=f"ouverte, pas nécessaire au rig — {a['path']}")
            for a in app_inventory.unexpected(cfg)]
