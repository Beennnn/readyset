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
    """La DERNIÈRE ligne du fichier qui contient `needle`, cherchée EN REMONTANT.

    Pourquoi pas `_tail` : Live n'écrit « Output Device » qu'au CHANGEMENT, puis des
    dizaines de milliers de lignes par-dessus pendant qu'il joue. Une fenêtre de fin de
    taille fixe rate donc la ligne dès qu'une session bavarde est passée derrière — et le
    check répondait « aucune sortie déclarée dans le journal » (jaune, vague, avec un
    conseil inutile puisque la sortie AVAIT déjà été réglée un jour) alors que la dernière
    valeur écrite était « No Device », c'est-à-dire un silence complet à annoncer en rouge.
    Mesuré le 2026-08-22 : 6,8 Mo de journal, la ligne cherchée à 1,4 Mo de la fin.
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
            # La première tranche peut avoir coupé une ligne en deux : elle repart au tour
            # suivant, recollée à ce qui la précède.
            carry = lines.pop(0)
            for line in reversed(lines):
                if needle_b in line:
                    return line.decode("utf-8", "ignore")
        if needle_b in carry:
            return carry.decode("utf-8", "ignore")
    return None


def check_live_output(cfg: dict, mode: str) -> Result:
    """Où sort le son d'Ableton — lu dans son Log.txt, pas demandé à Ableton.

    Deux choses à savoir pour lire cette ligne sans se tromper.

    D'abord le libellé : il ne dit PAS « Live sort sur ces trois-là ». Le nom du
    check est fixe, et la liste des sorties ACCEPTÉES (`live_output`, par mode :
    le P-225 sur scène, la sortie macOS ou la RME au bureau) n'apparaît que si la
    sortie réelle n'en fait pas partie — sinon on lisait une énumération là où on
    attendait un état. C'était le reproche fait le 2026-08-18, et il était fondé.

    Ensuite la fraîcheur : le Log.txt n'est écrit QUE quand Ableton tourne. Ableton
    fermé, la dernière valeur reste lisible indéfiniment — et l'ancienne version
    rendait un VERT franc sur une lecture vieille de 18 h, à côté d'un « Ableton
    lancé ❌ ». Un état qu'on ne peut pas constater ne se déclare ni bon ni mauvais :
    Ableton fermé, la ligne passe en INFO et dit à quand remonte la lecture.
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
            # Découpe sur « : » SUIVI D'UN ESPACE : l'horodatage en contient trois
            # sans espace (23:38:16), donc un split sur le premier deux-points rendait
            # « 2026-08-19T23 » — que fromisoformat accepte sans broncher, en lisant
            # 23 h pile. L'écart mesuré devenait faux jusqu'à 59 minutes.
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

    # « No Device » n'est pas une sortie parmi d'autres : c'est l'absence de sortie, donc
    # un silence garanti dès la première note. Live le RESTAURE au lancement sans rien
    # réécrire dans le journal (vérifié le 2026-08-22 : lancé à 16:47, dernière ligne du
    # 21/08 à 15:40 — et pas un son). Le raisonnement de fraîcheur ci-dessous vaut pour
    # une sortie plausible qu'on n'a pas pu reconfirmer ; ici il n'y a rien à nuancer,
    # la dernière volonté connue de Live est « aucun périphérique ». Rouge, et le
    # correctif règle la sortie.
    if short.lower().startswith("no device"):
        stamp = (when_iso or "").replace("T", " ")[:16]
        return Result("audio:live", label, FAIL,
                      _hint(f"aucun périphérique de sortie (No Device{', du ' + stamp if stamp else ''})",
                            "Live ne sortira aucun son tant que ce n'est pas réglé"))

    # LA LIGNE EST-ELLE DE CETTE SESSION ? Le Log.txt n'écrit « Output Device » qu'au
    # CHANGEMENT, et le même fichier couvre des mois : une ligne peut donc décrire la
    # session d'avant-hier pendant que Live tourne aujourd'hui sur autre chose. Mesuré le
    # 2026-08-19 : Live lancé la veille à 19:38, dernière ligne datant du 17 juin, et la
    # sortie réellement sélectionnée était « No Device » — soit aucun son du tout.
    #
    # Une valeur antérieure au lancement ne prouve donc rien sur maintenant. On ne la
    # déclare ni bonne ni mauvaise : on dit qu'elle n'est pas confirmée, et le correctif
    # (qui RÈGLE la sortie) fait apparaître une ligne fraîche, ce qui rend le check
    # concluant. C'est aussi pour ça que la mise en place applique la sortie au lieu de
    # se fier à ce qu'elle lit.
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


    # Ableton fermé → rien à constater : on rend la dernière valeur connue, datée,
    # sans verdict. Même motif de détection que le check « Ableton lancé », pour que
    # les deux lignes ne puissent pas se contredire.
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
# ready : une lecture a abouti au moins une fois (sinon on ne conclut RIEN sur
# l'audio). running : une sonde est en vol, inutile d'en lancer une seconde.
# `stalled` : la dernière lecture a EXPIRÉ (pas échoué — expiré). C'est la signature d'un
# coreaudiod figé : le processus est là, mais plus personne n'obtient de réponse de lui,
# et plus aucune app ne sort de son. Vécu le 2026-09-12 (9 h de silence après une boucle
# d'écriture de réglages déclenchée par un pilote tiers). Le check `sys:coreaudio` lit ce
# drapeau ; il ne coûte donc aucun processus de plus — la sonde audio existante suffit.
_profile_cache: dict = {"ts": 0.0, "data": None, "ready": False, "running": False,
                        "stalled": False}


_PROFILE_TTL = 10.0


def audio_cache_reset() -> None:
    """Oublie l'état « figé » et force une relecture au prochain check — appelé par le
    correctif qui relance coreaudiod, pour que la ligne repasse au vert dès que le
    service répond, sans attendre la fin du TTL."""
    _profile_cache["stalled"] = False
    _profile_cache["ts"] = 0.0


def _audio_probe() -> None:
    """Interroge CoreAudio en tâche de fond et range le résultat dans le cache."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        _profile_cache["data"] = json.loads(out)
        _profile_cache["ready"] = True
        _profile_cache["stalled"] = False
    except subprocess.TimeoutExpired:
        # Vingt secondes sans réponse, ce n'est pas « lent », c'est figé : en temps
        # normal l'inventaire prend 1 à 3 s. On garde la dernière lecture (voir ci-dessous)
        # ET on lève le drapeau, pour que `check_coreaudio` le dise en rouge.
        _profile_cache["stalled"] = True
    except Exception:
        # Échec ou blocage : on GARDE la dernière lecture valable plutôt que de la
        # remplacer par du vide, qui ferait clignoter en rouge des appareils bien
        # présents. `ready` reste à sa valeur, donc l'ancienne réponse continue de servir.
        pass
    finally:
        _profile_cache["running"] = False


def _audio_items() -> list[dict]:
    """L'inventaire CoreAudio, JAMAIS bloquant — il rend ce qu'il a sous la main.

    Pourquoi ce détour par un thread plutôt qu'un simple appel avec délai : le
    2026-08-18, une mesure `audiolevel` a laissé CoreAudio coincé, et `system_profiler
    SPAudioDataType` a cessé de rendre la main. Le `timeout=` de subprocess n'a pas
    suffi — expiré, il TUE le processus, mais un processus bloqué dans un appel noyau
    ne meurt pas tout de suite, et l'attente débordait largement. Résultat : /api/state
    ne répondait plus du tout et le dashboard restait sur « …chargement ».

    Un service de scène ne doit jamais dépendre d'un appel système qui peut se figer.
    La lecture part donc en tâche de fond et le check répond immédiatement avec la
    dernière valeur connue ; tant qu'aucune lecture n'a abouti, `ready` est faux et les
    checks audio le DISENT (« lecture en cours ») au lieu d'annoncer une absence fausse.
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
    """Vrai dès qu'une lecture CoreAudio a abouti au moins une fois."""
    return bool(_profile_cache["ready"])


def check_audio(cfg: dict, mode: str = "live") -> Result | None:
    # L'interface attendue est un réglage PAR MODE — le P-225 sur scène, rien au bureau —
    # et le réglage global n'est qu'un repli pour un rig qui n'a qu'un mode. Lire le
    # global d'abord était un bug silencieux de la même famille que [set].app : la config
    # disait « P-225 », le moteur vérifiait « USB Audio », son propre défaut générique,
    # et le studio affichait une ligne qu'il avait justement demandé à ne pas avoir.
    m = cfg["modes"].get(mode, {})
    # Dès qu'UN mode déclare son interface, ne rien déclarer devient un choix, pas un
    # oubli : le studio veut justement qu'aucune interface ne soit exigée. Le repli
    # global ne sert donc qu'aux rigs qui n'ont pas de modes du tout.
    par_mode = any("audio_interface" in v for v in cfg["modes"].values() if isinstance(v, dict))
    want = m.get("audio_interface") if par_mode else cfg["checks"].get("audio_interface")
    sev = m.get("interface_severity", "fail")
    if sev == OFF or not want:
        return None
    names = [it.get("_name", "") for it in _audio_items()]
    if not _audio_ready():
        return Result("audio", f"Interface audio « {want} »", INFO, "lecture CoreAudio en cours…")
    hit = any(want.lower() in n.lower() for n in names)
    # Même principe que pour les applications : lu en rouge, « Interface audio « RME » »
    # affirme une présence que la couleur dément. Le libellé dit ce qui EST.
    return Result(
        key="audio",
        label=f"Interface audio « {want} »" if hit else f"Interface « {want} » introuvable",
        status=OK if hit else sev,
        detail="" if hit else ("non détectée" if sev == FAIL else "non détectée (OK en studio)"),
    )


def check_audio_devices(cfg: dict, mode: str) -> list[Result]:
    """Périphériques audio SUPPLÉMENTAIRES à surveiller, un bloc [[checks.audio_devices]]
    par entrée.

    Distinct de `check_audio` (l'interface principale) et de `check_live_output` (ce que
    Live utilise VRAIMENT) : ici on constate seulement qu'un périphérique est présent
    côté CoreAudio. Cas d'usage : le P-225 expose une carte son USB « P-Series » en plus
    de son port MIDI. Son MIDI peut très bien répondre alors que sa sortie audio, elle,
    n'est pas montée — un câble USB à moitié mort, un hub qui décroche — et on ne s'en
    aperçoit qu'en lançant le son. Le clavier a l'air branché, le piano reste muet.

    `severity` par entrée (défaut warn), et peut être un dict par mode :
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
            # Ce check ne prouve QUE la présence côté CoreAudio — jamais qu'un son
            # sort réellement par là. C'est la limite du constat : une carte peut
            # être montée et rester muette (mauvaise sortie choisie dans Live, volume
            # à zéro, câble mort côté jack). Le seul verdict qui vaut est l'oreille,
            # d'où le renvoi vers la page Soundcheck, qui joue et fait écouter.
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
    """Le service audio de macOS (coreaudiod) tourne-t-il ET répond-il ?

    C'est le check qui explique tous les autres quand « il n'y a plus de son » : la
    sortie par défaut est bonne, l'interface est détectée, Ableton pointe dessus — et
    rien ne sort, parce que le processus qui mixe tout ça est figé. Sans cette ligne,
    on cherche le défaut dans les apps ; avec, on lit la cause en une ligne et le bouton
    est à côté. Un coreaudiod figé n'est jamais acceptable, dans aucun mode : pas de
    sévérité configurable, c'est rouge.
    """
    label = "Service audio macOS (coreaudiod)"
    pid = _coreaudiod_pid()
    if pid is None:
        # launchd le ressuscite normalement en ~1 s ; le voir absent deux fois de suite
        # veut dire qu'il boucle en crash — le relancer à la main ne suffira pas.
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
        # « Pas encore lu » et « lu, rien trouvé » ne sont pas la même chose : le premier
        # est de l'attente, le second un vrai défaut. Les confondre ferait clignoter un
        # avertissement à chaque démarrage du service.
        return Result("sys:output", "Sortie son par défaut (Mac)",
                      INFO if not _audio_ready() else WARN,
                      "lecture CoreAudio en cours…" if not _audio_ready() else "indéterminée")
    ok = want.lower() in name.lower()
    return Result(
        key="sys:output", label="Sortie son par défaut (Mac)",
        status=OK if ok else FAIL,
        detail=f"actuellement : {name}" if not ok else name,
    )
