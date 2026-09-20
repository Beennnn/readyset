"""Bring-up sequence — launch the rig apps in order, then open the gig set.

Poll-for-readiness rather than fixed sleeps: after launching Bome we wait until
its virtual MIDI ports actually appear before opening the Ableton set, so the set
binds to live ports instead of racing an app that is still booting.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from pathlib import Path

import mido

from .. import windows, liveaudio


# Sentinel message: saying "already launched" is not saying "launched", and the caller
# needs the nuance — both for the glyph it displays and for the delay it saves itself.
ALREADY = "déjà lancée"


def running_from(app_path: str) -> list[str]:
    """The processes running FROM this precise bundle.

    We compare PATHS, not names: two installations of the same application carry the same
    name AND the same bundle identifier — Ableton is the proof of it, with "Suite" and
    "Suite 3" both under com.ableton.live. Only the path separates them.

    Without that guard rail, `open -a` stays harmless on an already-launched app (all it
    does is bring it to the foreground) EXCEPT when another copy is running: there it
    starts a second one, and two instances fight over the same audio and MIDI interfaces.
    """
    prefix = str(Path(app_path)).rstrip("/") + "/Contents/MacOS/"
    r = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True)
    return [c for c in r.stdout.splitlines() if c.startswith(prefix)]


def _open_app(app_path: str, hidden: bool = False) -> tuple[bool, str]:
    if not Path(app_path).exists():
        return False, f"introuvable : {app_path}"
    if running_from(app_path):
        return True, ALREADY
    # -g: do not come to the foreground. -j: start hidden. Both are settled at LAUNCH
    # time, hence without any Accessibility permission — it is the cleanest way never to
    # see the window of an app that has no business on screen flash by.
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
        glyphe = "↷" if msg == ALREADY else ("▶" if ok else "✖")
        log(f"  {glyphe} {name} — {msg}")
        # The settle delay waits for a FRESHLY launched app to be ready. An app that was
        # already there has been ready for a long time: waiting two more seconds per app
        # lengthened the bring-up for nothing, precisely in the most common case.
        if ok and msg != ALREADY:
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


def _live_processes() -> list[str]:
    """Command line of every running Ableton Live, whatever the install."""
    r = subprocess.run(["pgrep", "-fl", "/MacOS/Live"], capture_output=True, text=True)
    return [l.split(" ", 1)[1] for l in r.stdout.splitlines() if " " in l]


def _loopback_port(cfg: dict) -> str:
    """The MIDI port whose appearance proves Live has finished loading.

    It is READ from the config: it is an installation name, not a constant of the engine —
    by hard-coding it, we kept "Ableton Loopback" in the code for months after the bus had
    been renamed. The first `midi_required` is the right default: it is by definition the
    port without which the rig does not work.
    """
    ports = cfg.get("checks", {}).get("midi_required") or []
    return ports[0] if ports else "Rig Bus"


def _resolved_mode(cfg: dict, mode: str | None) -> str:
    from .. import checks          # late import: `checks` is heavy and is not required
    if mode:                      # to launch the apps — only to set the sound.
        return mode
    return checks.resolve_mode(cfg, cfg.get("mode", {}).get("default", "auto"))


def warn_if_output_missing(cfg: dict, mode: str, log=print) -> bool:
    """Is the expected output even PLUGGED IN? Said BEFORE opening the set.

    This is the only form of "set the output before launching Ableton" that exists: Live
    restores its own device at start-up and accepts no setting as long as it is not
    running (no AppleScript, no readable preferences, no Python API — see the README of
    `ableton-audio-output`). What we CAN do is refuse to discover the problem after the
    fact: if the expected interface is not plugged in, Live will open onto nothing, put up
    its modal dialog, and the rest of the bring-up will be fought against a deaf app.
    Better to announce it on the line where it is still repairable — by plugging a cable.
    """
    from .. import checks
    wants = cfg["modes"][mode].get("live_output") or []
    if not wants or not checks._audio_ready():
        return True               # nothing expected, or inventory not read yet: stay quiet
    names = [it.get("_name", "") for it in checks._audio_items()]
    if any(w.lower() in n.lower() for w in wants for n in names):
        return True
    log(f"  ⚠️  aucune sortie attendue n'est branchée (attendu : {' ou '.join(wants)})")
    log(f"      → Ableton va s'ouvrir sans son et poser sa fenêtre « section audio désactivée »")
    return False


def open_set(cfg: dict, log=print, dry_run: bool = False, mode: str | None = None) -> None:
    if not cfg["set"].get("open_after_launch", True):
        log("  (ouverture du set désactivée : [set].open_after_launch = false)")
        return
    project = cfg["set"]["project"]
    app = cfg["set"]["ableton_app"]
    if dry_run:
        pe = "" if Path(project).exists() else "  (set introuvable !)"
        ae = "" if Path(app).exists() else "  (Ableton introuvable !)"
        log(f"  [dry-run] ouvrirait « {Path(project).name} »{pe}")
        log(f"  [dry-run]   dans {Path(app).stem}{ae}")
        want = liveaudio.wanted(cfg, _resolved_mode(cfg, mode))
        log(f"  [dry-run] congédierait une éventuelle fenêtre modale de Live, "
            f"puis réglerait la sortie sur « {want or '(aucune déclarée)'} »")
        return
    if not Path(project).exists():
        log(f"  ✖ set introuvable : {project}")
        log(f"    → corrige [set].project dans rig.toml")
        return
    if not Path(app).exists():
        log(f"  ✖ Ableton introuvable : {app}")
        return
    # NEVER add a second Live. On 2026-08-29, a badly named config key made the engine
    # fall back on another Ableton installation, and this "open" launched Suite next to
    # the Suite 3 that was running: two Lives fighting over the same audio and MIDI
    # interfaces. Opening the project in the EXPECTED instance stays fine — macOS loads
    # it into the Live already there. It is the wrong install that we refuse.
    autres = [c for c in _live_processes() if not c.startswith(str(app))]
    if autres:
        log(f"  ✖ un autre Ableton tourne déjà — {Path(app).stem} ne sera pas lancé :")
        for c in autres:
            log(f"      {c}")
        log("    → quitte-le d'abord : deux Live ouverts se disputent audio et MIDI")
        return
    r = subprocess.run(["open", "-a", app, project], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  ✖ ouverture du set : {r.stderr.strip()}")
        return
    log(f"  ▶ ouverture de « {Path(project).name} » dans {Path(app).stem}")
    log(f"  … attente du port « {_loopback_port(cfg)} »")
    # A reminder halfway through, without any system permission: the most frequent cause
    # of a wait that drags on is a dialog waiting for an answer - "Live quit unexpectedly,
    # recover the work?" after a crash. It blocks the loading, so the port cannot appear,
    # and nothing on screen says so as long as you are looking at the terminal. One line
    # is enough to steer the eye towards the window, there where automating it would
    # require a permission.
    if _wait_for(lambda: _midi_port_present(_loopback_port(cfg)), timeout=20, interval=1):
        log("  ✔ Ableton en ligne")
        return
    log("  … toujours rien après 20s — si Live affiche un dialogue, réponds-lui "
        "(« récupérer le travail ? » → Non)")
    if _wait_for(lambda: _midi_port_present(_loopback_port(cfg)), timeout=40, interval=1):
        log("  ✔ Ableton en ligne")
    else:
        log("  ⚠️  Ableton pas encore prêt après 60s — gros set, plugins qui chargent, "
            "ou une fenêtre qui attend une réponse")

    # The sound is set HERE, not in the fix pass that follows. Two reasons, both paid for
    # on 2026-08-22: this is the first moment where it is POSSIBLE (Live has to be
    # running), and it is the last where it is still HARMLESS — the window tidying that
    # comes right after also drives the interface, and an un-dismissed modal dialog makes
    # it fail in turn. Dismiss first, set next, tidy after.
    cleared, note = liveaudio.dismiss_dialog()
    if note:
        log(f"  {'🧹' if cleared else '✖'} {note}")
    ok, msg = liveaudio.apply(cfg, _resolved_mode(cfg, mode))
    log(f"  {'🔊' if ok else '✖'} sortie d'Ableton — {msg}")



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
    """Tidy the windows at the end of the bring-up (see readyset/windows.py).

    After the launch, even when started hidden, apps that were already open before the
    preflight may linger on screen — and Ableton itself has just come to the front by
    opening the set. So this final pass leaves the screen in stage state: the set in
    front, the rest tidied away.
    """
    if not cfg.get("windows", {}).get("after_preflight", True):
        return
    log("  🪟 rangement des fenêtres…")
    windows.tidy(cfg, log=log, dry_run=dry_run)


def bring_up(cfg: dict, log=print, dry_run: bool = False, mode: str | None = None) -> None:
    log("Lancement des apps du rig…" if not dry_run else "Séquence de mise en place (dry-run) :")
    launch_apps(cfg, log=log, dry_run=dry_run)
    ensure_amphetamine_session(cfg, log=log, dry_run=dry_run)
    if not dry_run:
        warn_if_output_missing(cfg, _resolved_mode(cfg, mode), log=log)
    open_set(cfg, log=log, dry_run=dry_run, mode=mode)
    tidy_windows(cfg, log=log, dry_run=dry_run)


def _attendre_chargement(motif: str, calme: float, plafond: float, log) -> bool:
    """Wait until the DAW goes quiet. Returns false if there is nothing to observe.

    A fixed duration is a bet on the size of the set, and it gets it wrong on both sides:
    too short, the scene fires into a half-loaded set; too long, you stare at the screen
    doing nothing during the bring-up. The log, on the other hand, tells the truth — the
    DAW writes to it non-stop while it loads, and stops when it is done (measured: silence
    four seconds after the last action).
    """
    fichiers = [f for f in glob.glob(os.path.expanduser(motif)) if os.path.exists(f)]
    if not fichiers:
        return False
    f = max(fichiers, key=os.path.getmtime)
    debut = time.monotonic()
    mtime, dernier_ecrit = os.path.getmtime(f), time.monotonic()
    while time.monotonic() - debut < plafond:
        time.sleep(0.4)
        m = os.path.getmtime(f)
        if m != mtime:
            mtime, dernier_ecrit = m, time.monotonic()
        elif time.monotonic() - dernier_ecrit >= calme:
            log(f"  ✔ set chargé — journal silencieux depuis {calme:.0f}s "
                f"({time.monotonic() - debut:.0f}s d'attente)")
            return True
    log(f"  ⚠️  journal toujours actif après {plafond:.0f}s — on lance quand même")
    return True


def start_scene(cfg: dict, log=print, dry_run: bool = False) -> None:
    """Fire a scene of the set, once the set is loaded.

    Two messages, in that order: a CC whose VALUE is the scene number, then a note that
    triggers the selected scene. That pair is not invented here — it is the protocol the
    rig's control surface already speaks (scene 0 = reset, 1 = stop, 3 and beyond = the
    songs). Reusing it avoids a second mapping in the DAW, and above all avoids two
    truths about the same thing.

    Kept apart from open_set deliberately: opening a set and making it PLAY are two
    distinct decisions, and the second must not fire when the set is reopened in the
    middle of the evening to check a setting. It is the bring-up that calls it, after
    having set the audio output — in that order, otherwise the first bars would come out
    on the interface we have just corrected.
    """
    sc = cfg["set"].get("start_scene") or {}
    port = sc.get("port", "")
    if not port:
        return                       # not configured: nothing to do, and nothing to say
    canal = int(sc.get("channel", 1)) - 1
    num_cc, scene = int(sc.get("select_cc", 2)), int(sc.get("scene", 0))
    note = int(sc.get("trigger_note", 38))
    motif = sc.get("log_glob", "")
    if dry_run:
        log(f"  [dry-run] attendrait la fin du chargement puis lancerait la scène {scene} "
            f"(CC {num_cc} puis note {note}, canal {canal + 1}) sur « {port} »")
        return
    log("  … attente de la fin du chargement du set")
    if not (motif and _attendre_chargement(motif, float(sc.get("quiet_seconds", 2)),
                                           float(sc.get("max_seconds", 25)), log)):
        attente = float(sc.get("delay_seconds", 12))
        log(f"  (pas de journal à observer — attente fixe de {attente:.0f}s)")
        time.sleep(attente)
    cible = next((p for p in mido.get_output_names() if port.lower() in p.lower()), None)
    if cible is None:
        log(f"  ✖ départ du set : port « {port} » absent")
        return
    try:
        with mido.open_output(cible) as out:
            out.send(mido.Message("control_change", channel=canal,
                                  control=num_cc, value=scene))
            # Note-on ALONE, like the surface: {cc:1,2,0} then {noteon:1,38,127}. A
            # note-off had been added here out of hygiene; the rig has been running this
            # way for months without a hanging note, so the worry was theoretical and the
            # addition a gratuitous divergence from the reference protocol.
            out.send(mido.Message("note_on", channel=canal, note=note, velocity=127))
        log(f"  ▶ scène {scene} lancée — CC {num_cc} puis note {note}, canal {canal + 1}")
    except Exception as exc:         # a failed start must not sink the whole bring-up
        log(f"  ✖ départ du set : {exc}")
