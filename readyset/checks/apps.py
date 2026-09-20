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
    # The app that [set] names is the ONLY Ableton install allowed to run. Two open Live
    # instances fight over the audio and MIDI interfaces, and nothing said so: the check
    # only asked for « at least one process ». Observed on 2026-08-29, Suite launched
    # next to Suite 3.
    want = cfg["set"].get("ableton_app", "")
    want_stem = Path(want).stem.lower() if want else ""
    out = []
    for label, pattern in cfg["checks"]["apps"].items():
        cmds = _pgrep_cmds(pattern)
        # The label states the STATE, not the expectation. « Ableton lancé » written in
        # red asserts the opposite of what is happening: you read the text before the
        # colour, and it takes a second to understand it has to be read backwards. On
        # stage that second costs dear.
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
    """Severity of the anti-sleep in this mode — "fail" on stage, and that is not negotiable.

    A Mac that falls asleep on the second song means the set stops: in live, a missing
    Amphetamine session is an ERROR, in the same way an unplugged keyboard is. The rule
    is written here rather than deduced from a boolean so that it reads in the config
    (`amphetamine_severity = "fail"`) instead of having to be guessed.

    `require_amphetamine` (boolean) is still read for the rig.toml files that only know
    it, but it can only say « checked » or « not checked » — hence the trap it carried:
    setting it to false in live did not lower the severity, it MADE THE LINE DISAPPEAR.
    A rig with no anti-sleep then looked like a rig with no problem.
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
        # We do NOT know: neither green (nothing was observed), nor red (nothing proves
        # the failure). The warning is the only honest level here.
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", WARN, f"pmset: {exc}")
    active = "(Amphetamine)" in out
    return Result("sys:amphetamine", "Amphetamine (anti-veille)",
                  OK if active else sev,
                  "session active" if active
                  else _hint("lancé, aucune session active",
                             "l'app tourne mais ne retient rien : démarrer une session "
                             "anti-veille (le bouton le fait)"))


def _process_age(pattern: str) -> float | None:
    """How many seconds this process has been running — None if it is not running.

    `ps -o etime=` rather than `lstart`: an elapsed time has neither a date format nor a
    month name to interpret, so nothing that could change with the system's language.
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


# The right to drive an interface is asked of macOS, granted PER CALLING PROCESS, and can
# vanish when the binary is updated. So we MEASURE it, like everything else: probing costs
# ~80 ms and we keep it for a minute — it only changes on a human click in the System
# Settings.
_AX_CACHE: dict = {"at": 0.0, "ok": None, "detail": ""}


_AX_TTL = 60.0


def _accessibility_probe() -> tuple[bool, str]:
    """Can this process read ANOTHER application's interface? Measured, not assumed.

    The gesture probed is deliberately the most harmless one that exercises the same
    authorisation as the fixes: counting the Finder's windows. No click, no keystroke,
    nothing that moves on screen — this check runs in the state loop, mid-set included.

    ⚠️ macOS separates interface READING (error -25211) from SENDING KEYSTROKES
    (error 1002), and a process can hold the first without the second — observed on
    2026-08-22. A green here therefore proves the service is authorised, not that every
    gesture will go through; that is why `liveaudio` ALSO re-translates the refusal at
    call time rather than relying on this check.
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
    """Does the service have the right to REPAIR? Without it, half the fixes lie.

    Two fixes drive an interface: setting Ableton's output (`live-output`) and tidying
    the windows. Both go through `osascript`, hence through the accessibility grant of
    the process that launches them — the launchd service, not the terminal where it
    worked by hand.

    Without this line, the missing grant was only discovered at the instant it was
    needed, that is to say during the bring-up: Ableton's output stayed on « No Device »,
    the raw error message went into a log nobody reads, and the rig declared itself
    ready. Lived through on 2026-08-22, at 16:47. An unobserved capability is exactly
    what this table exists to refuse.
    """
    now = time.monotonic()
    if _AX_CACHE["ok"] is None or now - _AX_CACHE["at"] > _AX_TTL:
        ok, detail = _accessibility_probe()
        _AX_CACHE.update(at=now, ok=ok, detail=detail)
    label = "Autorisation de réparer (Accessibilité)"
    # « à ce processus » and not « au service »: the line tells the truth from the
    # dashboard as much as from a terminal, and the wording reminds you in passing that
    # the answer can differ from one caller to the next — that is the whole trap.
    if _AX_CACHE["ok"]:
        return Result("sys:accessibility", label, OK, "accordée à ce processus")
    return Result("sys:accessibility", label, FAIL,
                  _hint(f"refusée à ce processus ({_AX_CACHE['detail']})",
                        "sans elle, ni la sortie d'Ableton ni le rangement des fenêtres "
                        "ne peuvent être réglés automatiquement"))


_DLG_CACHE: dict = {"at": 0.0, "val": None}


_DLG_TTL = 10.0


def check_live_dialog(cfg: dict) -> Result | None:
    """Is Ableton waiting for a click in a window? Line ABSENT when all is well.

    A modal window in Live is not a display detail: it makes the app deaf to everything
    else — to ⌘, to the output fix, to the window tidying, and to any gesture the rig
    might want to make. On 2026-08-22, Ableton stayed stuck on « La section audio est
    désactivée » while the table displayed 26 lines without ever mentioning the one thing
    that was blocking everything.

    The silence when there is nothing (`None`) is deliberate: this is an event, not a
    piece of gear. A green line « no window waiting » would add permanent noise for a
    state that almost never happens — same choice as the « applis en trop », which only
    appear when there are any.
    """
    if not _pgrep(cfg["checks"]["apps"].get("Ableton", "Ableton Live.*/MacOS/Live")):
        return None
    # Without the grant, we cannot LOOK: display nothing rather than a false calm — the
    # accessibility check is already red and carries the information.
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


# One line PER extra app, and not a single « 4 apps ouvertes »: each is judged separately
# (WhatsApp on stage is not Audio MIDI Setup), each has its own « Quitter » button, and the
# monitor's history can say which one appeared along the way.
def check_unexpected_apps(cfg: dict, mode: str) -> list[Result]:
    """Open apps the rig does not need — warn in live, info in studio.

    Never FAIL: an extra app does not make the rig unplayable, it makes it fragile.
    Making it blocking would mostly teach you to ignore the reds.
    """
    default = WARN if mode == "live" else INFO
    sev = cfg["modes"][mode].get("unexpected_apps_severity", default)
    if sev == OFF:
        return []
    return [Result(key=f"xapp:{a['name']}", label=a["name"], status=sev,
                   detail=f"ouverte, pas nécessaire au rig — {a['path']}")
            for a in app_inventory.unexpected(cfg)]
