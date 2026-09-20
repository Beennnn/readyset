"""Checks on SOUND — from the kernel service up to what the DAW says it plays through.

Ordered by cause: coreaudiod first (when it is down it explains everything below it),
then the Mac's default output, the interface, the named devices, and finally the DAW's
own output setting, which is the one that actually decides whether the room hears
anything.

`system_profiler` costs about a second, so its answer is cached here rather than at the
call site: the dashboard polls, and every reader would otherwise pay it.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import liveaudio
from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint
from .apps import _pgrep, _process_age


def _tail(path: str, nbytes: int = 200_000) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
        return fh.read().decode("utf-8", "ignore")


def _last_line_containing(path: str, needle: str, chunk: int = 256_000) -> str | None:
    """The LAST line of the file containing `needle`, searched for BY WALKING BACKWARDS.

    Why not `_tail`: Live only writes « Output Device » on a CHANGE, then tens of
    thousands of lines on top of it while it plays. A fixed-size window at the end
    therefore misses the line as soon as a chatty session has gone by — and the check
    answered « aucune sortie déclarée dans le journal » (yellow, vague, with useless
    advice since the output HAD been set at some point) while the last value written was
    « No Device », that is to say a complete silence to be announced in red.
    Measured on 2026-08-22: 6.8 MB of log, the line sought 1.4 MB from the end.
    """
    needle_b = needle.encode()
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        pos, carry = size, b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + carry).split(b"\n")
            # The first slice may have cut a line in two: it goes back in on the next
            # round, glued to whatever precedes it.
            carry = lines.pop(0)
            for line in reversed(lines):
                if needle_b in line:
                    return line.decode("utf-8", "ignore")
        if needle_b in carry:
            return carry.decode("utf-8", "ignore")
    return None


def check_live_output(cfg: dict, mode: str) -> Result:
    """Where Ableton's sound goes out — read from its Log.txt, not asked of Ableton.

    Two things to know in order to read this line without getting it wrong.

    First the label: it does NOT say « Live goes out on those three ». The check's
    name is fixed, and the list of ACCEPTED outputs (`live_output`, per mode: the
    stage keyboard on stage, the macOS output or the desk interface at the desk)
    only appears if the real output is not one of them — otherwise you were reading
    an enumeration where you expected a state. That was the complaint made on
    2026-08-18, and it was well founded.

    Then freshness: the Log.txt is ONLY written while Ableton is running. With Ableton
    closed, the last value stays readable indefinitely — and the old version returned
    a plain GREEN on a reading 18 h old, right next to an « Ableton lancé ❌ ». A state
    that cannot be observed is declared neither good nor bad: with Ableton closed, the
    line goes to INFO and says how far back the reading goes.
    """
    wants = cfg["modes"][mode]["live_output"]
    label = "Sortie audio d'Ableton"
    logs = sorted(
        glob.glob(os.path.expanduser("~/Library/Preferences/Ableton/Live*/Log.txt")),
        key=lambda p: os.path.getmtime(p), reverse=True,
    )
    if not logs:
        return Result("audio:live", label, WARN, "log Ableton introuvable")
    dev, when_iso = None, None
    try:
        line = _last_line_containing(logs[0], "Audio In Out: Output Device:")
        if line:
            dev = line.split("Output Device:", 1)[1].strip()
            # Split on « : » FOLLOWED BY A SPACE: the timestamp contains three of them
            # without a space (23:38:16), so a split on the first colon returned
            # « 2026-08-19T23 » — which fromisoformat accepts without flinching, reading
            # 23:00 exactly. The measured gap then became wrong by up to 59 minutes.
            when_iso = line.split(": ", 1)[0]         # 2026-08-19T23:38:16.911624
    except Exception as exc:
        return Result("audio:live", label, WARN, f"lecture log: {exc}")
    if dev is None:
        return Result("audio:live", label, WARN,
                      _hint("aucune sortie déclarée dans le journal de Live",
                            "régler la sortie une fois : la ligne apparaîtra et cette "
                            "vérification deviendra possible"))


    short = dev.split(" (")[0]
    ok = any(w.lower() in dev.lower() for w in wants)

    # « No Device » is not one output among others: it is the ABSENCE of an output, hence
    # a guaranteed silence from the very first note. Live RESTORES it at launch without
    # rewriting anything in the log (verified on 2026-08-22: launched at 16:47, last line
    # from 21/08 at 15:40 — and not a single sound). The freshness reasoning below holds
    # for a plausible output that could not be reconfirmed; here there is nothing to
    # qualify, Live's last known intent is « no device at all ». Red, and the fix sets
    # the output.
    if short.lower().startswith("no device"):
        stamp = (when_iso or "").replace("T", " ")[:16]
        return Result("audio:live", label, FAIL,
                      _hint(f"aucun périphérique de sortie (No Device{', du ' + stamp if stamp else ''})",
                            "Live ne sortira aucun son tant que ce n'est pas réglé"))

    # IS THE LINE FROM THIS SESSION? The Log.txt only writes « Output Device » on a
    # CHANGE, and the same file spans months: a line can therefore describe the session
    # from the day before yesterday while Live runs today on something else. Measured on
    # 2026-08-19: Live launched the previous day at 19:38, last line dating from 17 June,
    # and the output actually selected was « No Device » — that is, no sound at all.
    #
    # A value older than the launch therefore proves nothing about now. We declare it
    # neither good nor bad: we say it is not confirmed, and the fix (which SETS the
    # output) makes a fresh line appear, which makes the check conclusive. That is also
    # why the bring-up applies the output instead of trusting what it reads.
    age = _process_age(cfg["checks"]["apps"].get("Ableton", "Ableton Live.*/MacOS/Live"))
    if age is not None and when_iso:
        try:
            logged_ago = (datetime.now() - datetime.fromisoformat(when_iso)).total_seconds()
        except ValueError:
            logged_ago = None
        if logged_ago is not None and logged_ago > age:
            stamp = when_iso.replace("T", " ")[:16]
            return Result("audio:live", label, WARN,
                          _hint(f"non confirmée depuis le lancement de Live "
                                f"(dernière trace : {short}, du {stamp})",
                                "régler la sortie pour en avoir le cœur net"))


    # Ableton closed → nothing to observe: we return the last known value, dated, with
    # no verdict. Same detection pattern as the « Ableton lancé » check, so that the two
    # lines cannot contradict each other.
    pattern = cfg["checks"]["apps"].get("Ableton", "Ableton Live.*/MacOS/Live")
    if not _pgrep(pattern):
        when = datetime.fromtimestamp(os.path.getmtime(logs[0])).strftime("%d/%m à %H:%M")
        return Result("audio:live", label, INFO,
                      f"Ableton n'est pas lancé — dernière sortie connue : {short} ({when})")

    if ok:
        return Result("audio:live", label, OK, short)
    return Result("audio:live", label, FAIL,
                  _hint(f"sort sur {short}",
                        "attendu : " + " ou ".join(wants) +
                        " — à changer dans Live > Préférences > Audio"))


# system_profiler is slow (~1s); cache its JSON so audio + default-output checks
# (and a polling dashboard) share one call instead of shelling out repeatedly.
# ready: a read has succeeded at least once (otherwise we conclude NOTHING about the
# audio). running: a probe is in flight, no point launching a second one.
# `stalled`: the last read TIMED OUT (not failed — timed out). That is the signature of a
# frozen coreaudiod: the process is there, but nobody gets an answer out of it any more,
# and no app gets any sound out either. Lived through on 2026-09-12 (9 h of silence after
# a settings-writing loop triggered by a third-party driver). The `sys:coreaudio` check
# reads this flag; it therefore costs no extra process — the existing audio probe is enough.
_profile_cache: dict = {"ts": 0.0, "data": None, "ready": False, "running": False,
                        "stalled": False}


_PROFILE_TTL = 10.0


def audio_cache_reset() -> None:
    """Forget the « frozen » state and force a re-read at the next check — called by the
    fix that restarts coreaudiod, so that the line goes back to green as soon as the
    service answers, without waiting for the TTL to expire."""
    _profile_cache["stalled"] = False
    _profile_cache["ts"] = 0.0


def _audio_probe() -> None:
    """Query CoreAudio in the background and file the result away in the cache."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        _profile_cache["data"] = json.loads(out)
        _profile_cache["ready"] = True
        _profile_cache["stalled"] = False
    except subprocess.TimeoutExpired:
        # Twenty seconds with no answer is not « slow », it is frozen: normally the
        # inventory takes 1 to 3 s. We keep the last read (see below) AND we raise the
        # flag, so that `check_coreaudio` says it in red.
        _profile_cache["stalled"] = True
    except Exception:
        # Failure or blockage: we KEEP the last valid read rather than replacing it with
        # emptiness, which would make devices that are very much present blink red.
        # `ready` stays at its value, so the old answer goes on serving.
        pass
    finally:
        _profile_cache["running"] = False


def _audio_items() -> list[dict]:
    """The CoreAudio inventory, NEVER blocking — it returns whatever it has at hand.

    Why this detour through a thread rather than a plain call with a timeout: on
    2026-08-18, an `audiolevel` measurement left CoreAudio stuck, and `system_profiler
    SPAudioDataType` stopped giving control back. subprocess's `timeout=` was not
    enough — on expiry it KILLS the process, but a process blocked inside a kernel call
    does not die right away, and the wait overran by a lot. Result: /api/state no longer
    answered at all and the dashboard stayed on « …chargement ».

    A stage service must never depend on a system call that can freeze. The read
    therefore goes off in the background and the check answers immediately with the last
    known value; as long as no read has succeeded, `ready` is false and the audio checks
    SAY so (« lecture en cours ») instead of announcing a false absence.
    """
    now = time.time()
    if (not _profile_cache["running"]
            and now - _profile_cache["ts"] > _PROFILE_TTL):
        _profile_cache["ts"] = now
        _profile_cache["running"] = True
        threading.Thread(target=_audio_probe, daemon=True).start()
    data = _profile_cache["data"] or {}
    return [it for top in data.get("SPAudioDataType", []) for it in top.get("_items", [])]


def _audio_ready() -> bool:
    """True as soon as a CoreAudio read has succeeded at least once."""
    return bool(_profile_cache["ready"])


def check_audio(cfg: dict, mode: str = "live") -> Result | None:
    # The expected interface is a PER-MODE setting — the stage keyboard on stage, nothing
    # at the desk — and the global setting is only a fallback for a rig that has a single
    # mode. Reading the global one first was a silent bug of the same family as [set].app:
    # the config said « stage keyboard », the engine checked « USB Audio », its own generic
    # default, and the studio displayed a line it had specifically asked not to have.
    m = cfg["modes"].get(mode, {})
    # As soon as ONE mode declares its interface, declaring nothing becomes a choice, not
    # an oversight: the studio specifically wants no interface to be required. The global
    # fallback therefore only serves rigs that have no modes at all.
    par_mode = any("audio_interface" in v for v in cfg["modes"].values() if isinstance(v, dict))
    want = m.get("audio_interface") if par_mode else cfg["checks"].get("audio_interface")
    sev = m.get("interface_severity", "fail")
    if sev == OFF or not want:
        return None
    names = [it.get("_name", "") for it in _audio_items()]
    if not _audio_ready():
        return Result("audio", f"Interface audio « {want} »", INFO, "lecture CoreAudio en cours…")
    hit = any(want.lower() in n.lower() for n in names)
    # Same principle as for the applications: read in red, « Interface audio « … » »
    # asserts a presence that the colour contradicts. The label states what IS.
    return Result(
        key="audio",
        label=f"Interface audio « {want} »" if hit else f"Interface « {want} » introuvable",
        status=OK if hit else sev,
        detail="" if hit else ("non détectée" if sev == FAIL else "non détectée (OK en studio)"),
    )


def check_audio_devices(cfg: dict, mode: str) -> list[Result]:
    """ADDITIONAL audio devices to watch, one [[checks.audio_devices]] block per
    entry.

    Distinct from `check_audio` (the main interface) and from `check_live_output` (what
    Live REALLY uses): here we only observe that a device is present on the CoreAudio
    side. Use case: the stage keyboard exposes a USB sound card of its own on top of
    its MIDI port. Its MIDI can answer perfectly well while its audio output is not
    mounted — a half-dead USB cable, a hub that drops out — and you only notice it when
    you start the sound. The keyboard looks plugged in, the piano stays mute.

    `severity` per entry (warn by default), and can be a dict per mode:
        severity = { live = "fail", studio = "warn" }
    """
    names = [it.get("_name", "") for it in _audio_items()]
    out = []
    for dev in cfg["checks"].get("audio_devices", []):
        want = dev.get("match", "")
        sev = dev.get("severity", WARN)
        if isinstance(sev, dict):
            sev = sev.get(mode, WARN)
        if sev == OFF:
            continue
        if not _audio_ready():
            out.append(Result(key=f"audio:{dev.get('name', want)}",
                              label=dev.get("name", f"Périphérique audio « {want} »"),
                              status=INFO, detail="lecture CoreAudio en cours…"))
            continue
        hit = next((n for n in names if want.lower() in n.lower()), None)
        out.append(Result(
            key=f"audio:{dev.get('name', want)}",
            label=dev.get("name", f"Périphérique audio « {want} »"),
            status=OK if hit else sev,
            # This check ONLY proves presence on the CoreAudio side — never that sound
            # actually comes out that way. That is the limit of the observation: a card
            # can be mounted and stay mute (wrong output picked in Live, volume at
            # zero, dead cable on the jack side). The only verdict that counts is the
            # ear, hence the pointer to the Soundcheck page, which plays and lets you listen.
            detail=hit or _hint(
                "non détecté côté CoreAudio",
                "le rebrancher / le rallumer, puis JOUER du son pour vérifier qu'il "
                "sort bien par cette sortie (page 🎹 Soundcheck du dashboard) — "
                "être vu par le Mac ne prouve pas qu'on l'entend"),
        ))
    return out


def _coreaudiod_pid() -> int | None:
    r = subprocess.run(["pgrep", "-x", "coreaudiod"], capture_output=True, text=True)
    first = r.stdout.split()
    return int(first[0]) if first else None


def check_coreaudio(cfg: dict) -> Result:
    """Is the macOS audio service (coreaudiod) running AND answering?

    This is the check that explains all the others when « there is no sound any more »:
    the default output is right, the interface is detected, Ableton points at it — and
    nothing comes out, because the process that mixes all that is frozen. Without this
    line, you look for the fault in the apps; with it, you read the cause in one line
    and the button is right next to it. A frozen coreaudiod is never acceptable, in any
    mode: no configurable severity, it is red.
    """
    label = "Service audio macOS (coreaudiod)"
    pid = _coreaudiod_pid()
    if pid is None:
        # launchd normally resurrects it in ~1 s; seeing it absent twice in a row means
        # it is crash-looping — restarting it by hand will not be enough.
        return Result("sys:coreaudio", label, FAIL,
                      _hint("coreaudiod ne tourne pas",
                            "launchd devrait le relancer seul ; s'il reste absent, "
                            "un pilote audio tiers le fait planter au démarrage"))
    if _profile_cache["stalled"]:
        return Result("sys:coreaudio", label, FAIL,
                      "figé : le processus tourne mais ne répond plus — aucune app "
                      "ne peut sortir de son tant qu'il n'est pas relancé")
    return Result("sys:coreaudio", label, OK, f"répond (pid {pid})")


def check_default_output(cfg: dict) -> Result:
    """The macOS default sound output must be the Mac itself (built-in), not an
    external / AirPlay / conferencing device."""
    want = cfg["checks"].get("default_output_match", "MacBook")
    name = None
    for it in _audio_items():
        if it.get("coreaudio_default_audio_output_device") == "spaudio_yes":
            name = it.get("_name", "")
            break
    if name is None:
        # « Not read yet » and « read, nothing found » are not the same thing: the first
        # is waiting, the second a genuine fault. Confusing them would make a warning
        # blink at every start-up of the service.
        return Result("sys:output", "Sortie son par défaut (Mac)",
                      INFO if not _audio_ready() else WARN,
                      "lecture CoreAudio en cours…" if not _audio_ready() else "indéterminée")
    ok = want.lower() in name.lower()
    return Result(
        key="sys:output", label="Sortie son par défaut (Mac)",
        status=OK if ok else FAIL,
        detail=f"actuellement : {name}" if not ok else name,
    )
