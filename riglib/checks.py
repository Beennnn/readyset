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
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import mido

from . import apps, sysload, vpn

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


def check_apps(cfg: dict) -> list[Result]:
    out = []
    for label, pattern in cfg["checks"]["apps"].items():
        running = _pgrep(pattern)
        out.append(Result(
            key=f"app:{label}", label=f"{label} lancé",
            status=OK if running else FAIL,
            detail="" if running else "process introuvable",
        ))
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


def check_amphetamine(cfg: dict) -> Result:
    """Amphetamine must be running AND holding an active anti-sleep session. A live
    session shows up as an '(Amphetamine)' power assertion in `pmset -g assertions`."""
    if not _pgrep("Amphetamine.app/Contents/MacOS/Amphetamine"):
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", FAIL, "pas lancé")
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("sys:amphetamine", "Amphetamine (anti-veille)", WARN, f"pmset: {exc}")
    active = "(Amphetamine)" in out
    return Result("sys:amphetamine", "Amphetamine (anti-veille)",
                  OK if active else FAIL,
                  "session active" if active else "lancé, aucune session active")


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
        for line in _tail(logs[0]).splitlines():
            if "Audio In Out: Output Device:" in line:
                dev = line.split("Output Device:", 1)[1].strip()
                # Découpe sur « : » SUIVI D'UN ESPACE : l'horodatage en contient trois
                # sans espace (23:38:16), donc un split sur le premier deux-points rendait
                # « 2026-08-19T23 » — que fromisoformat accepte sans broncher, en lisant
                # 23 h pile. L'écart mesuré devenait faux jusqu'à 59 minutes.
                when_iso = line.split(": ", 1)[0]     # 2026-08-19T23:38:16.911624
    except Exception as exc:
        return Result("audio:live", label, WARN, f"lecture log: {exc}")
    if dev is None:
        return Result("audio:live", label, WARN,
                      _hint("aucune sortie déclarée dans le journal de Live",
                            "régler la sortie une fois : la ligne apparaîtra et cette "
                            "vérification deviendra possible"))

    short = dev.split(" (")[0]
    ok = any(w.lower() in dev.lower() for w in wants)

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
_profile_cache: dict = {"ts": 0.0, "data": None, "ready": False, "running": False}
_PROFILE_TTL = 10.0


def _audio_probe() -> None:
    """Interroge CoreAudio en tâche de fond et range le résultat dans le cache."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        _profile_cache["data"] = json.loads(out)
        _profile_cache["ready"] = True
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
    want = cfg["checks"]["audio_interface"]
    sev = cfg["modes"].get(mode, {}).get("interface_severity", "fail")
    if sev == OFF:
        return None
    names = [it.get("_name", "") for it in _audio_items()]
    if not _audio_ready():
        return Result("audio", f"Interface audio « {want} »", INFO, "lecture CoreAudio en cours…")
    hit = any(want.lower() in n.lower() for n in names)
    return Result(
        key="audio", label=f"Interface audio « {want} »",
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


# ─────────────────────────── Charge système ───────────────────────────
#
# Trois checks, et l'ordre entre eux n'est pas décoratif : il dit la causalité.
#
# La mémoire d'abord, parce que c'est elle qui tombe en premier et qui fait tomber le
# reste. Mesuré le 2026-08-23 sur un set qui « faisait exploser le CPU » : Live à 61 %
# d'un cœur — rien — mais 119 Mo de RAM libre sur 32 Go, 8 Go de swap sur 9,2, et par
# ricochet coreaudiod à 92 % et WindowServer à 93 %. Le CPU n'était pas la panne, il
# en était le bruit. Un rig qui ne surveille que le CPU regarde la fumée, pas le feu.
#
# La charge ensuite, comme confirmation : un load average par cœur qui s'envole SANS
# pression mémoire pointe ailleurs (un rendu, une indexation, une synchro).
#
# Les gloutons en dernier, parce que ce sont eux qu'on peut réellement fermer — et
# c'est le seul des trois qui porte un bouton.


def _load_cfg(cfg: dict) -> dict:
    return cfg.get("checks", {}).get("load", {}) or {}


def check_memory(cfg: dict, mode: str) -> Result | None:
    """Pression mémoire et swap — la vraie cause des « CPU qui explosent »."""
    sev = cfg["modes"][mode].get("memory_severity", FAIL if mode == "live" else WARN)
    if sev == OFF:
        return None

    lc = _load_cfg(cfg)
    swap_warn = float(lc.get("swap_warn_pct", 60))
    swap_fail = float(lc.get("swap_fail_pct", 90))

    snap = sysload.snapshot(cfg)
    swap_pct = snap.swap_pct
    used_gb, total_gb = snap.swap_used_mb / 1024, snap.swap_total_mb / 1024
    detail = (f"pression {snap.pressure_label} · swap {used_gb:.1f}/{total_gb:.1f} Go "
              f"({swap_pct:.0f} %)")

    critical = snap.pressure == sysload._PRESSURE_CRITICAL or swap_pct >= swap_fail
    elevated = snap.pressure == sysload._PRESSURE_WARN or swap_pct >= swap_warn

    if critical:
        return Result("sys:mem", "Mémoire", sev,
                      _hint(detail, "le thread audio pagine — ferme les gloutons ci-dessous"))
    if elevated:
        # Jamais au-dessus de WARN quand ce n'est qu'« élevé » : la pression monte
        # naturellement sur une machine qui travaille, et un rouge à chaque set
        # apprendrait surtout à ne plus lire les rouges.
        return Result("sys:mem", "Mémoire", WARN,
                      _hint(detail, "ça tient, mais la marge est fine"))
    return Result("sys:mem", "Mémoire", OK, detail)


def check_load(cfg: dict, mode: str) -> Result | None:
    """Load average rapporté au nombre de cœurs.

    Rapporté au nombre de cœurs, sinon le chiffre ne veut rien dire : 8 est confortable
    sur 10 cœurs et catastrophique sur 2. C'est un check de CONFIRMATION — il ne dit
    jamais quoi faire, il dit si la machine peine, et croisé avec la mémoire il dit
    laquelle des deux est en cause.
    """
    sev = cfg["modes"][mode].get("load_severity", WARN)
    if sev == OFF:
        return None

    lc = _load_cfg(cfg)
    warn_at = float(lc.get("load_warn_per_cpu", 2.0))
    fail_at = float(lc.get("load_fail_per_cpu", 4.0))

    snap = sysload.snapshot(cfg)
    per_cpu = snap.load_per_cpu
    if per_cpu is None:
        return Result("sys:load", "Charge", INFO, "load average illisible")

    detail = f"{snap.load1:.0f} sur {snap.cpus} cœurs ({per_cpu:.1f}/cœur)"
    if per_cpu >= fail_at:
        return Result("sys:load", "Charge", sev,
                      _hint(detail, "la machine est saturée — regarde la mémoire d'abord"))
    if per_cpu >= warn_at:
        return Result("sys:load", "Charge", WARN, detail)
    return Result("sys:load", "Charge", OK, detail)


def check_ram_hogs(cfg: dict, mode: str) -> list[Result]:
    """Apps non nécessaires au rig qui tiennent beaucoup de mémoire.

    Distinct de `check_unexpected_apps`, qui compte les apps OUVERTES : ici on pèse.
    Une app peut être tolérée par la liste d'exceptions et rester le problème parce
    qu'elle tient neuf giga — c'était exactement le cas de Chrome le 2026-08-23.

    Une ligne par app, chacune avec son bouton, comme pour les apps en trop : « 3 apps
    lourdes » ne se clique pas, « Quitter Chrome (9,3 Go) » si.
    """
    default = WARN if mode == "live" else INFO
    sev = cfg["modes"][mode].get("ram_hogs_severity", default)
    if sev == OFF:
        return []

    lc = _load_cfg(cfg)
    threshold = float(lc.get("hog_mb", 1500))
    out = []
    for a in sysload.hogs(cfg, min_mb=threshold):
        gb = a["mb"] / 1024
        out.append(Result(key=f"ramhog:{a['name']}", label=a["name"], status=sev,
                          detail=f"{gb:.1f} Go de mémoire, et le rig n'en a pas besoin"))
    return out


def run_all(cfg: dict, mode: str = "live", with_audio: bool = True,
            manual: dict | None = None, phone: dict | None = None) -> list[Result]:
    manual = manual or {}
    m = cfg["modes"][mode]
    results = check_apps(cfg) + check_streamdeck(cfg) + check_midi(cfg)
    results += [check_keyboard(cfg, mode), check_breath(cfg, mode)]
    results += [check_stage_network(cfg, mode)]
    results += check_lamps(cfg, mode)
    results += [check_bome_iphone(cfg), check_vpn(cfg)]
    results += check_unexpected_apps(cfg, mode)
    results += [check_memory(cfg, mode), check_load(cfg, mode)]
    results += check_ram_hogs(cfg, mode)
    results += [check_mac_power(cfg, mode),
                check_iphone_charge(cfg, mode, acked=bool(manual.get("iphone_charge")),
                                    phone=phone)]
    if m.get("require_amphetamine", True):
        results.append(check_amphetamine(cfg))
    if with_audio:
        results += [check_default_output(cfg), check_audio(cfg, mode)]
        results += check_audio_devices(cfg, mode)
        results.append(check_live_output(cfg, mode))
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
