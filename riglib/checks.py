"""Health checks — the source of truth for both preflight and monitor.

Each check returns a Result. Four levels:
  ok    vert   — présent / lancé comme attendu
  info  bleu   — absent, et c'est normal : de l'optionnel non branché (lampes d'ambiance,
                 interface audio au bureau, réseau de scène quand on n'y est pas). Visible
                 dans le dashboard, mais ne pèse NI sur le verdict NI sur le code de sortie.
  warn  orange — quelque chose manque et ça mérite un coup d'œil ; pas bloquant
  fail  rouge  — un élément requis manque ; le rig n'est pas prêt à jouer

The checks are cheap (pgrep, an in-process MIDI port enumeration) except audio,
which shells out to system_profiler (~1s) and is therefore rate-limited by the
monitor loop rather than run every cycle.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import mido

from . import apps, vpn

# Quatre niveaux, du plus calme au plus grave. INFO est SOUS l'avertissement : il dit
# « absent, et c'est normal » — un équipement facultatif qu'on n'a simplement pas branché
# ce soir (lampes d'ambiance, interface audio au bureau). C'est le niveau qui manquait :
# tout mettre en WARN faisait clignoter en orange des choses dont personne ne se soucie,
# et à force on ne lit plus les oranges du tout — y compris les vrais.
# Un check INFO ne rend PAS le rig « non prêt » : `worst()` l'ignore, le code de sortie
# reste 0, et la pastille/le liseré ne s'allument pas.
OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
_ICON = {OK: "✅", INFO: "ℹ️ ", WARN: "⚠️ ", FAIL: "❌"}

# Valeur spéciale acceptée partout où une sévérité se règle (breath_severity,
# interface_severity, network_severity, lamp_severity, [[checks.audio_devices]].severity…) :
# le check ne s'exécute PAS et n'apparaît nulle part dans ce mode.
#
# À ne pas confondre avec INFO. "info" dit « absent, et c'est normal » — la ligne reste
# affichée, on sait que la question a été posée. "off" dit « la question n'a pas de sens
# ici » : sur scène le son sort du P-225, la RME n'est même pas dans le sac, donc afficher
# « RME non détectée » chaque soir n'apprend rien à personne et allonge la liste à lire
# avant de jouer. Ne l'utiliser que pour ça — masquer un vrai problème derrière "off",
# c'est se rendre aveugle.
OFF = "off"


@dataclass
class Result:
    key: str        # stable id for state tracking (e.g. "app:Ableton")
    label: str      # human label
    status: str     # ok | info | warn | fail
    detail: str = ""

    @property
    def icon(self) -> str:
        return _ICON[self.status]

    @property
    def ok(self) -> bool:
        return self.status == OK

    # Le contenu d'un check COMPOSITE : ses sous-éléments, chacun vu ou pas encore.
    # Un check reste UNE ligne — c'est ce qui garde la liste lisible d'un coup d'œil —
    # mais il énumérait alors ses manquants dans son texte, où toute surface étroite les
    # tronque (« … : Pédale, Notes, Souff… »). Détaillés ici, ceux qui ont la place les
    # déplient au lieu de les couper. None quand le check n'a rien à détailler.
    # Chacun : {"name": str, "ok": bool, "icon": str} — l'icône illustre le GESTE à faire,
    # et se lit avant le mot ; elle est facultative, un nom seul reste parfaitement lisible.
    parts: list[dict] | None = None

    def to_dict(self) -> dict:
        d = {"key": self.key, "label": self.label,
             "status": self.status, "detail": self.detail}
        if self.parts is not None:
            d["parts"] = [{"name": p["name"], "ok": bool(p.get("ok")),
                           "icon": p.get("icon") or ""} for p in self.parts]
        return d


def _source_label(phone: dict | None) -> str:
    """Qui a fait le relevé — le téléphone qui publie, ou le Mac qui interroge.

    Ce n'est pas de la décoration : les deux n'ont pas les mêmes angles morts. Le
    téléphone ne parle qu'au branchement (donc peut se taire longtemps sans que rien
    n'aille mal) ; le Mac bat régulièrement mais s'arrête net si l'appairage saute.
    Savoir laquelle des deux vient de parler, c'est savoir quoi aller vérifier.
    """
    src = (phone or {}).get("source", "")
    if src.startswith("mac:"):
        return "le Mac en USB" if src.endswith("usb") else "le Mac en Wi-Fi"
    return "le téléphone"


def _ago(seconds: float | None) -> str:
    """« il y a 12 s » / « il y a 4 min » — l'âge d'une observation, en toutes lettres.

    Un horodatage brut oblige à faire la soustraction de tête, juste avant de jouer.
    """
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:                       # au-delà d'une heure et demie, « 120 min »
        return f"{int(seconds // 60)} min"   # oblige à diviser de tête pour situer
    return f"{seconds / 3600:.0f} h"


def _hint(observed: str, advice: str) -> str:
    """Colle un conseil au constat : « ce que je vois → ce que tu peux faire ».

    Un check rouge sans conseil oblige à se souvenir du geste — et sur scène, cinq
    minutes avant de jouer, c'est exactement ce qui manque. Le constat reste devant
    (c'est lui qui est vrai), le conseil suit. Réservé au matériel : quand une
    résolution automatique existe, c'est un bouton de remedy.py qu'il faut, pas une
    phrase (personne ne lit un conseil qu'une machine pourrait exécuter).
    """
    # Le saut de ligne est SÉMANTIQUE, pas décoratif : chaque affichage le rend à sa
    # façon. Le dashboard est en `white-space: pre-line` et met le conseil sur sa propre
    # ligne — avant, un `nowrap` + ellipse coupait les conseils en plein milieu, ce qui
    # est pire que pas de conseil. Le terminal, lui, les recolle et enroule tout seul.
    return f"{observed}\n→ {advice}"


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


def _midi_inputs() -> list[str]:
    from .midi_lock import MIDI_LOCK
    try:
        with MIDI_LOCK:
            names = list(mido.get_input_names())
    except Exception as exc:  # pragma: no cover - backend init failure
        return [f"__error__:{exc}"]
    # CoreMIDI can hand back an endpoint whose name reads as None: a device that
    # vanished while the process kept its client open leaves a nameless ghost behind.
    # A fresh process never sees it, which is why `rig check` stayed green while the
    # long-lived dashboard crashed on EVERY request for 18 h (2026-08-18, after the
    # Dell dock dropped the whole USB chain). Filtering here fixes every caller at
    # once — several of them do `p.lower()` and would raise the same way.
    return [n for n in names if isinstance(n, str) and n]


def check_midi(cfg: dict) -> list[Result]:
    ports = _midi_inputs()
    if ports and ports[0].startswith("__error__:"):
        return [Result("midi:backend", "Backend MIDI", FAIL, ports[0].split(":", 1)[1])]

    def present(name: str) -> bool:
        return any(name.lower() in p.lower() for p in ports)

    out = []
    for name in cfg["checks"]["midi_required"]:
        hit = present(name)
        out.append(Result(
            key=f"midi:{name}", label=f"Port MIDI « {name} »",
            status=OK if hit else FAIL,
            detail="" if hit else "absent",
        ))
    return out


def _present(substr: str) -> str | None:
    """Return the first MIDI input name containing substr (case-insensitive), else None."""
    for p in _midi_inputs():
        if not p.startswith("__error__:") and substr.lower() in p.lower():
            return p
    return None


def check_keyboard(cfg: dict, mode: str) -> Result:
    """Three tiers: a preferred keyboard (keyboard_ok, e.g. Digital Piano / P-225) →
    green; only a fallback (keyboard_warn, e.g. microKey Air) → yellow; none → red.
    So in the studio microKey alone works but warns; the P-225 always satisfies."""
    m = cfg["modes"][mode]
    ok_list = m.get("keyboard_ok", m.get("keyboard", []))
    warn_list = m.get("keyboard_warn", [])
    # One _present() per candidate, not two: each call re-enumerates CoreMIDI under
    # the MIDI lock, so the double evaluation doubled the cost of the check.
    found_ok = next((hit for a in ok_list if (hit := _present(a))), None)
    if found_ok:
        return Result("kbd:keyboard", "Clavier", OK, f"détecté : {found_ok}")
    found_warn = next((hit for a in warn_list if (hit := _present(a))), None)
    if found_warn:
        return Result("kbd:keyboard", "Clavier", WARN,
                      _hint(f"{found_warn} (clavier principal absent)",
                            "allumer le clavier principal et le brancher en USB"))
    return Result("kbd:keyboard", "Clavier", FAIL,
                  _hint("aucun clavier branché",
                        "l'allumer et le brancher en USB au Mac"))


def check_breath(cfg: dict, mode: str) -> Result | None:
    """Breath controller. Severity depends on mode: part of the live rig (fail),
    optional at the desk (warn)."""
    sev = cfg["modes"][mode].get("breath_severity", "warn")
    if sev == OFF:
        return None
    found = _present(cfg["checks"].get("breath_port", "Breath Controller"))
    if found:
        return Result("kbd:breath", "Breath controller", OK, f"détecté : {found}")
    return Result("kbd:breath", "Breath controller", sev,
                  "absent" if sev == FAIL else "absent (optionnel en studio)")


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


def _ping(host: str, timeout_s: int = 1) -> bool:
    try:
        return subprocess.run(["ping", "-c1", f"-t{timeout_s}", host],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout_s + 2).returncode == 0
    except Exception:
        return False


def resolve_mode(cfg: dict, requested: str = "auto") -> str:
    """live | studio | auto → concrete mode. Auto = studio when the studio router
    (192.168.1.1) answers, else live. So the tool boots live and flips to studio
    only once it sees the home network."""
    if requested in ("live", "studio"):
        return requested
    return "studio" if _ping(cfg["checks"].get("studio_router", "192.168.1.1")) else "live"


def _network_severity(cfg: dict, mode: str) -> str:
    """Severity of the two stage-network checks, per mode. Both answer the same physical
    question — « suis-je branché sur le réseau de scène ? » — so they share one knob:
    blocking on stage (no modem = no iPhone, no lamps, no remote), informational at the
    desk, where that network is simply somewhere else and can never answer."""
    return cfg["modes"].get(mode, {}).get("network_severity", "fail")


def check_stage_network(cfg: dict, mode: str) -> Result | None:
    """Le réseau de scène, en UNE ligne à deux étages plutôt qu'en deux checks.

    Ils posaient déjà la même question physique — « suis-je sur le réseau de scène ? » —
    au point de partager un seul réglage de sévérité. Séparés, ils s'allumaient toujours
    ensemble et coûtaient deux lignes pour un seul fait.

    L'ordre du diagnostic va de la cause à la conséquence : le modem D'ABORD, parce que
    c'est lui qui distribue les adresses. Modem éteint → aucune IP possible, et annoncer
    « le Mac n'est pas sur le réseau » ferait chercher du côté du Mac un problème qui est
    dans la mallette. Modem debout mais pas d'IP → là seulement, c'est le Mac (câble,
    mauvais WiFi). Le cas inverse existe aussi : une IP et un modem muet, c'est-à-dire un
    modem qui a donné le bail puis a lâché.
    """
    sev = _network_severity(cfg, mode)
    if sev == OFF:
        return None
    prefix = cfg["checks"].get("stage_network", "192.168.1")
    host = cfg["checks"].get("modem_host", "192.168.1.1")
    label = f"Réseau de scène ({prefix}.x)"

    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("net:stage", label, WARN, f"ifconfig: {exc}")
    ip = next((w for line in out.splitlines() if f"inet {prefix}." in line
               for w in line.split() if w.startswith(f"{prefix}.")), None)
    modem = _ping(host)

    if modem and ip:
        return Result("net:stage", label, OK, f"Mac en {ip} · modem {host} répond")

    if sev != FAIL:      # au bureau, ce réseau est simplement ailleurs
        return Result("net:stage", label, sev, "absent (normal hors scène)")

    if not modem and not ip:
        detail = _hint(f"le modem {host} ne répond pas, et le Mac n'a pas d'IP en {prefix}.x",
                       "allumer le modem de scène EN PREMIER : c'est lui qui distribue les "
                       "adresses, le Mac ne peut rien obtenir tant qu'il est éteint")
    elif not modem:
        detail = _hint(f"Mac en {ip}, mais le modem {host} ne répond pas",
                       "le modem a donné son adresse puis s'est tu — le rallumer ; "
                       "sans lui, ni iPhone, ni lampes, ni télécommande")
    else:
        detail = _hint(f"le modem {host} répond, mais le Mac n'a pas d'IP en {prefix}.x",
                       "côté Mac : vérifier le câble, ou qu'il est bien sur le WiFi de "
                       "scène et pas resté sur un autre réseau")
    return Result("net:stage", label, sev, detail)


def _streamdeck_specs(cfg: dict) -> list[dict]:
    """Normalise both config shapes into [{name, serial, products}].

    Legacy shape (still accepted): streamdecks = { XL = "Stream Deck XL", … } — one
    product-name substring per deck. Rich shape: a list of [[checks.streamdecks]]
    tables carrying a serial AND one or more product names.
    """
    raw = cfg["checks"].get("streamdecks", {"XL": "Stream Deck XL", "Plus": "Stream Deck +"})
    if isinstance(raw, dict):
        return [{"name": k, "serial": "", "products": [v]} for k, v in raw.items()]
    specs = []
    for deck in raw:
        products = deck.get("product", [])
        if isinstance(products, str):
            products = [products]
        specs.append({"name": deck.get("name", "?"),
                      "serial": deck.get("serial", ""),
                      "products": products})
    return specs


def check_streamdeck(cfg: dict) -> list[Result]:
    """Each Stream Deck must be present on USB (via ioreg — SPUSBDataType is empty on
    this Mac). 'Asleep' (dimmed screen) is an app-internal state we can't read.

    A deck matches on its USB **serial** first, product name second, and passes if
    EITHER hits. Why not product name alone (the pre-2026-08-17 criterion): the
    marketing string ioreg exposes isn't stable — the Plus enumerates as "Stream Deck +"
    on some firmwares, so a lone "Stream Deck Plus" substring reports a genuinely
    plugged deck as absent. The serial never changes; read it off the Stream Deck app's
    own device id in any profile manifest — `"UUID": "@(1)[vid/pid/SERIAL]"`.

    Never match the bare string "Stream Deck": the app opens every USB device on the
    bus, leaving AppleUSBHostDeviceUserClient nodes *named* "Stream Deck" hanging off
    unrelated hardware (the Dell dock, hubs…). They're there with no deck plugged at
    all — matching them would turn this check into a permanent green light. Quoting the
    needle (`"Stream Deck XL"`) is what keeps us on the `= "…"` property values.
    """
    try:
        # -l so idVendor/idProduct/kUSBSerialNumberString are printed, not just names.
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception as exc:
        return [Result("usb:streamdeck", "Stream Deck (USB)", WARN, f"ioreg: {exc}")]
    res = []
    for spec in _streamdeck_specs(cfg):
        label, serial = spec["name"], spec["serial"]
        hit = f"n° série {serial}" if serial and f'"{serial}"' in out else ""
        if not hit:
            hit = next((p for p in spec["products"] if f'"{p}"' in out), "")
        res.append(Result(f"usb:{label}", f"Stream Deck {label}",
                          OK if hit else FAIL,
                          f"branché ({hit})" if hit else
                          _hint("non détecté en USB",
                                "le brancher en USB ; s'il passe par le dock, "
                                "vérifier que le dock est alimenté")))
    return res


def _ip_for_mac(mac: str) -> str | None:
    """Resolve a device's current IP from its MAC via the ARP table (the lamps get
    reserved-but-variable DHCP addresses, so MAC is the stable key)."""
    target = mac.lower().replace("-", ":")
    target = ":".join(p.zfill(2) for p in target.split(":"))
    try:
        out = subprocess.run(["arp", "-a", "-n"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "(" not in line or ")" not in line:
            continue
        ip = line.split("(", 1)[1].split(")", 1)[0]
        m = line.split(" at ", 1)[1].split()[0] if " at " in line else ""
        norm = ":".join(p.zfill(2) for p in m.lower().split(":")) if m else ""
        if norm == target:
            return ip
    return None


def check_lamps(cfg: dict, mode: str) -> list[Result]:
    """Stage lamps L1/L2 (Tuya). Found by MAC in the ARP table, then pinged. Warn-level
    (ambiance, not sound-critical); ARP only sees them once they've talked on the net."""
    sev = cfg["checks"].get("lamp_severity", WARN)
    if sev == OFF:
        return []
    # Une seule ligne pour toutes les lampes : elles s'allument ensemble, s'éteignent
    # ensemble et se réparent du même geste. Une ligne par lampe répétait deux fois le
    # même fait — et avec quatre lampes le tableau ne parlerait plus que d'elles. Le
    # détail les NOMME quand même : c'est le nom qui manque quand une seule tombe.
    up, silent, missing = [], [], []
    for lamp in cfg["checks"].get("lamps", []):
        name = lamp.get("name", "?")
        ip = _ip_for_mac(lamp.get("mac", ""))
        if ip and _ping(ip):
            up.append(f"{name} ({ip})")
        elif ip:
            silent.append(f"{name} ({ip})")
        else:
            missing.append(name)
    if not (up or silent or missing):
        return []
    label = "Lampes de scène"
    if not silent and not missing:
        return [Result("lamp:all", label, OK, "connectées : " + ", ".join(up))]
    constat = []
    if up:
        constat.append("connectée(s) : " + ", ".join(up))
    if silent:
        constat.append("adresse prise mais muette(s) : " + ", ".join(silent))
    if missing:
        constat.append("introuvable(s) : " + ", ".join(missing))
    return [Result("lamp:all", label, sev,
                   _hint(" · ".join(constat),
                         "les allumer et vérifier qu'elles sont visibles sur le réseau"))]


def _instant_amperage() -> int | None:
    try:
        out = subprocess.run(["ioreg", "-rn", "AppleSmartBattery"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if '"InstantAmperage"' in line:
            try:
                v = int(line.split("=")[1].strip())
                if v > 2**63:          # some Macs report unsigned; fold to signed
                    v -= 2**64
                return v
            except Exception:
                return None
    return None


def check_mac_power(cfg: dict, mode: str) -> Result:
    """Mac must be on AC. Unplugged → mode-based (fail live / warn studio). Plugged BUT
    the battery is draining (load > adapter) → always FAIL ('se vide même en charge')."""
    sev = cfg["modes"][mode].get("mac_power_severity", "warn")
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("sys:macpower", "Alimentation Mac", WARN, f"pmset: {exc}")
    import re
    mp = re.search(r"(\d+%)", out)
    pct = mp.group(1) if mp else "?"
    plugged = "'AC Power'" in out
    draining = "discharging" in out.lower()
    if plugged and not draining:
        amp = _instant_amperage()
        if amp is not None and amp < 0:
            draining = True
    if not plugged:
        return Result("sys:macpower", "Alimentation Mac", sev, f"sur batterie ({pct})")
    if draining:
        return Result("sys:macpower", "Alimentation Mac", FAIL, f"branché mais se décharge ! ({pct})")
    return Result("sys:macpower", "Alimentation Mac", OK, f"branché ({pct})")


def check_iphone_charge(cfg: dict, mode: str, acked: bool = False,
                       phone: dict | None = None) -> Result | None:
    """iPhone charging — not detectable from the Mac (the iPhone charges on a separate
    charger and talks to Bome over Wi-Fi, so it never appears here).

    Two ways to know, in order of trust: the PHONE says so (POST /api/phone, a shortcut
    on the phone — a real observation, timestamped, that stops being true on its own), or
    you tick it by hand (a declaration, which survives unplugging the phone). Neither →
    fail on stage, mere info at the desk.

    Rejected, and not to be retried: using the Bome link as a sign of life for THIS check.
    An established TCP connection proves the phone is reachable, not that it is plugged in
    — a phone on Wi-Fi and off its charger keeps that link up while it drains. It would
    have manufactured exactly the unobserved green light everything here avoids. The only
    thing that can contradict the flag is the battery going down; see the trend below.
    """
    sev = cfg["modes"][mode].get("iphone_power_severity", "warn")
    if sev == OFF:
        return None
    if phone and phone.get("fresh") and phone.get("charging") is not None:
        batt = f", {phone['battery']} %" if phone.get("battery") is not None else ""
        tr = phone.get("trend") or {}
        # La MESURE passe avant le DRAPEAU. « En charge » date du branchement et n'est
        # plus revérifié ; une batterie qui recule, elle, est en train de se produire.
        # Quand les deux se contredisent, c'est le drapeau qui a tort — câble sorti,
        # multiprise éteinte, chargeur mort. Aucun de ces trois ne se déclare.
        if tr.get("falling"):
            drop = (f"{tr['from']} → {tr['to']} % en {_ago(tr['span'])}")
            left = tr.get("hours_left")
            hmin = float(cfg.get("server", {}).get("autonomy_min_hours", 3.0))
            if left is not None and left < hmin:
                # Le vrai risque du soir : pas « est-il branché ? » mais « tiendra-t-il
                # jusqu'à la fin ? ». À ce rythme, non — et c'est une erreur, pas une
                # remarque, parce qu'il n'y a plus de rattrapage une fois sur scène.
                return Result("sys:iphonecharge", "iPhone en charge", FAIL,
                              _hint(f"il ne tiendra pas : {drop}, soit ~{left} h "
                                    f"d'autonomie (moins de {hmin:g} h)",
                                    "le brancher MAINTENANT et vérifier que le niveau "
                                    "remonte avant de partir"))
            if phone["charging"]:
                return Result("sys:iphonecharge", "iPhone en charge", sev,
                              _hint(f"branché d'après {_source_label(phone)}, mais la "
                                    f"batterie DESCEND : {drop}",
                                    "câble sorti, multiprise éteinte ou chargeur mort — "
                                    "aucun des trois ne se déclare tout seul"))
            est = f", soit ~{left} h d'autonomie" if left is not None else ""
            return Result("sys:iphonecharge", "iPhone en charge", sev,
                          _hint(f"pas en charge, il descend : {drop}{est}",
                                "le brancher sur SON chargeur (pas sur le Mac : il "
                                "puiserait dans les 90 W du dock)"))
        if phone["charging"]:
            climb = f" (+{tr['delta']} % en {_ago(tr['span'])})" if tr.get("rising") else ""
            return Result("sys:iphonecharge", "iPhone en charge", OK,
                          f"{_source_label(phone)} le dit{batt}{climb} "
                          f"— il y a {_ago(phone['age'])}")
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint(f"pas en charge d'après {_source_label(phone)}{batt}",
                            "le brancher sur SON chargeur (pas sur le Mac : il puiserait "
                            "dans les 90 W du dock). La ligne verdit seule, sans rien cocher"))
    if acked:
        return Result("sys:iphonecharge", "iPhone en charge", OK, "confirmé manuellement")
    if phone and phone.get("seen"):
        # Il a parlé, puis s'est tu. C'est un TROISIÈME état, et le seul qui désigne le
        # téléphone lui-même : « pas de nouvelles » n'est pas « pas en charge », et surtout
        # pas « on ne sait pas encore ». Un téléphone éteint, sorti du Wi-Fi ou dont
        # l'automatisation ne part plus, c'est aussi le lien Bome qui va tomber — autant
        # le dire ici plutôt que de laisser une ligne verte périmée le cacher.
        last = "en charge" if phone.get("charging") else "PAS en charge"
        batt = f", {phone['battery']} %" if phone.get("battery") is not None else ""
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint(f"aucun relevé depuis {_ago(phone.get('age'))} "
                            f"(dernier signe, par {_source_label(phone)} : {last}{batt})",
                            "téléphone éteint ou hors du Wi-Fi — le lien Bome en dépend "
                            "aussi — ou appairage perdu côté Mac ; sinon, confirmer à la main"))
    # En live c'est BLOQUANT tant que ce n'est pas coché, et la ligne doit le dire :
    # « à confirmer » tout seul se lit comme une formalité, alors que c'est la seule
    # chose qui retient le rig. Au bureau, même phrase mais sans l'avertissement — il n'y
    # bloque rien (voir iphone_power_severity par mode).
    if sev == FAIL:
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint("à confirmer — le Mac ne peut pas le détecter",
                            "brancher le téléphone sur SON chargeur (pas sur le Mac : "
                            "il puiserait dans les 90 W du dock), puis cocher. Le rig "
                            "reste bloqué tant que ce n'est pas fait"))
    return Result("sys:iphonecharge", "iPhone en charge", sev,
                  "à confirmer — non détectable depuis le Mac")


def check_bome_iphone(cfg: dict) -> Result:
    """Detect the Bome Network ↔ iPhone link via an ESTABLISHED TCP connection on
    Bome Network's port (37000). The iPhone runs Bome Network and connects here."""
    port = cfg["checks"].get("bome_network_port", 37000)
    host = str(cfg["checks"].get("iphone_host", "")).strip()
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=6,
        ).stdout
    except Exception as exc:
        return Result("net:iphone", "Bome Network ↔ iPhone", FAIL, f"lsof: {exc}")

    lines = [l for l in out.splitlines() if "ESTABLISHED" in l]
    if host:
        lines = [l for l in lines if host in l]
    if not lines:
        # Le lien a DEUX bouts, et le conseil ne vaut que s'il désigne le bon. Bome
        # Network éteint sur le Mac est visible d'ici ; s'il tourne, alors le côté
        # muet est forcément le téléphone — c'est la seule chose qu'on ne voit pas.
        # remedy.py suit exactement la même règle : pas de bouton « relancer » quand
        # le Mac est déjà en ordre, sinon on relance ce qui marche.
        mac_side = cfg["checks"]["apps"].get("Bome Network", "Bome Network")
        if not _pgrep(mac_side):
            advice = "lancer Bome Network sur le MAC (il est éteint ici)"
        else:
            advice = ("Bome Network tourne sur le Mac → c'est côté IPHONE qu'il n'est "
                      "pas lancé. L'ouvrir sur le téléphone, et vérifier qu'il est sur "
                      "le même réseau que le Mac")
        return Result("net:iphone", "Bome Network ↔ iPhone", FAIL,
                      _hint("aucune connexion", advice))
    # NAME column looks like "192.168.1.10:37000->192.168.1.20:52345 (ESTABLISHED)"
    peer = ""
    for tok in lines[0].split():
        if "->" in tok:
            peer = tok.split("->", 1)[1]
            break
    return Result("net:iphone", "Bome Network ↔ iPhone", OK,
                  f"connecté{f' ({peer})' if peer else ''}")


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
        from . import liveaudio
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


# Le parsing vit dans riglib/vpn.py, avec la coupure : un seul lecteur de `scutil --nc
# list` pour les deux, sinon le check et le fix finissent par ne plus parler du même VPN.
# (Les entrées [PPP:Modem] y sont écartées : ce sont des gadgets série — pédale ToneX,
# cartes Seeed — que macOS range dans la même liste, pas des VPN.)
def check_vpn(cfg: dict) -> Result:
    try:
        active = vpn.connected(cfg)
    except Exception as exc:
        return Result("sys:vpn", "VPN inactif", WARN, f"scutil: {exc}")
    if not active:
        return Result("sys:vpn", "VPN inactif", OK, "")
    names = ", ".join(n for n, _ in active if n) or "?"
    return Result("sys:vpn", "VPN inactif", FAIL, f"VPN actif : {names}")


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
            for a in apps.unexpected(cfg)]


def _guard(label: str, fn) -> list:
    """Exécute un check ; s'il lève, rend une ligne EN ERREUR au lieu de tout emporter.

    Sans ce filet, une seule exception remontait jusqu'à `do_GET`, la connexion se
    fermait vide, et le client ne voyait pas « un check en panne » mais « serveur
    injoignable ». C'est ce qui a rendu la panne du 2026-08-18 si opaque : l'agent
    tournait, le port répondait, et rien ne sortait — pendant 18 heures.

    Un rig à 24 checks sur 25 reste utilisable ; un dashboard muet, non. Et le check qui
    a lâché le DIT, avec son exception : c'est une information, pas un silence.
    """
    try:
        out = fn()
    except Exception as exc:
        return [Result(f"err:{label}", label, WARN,
                       _hint(f"ce check a échoué : {type(exc).__name__} — {exc}",
                             "les autres checks restent valables ; celui-ci est à corriger "
                             "dans le code, pas sur le rig"))]
    if out is None:
        return []
    return out if isinstance(out, list) else [out]


def run_all(cfg: dict, mode: str = "live", with_audio: bool = True,
            manual: dict | None = None, phone: dict | None = None) -> list[Result]:
    manual = manual or {}
    todo = [
        ("Applications", lambda: check_apps(cfg)),
        ("Stream Decks", lambda: check_streamdeck(cfg)),
        ("Ports MIDI", lambda: check_midi(cfg)),
        ("Clavier", lambda: check_keyboard(cfg, mode)),
        ("Breath controller", lambda: check_breath(cfg, mode)),
        ("Réseau de scène", lambda: check_stage_network(cfg, mode)),
        ("Lampes", lambda: check_lamps(cfg, mode)),
        ("Lien iPhone", lambda: check_bome_iphone(cfg)),
        ("VPN", lambda: check_vpn(cfg)),
        ("Autorisation Accessibilité", lambda: check_accessibility(cfg)),
        ("Applis en trop", lambda: check_unexpected_apps(cfg, mode)),
        ("Alimentation Mac", lambda: check_mac_power(cfg, mode)),
        ("Charge iPhone", lambda: check_iphone_charge(
            cfg, mode, acked=bool(manual.get("iphone_charge")), phone=phone)),
        ("Amphetamine", lambda: check_amphetamine(cfg, mode)),
    ]
    if with_audio:
        todo += [
            # En tête du bloc audio : quand il est rouge, il EST la cause des suivants.
            ("Service audio", lambda: check_coreaudio(cfg)),
            ("Sortie par défaut", lambda: check_default_output(cfg)),
            ("Interface audio", lambda: check_audio(cfg, mode)),
            ("Périphériques audio", lambda: check_audio_devices(cfg, mode)),
            ("Sortie d'Ableton", lambda: check_live_output(cfg, mode)),
            ("Fenêtre d'Ableton", lambda: check_live_dialog(cfg)),
        ]
    results: list[Result] = []
    for label, fn in todo:
        results += _guard(label, fn)
    # Les checks réglés sur "off" rendent None : ils disparaissent ici, une bonne fois,
    # plutôt que chaque appelant ait à s'en soucier.
    return [r for r in results if r is not None]


def worst(results: list[Result]) -> str:
    """Pire niveau du lot. INFO n'apparaît JAMAIS ici : c'est une annotation par ligne,
    pas un état du rig — un rig dont tout l'optionnel est débranché reste « prêt »."""
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status == WARN for r in results):
        return WARN
    return OK
