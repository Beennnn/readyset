"""The rig's state, recomputed in the background and served as-is.

Split out of http.py because it answers a different question: http.py knows about
sockets, routes and headers; this file knows what the rig's state IS. The web server is
only one of its readers — the menu-bar app, the floating badge and the Stream Deck key
all read the same snapshot through it.

WHY A BACKGROUND THREAD. Each GET /api/state used to re-run every check: three to seven
seconds of ioreg, pings and log reads — and the page and the menu bar each did it on
their own side. Polling every second, as asked, would have meant two full passes per
second: the stage Mac's CPU goes up in smoke to refresh a table.

Decoupled, the cost no longer depends on how many readers there are or how fast they
poll: one pass every REFRESH_EVERY seconds whatever happens, and an instant read for
everyone.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ... import apps, checks, idevice, midimon
from ...core import cascade, config
from ...devices import icons_for
from ...fix import remedy
from ..alerts import Alerter


_MON = midimon.MidiMonitor()   # shared live MIDI monitor for the soundcheck page

# L'état du rig, calculé EN CONTINU par un thread de fond et servi tel quel.
#
# Avant, chaque GET /api/state relançait la totalité des checks : entre 3 et 7 secondes
# de ioreg, de pings et de lecture de journaux — et la page comme la barre de menus le
# faisaient chacune de leur côté. Interroger toutes les secondes, comme demandé, aurait
# donc voulu dire deux passes complètes par seconde : le CPU du Mac de scène part en
# fumée pour rafraîchir un tableau.
#
# Découplé, le coût ne dépend plus du nombre de lecteurs ni de leur cadence : une passe
# toutes les REFRESH_EVERY secondes, quoi qu'il arrive, et une lecture instantanée pour
# tout le monde. Les clients peuvent alors interroger à la seconde sans rien coûter.
_STATE: dict = {"data": None, "ts": 0.0}
REFRESH_EVERY = 2.0
_MODE = {"requested": None}    # None → use cfg default; else "auto"|"live"|"studio"
_MANUAL = {"iphone_charge": False}  # manual confirmations (things the Mac can't detect)

# Le dernier rapport du téléphone (POST /api/phone). Le Mac ne PEUT pas voir si l'iPhone
# est en charge : il se charge sur son propre chargeur et ne parle au rig que par Wi-Fi.
# Jusqu'ici on cochait donc une case à la main — une déclaration, pas une mesure, qui
# reste cochée après avoir débranché le téléphone. Le téléphone, lui, connaît la réponse :
# il la publie, et le check devient une vraie observation, horodatée.
_PHONE: dict = {"ts": 0.0, "charging": None, "battery": None, "name": "", "source": "",
                # Les derniers relevés [ts, batterie, en charge], pour la PENTE. Un seul
                # point ne dit rien : c'est la comparaison de deux relevés qui démonte un
                # « en charge » devenu faux. Court exprès — au-delà de quelques heures la
                # pente d'hier ne dit plus rien de la prise de ce soir.
                "history": []}
_HISTORY_CAP = 24
# Le rapport est relu au démarrage, et réécrit à chaque publication. Le téléphone parle
# sur ÉVÉNEMENT — branché, débranché — pas en battement régulier : sans ce fichier, un
# redémarrage du dashboard (ou du Mac, une heure avant de jouer) effacerait un fait qui
# est toujours vrai, et il faudrait débrancher puis rebrancher le téléphone rien que pour
# le lui réapprendre. `logs/` est le seul dossier local déjà exclu de git.
_PHONE_FILE = config.REPO_DIR / "logs" / "phone-report.json"


def phone_load() -> None:
    try:
        d = json.loads(_PHONE_FILE.read_text())
    except Exception:
        return                      # jamais publié, fichier absent ou illisible : tant pis
    for k in ("ts", "charging", "battery", "name", "source", "history"):
        if k in d:
            _PHONE[k] = d[k]


def _phone_save() -> None:
    try:
        _PHONE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PHONE_FILE.write_text(json.dumps(_PHONE))
    except Exception:
        pass                        # un rapport non persisté vaut mieux qu'un 500

# Le pictogramme de TYPE d'un check. Il ne remplace pas les vignettes de `gear.icons_for`
# (l'icône macOS réelle, le dessin de l'appareil) : celles-là se servent en HTTP et n'ont
# pas de sens là où l'on ne peut pas charger d'image — le panneau d'alarme est dessiné en
# AppKit et n'affichait, faute de mieux, qu'une puce « • » sur chacune de ses lignes.
# D'abord la clé exacte, ensuite le préfixe : l'alimentation du Mac mérite sa prise, et
# elle vit dans le même « sys: » que le VPN.
_GLYPH_KEY = {"sys:macpower": "🔌", "sys:vpn": "🛡", "sys:output": "🔊",
              "sys:accessibility": "🔓", "sys:iphonecharge": "🔋", "audio:live": "🎚"}
_GLYPH = {"app:": "🖥", "xapp:": "🖥", "usb:": "🎛", "kbd:": "🎹", "net:": "🌐",
          "lamp:": "💡", "sys:": "⚙️", "midi": "🎚", "audio": "🔊", "sc:": "🎤",
          "link:": "🔗"}


def _glyph_of(key: str) -> str:
    if key in _GLYPH_KEY:
        return _GLYPH_KEY[key]
    for prefix, g in _GLYPH.items():
        if key.startswith(prefix):
            return g
    return "•"


_GROUP = {"app:": "Apps", "xapp:": "Apps en trop", "usb:": "Stream Deck",
          "kbd:": "Clavier & jeu",
          "net:": "Réseau", "lamp:": "Lampes", "sys:": "Système",
          "midi?:": "MIDI optionnel", "midi:": "MIDI requis", "audio": "Audio"}


LOG_CAP = 5 * 1024 * 1024      # au-delà, on coupe
LOG_KEEP = 256 * 1024          # ce qu'on garde : la fin, la seule partie utile


def cfg_tidy_after(cfg: dict) -> bool:
    """Le rangement de fin de mise en place est-il activé ? (`[windows] after_preflight`)"""
    return bool(cfg.get("windows", {}).get("after_preflight", True))


def _group_of(key: str) -> str:
    for prefix, name in _GROUP.items():
        if key.startswith(prefix):
            return name
    return "Autre"


def _soundcheck_result(cfg: dict, mode: str) -> checks.Result | None:
    """Le soundcheck, remonté comme un CHECK à part entière dans /api/state.

    Il ne vivait que dans /api/midi, que seul le dashboard interroge. Conséquence
    constatée le 2026-08-18 : la page annonçait « PAS prêt — soundcheck non vérifié »
    pendant que /api/state répondait « ok, 0 bloquant » — donc la barre de menus, la
    pastille flottante et le liseré d'écran, qui lisent /api/state, s'éteignaient
    tranquillement. Exactement la contradiction d'un niveau à l'autre que le reste du
    tableau s'efforce d'éviter.

    Un seul endroit doit dire si le rig est prêt. Le voici, et tout le monde le lit.
    """
    sev = cfg["modes"].get(mode, {}).get("soundcheck_severity", "fail")
    if sev == checks.OFF:
        return None
    snap = _MON.snapshot()
    # Une icône par geste : « 2 gestes pas encore reçus » ne dit pas LESQUELS, et même
    # nommés, une liste de mots se lit mot à mot. Un pied, un clavier, un souffle se
    # reconnaissent d'un coup d'œil — c'est ce qu'on demande à une ligne qu'on regarde
    # entre deux morceaux. Les deux gestes universels sont ici ; ceux de la chaîne du
    # breath portent la leur dans rig.toml, à côté de leur nom.
    items = [{"name": "Pédale", "ok": bool(snap["flags"].get("pedal_cc64")), "icon": "🦶"},
             {"name": "Notes", "ok": snap["flags"].get("notes", 0) > 0, "icon": "🎹"}]
    items += [{"name": g["name"], "ok": bool(g.get("raw") and g.get("out")),
               "icon": g.get("icon") or ""} for g in snap.get("chain", [])]
    missing = [p["name"] for p in items if not p["ok"]]
    if not missing:
        return checks.Result("sc:play", "Soundcheck joué", checks.OK,
                             f"{len(items)} gestes vérifiés", parts=items)
    # `parts` porte les gestes un par un : le texte ci-dessous les énumère pour qui n'a
    # qu'une ligne (le dashboard, une notification), la liste sert à qui peut les déplier.
    return checks.Result("sc:play", "Soundcheck joué", sev,
                         checks._hint(f"{len(missing)} geste(s) pas encore reçu(s) : "
                                      + ", ".join(missing),
                                      "les jouer une fois — la vérification est passive, "
                                      "rien à cocher"),
                         parts=items)


def phone_record(charging=None, battery=None, name=None, source: str = "phone") -> None:
    """Enregistre un relevé, d'où qu'il vienne — le téléphone qui publie, ou le Mac qui
    interroge. Un seul magasin pour les deux : c'est ce qui permet à la pente de se
    calculer sur des points de provenances mélangées sans que rien n'ait à le savoir.
    Ce qui est nul est ignoré, jamais écrasé — un relevé partiel ne doit pas effacer ce
    qu'on savait déjà.
    """
    if charging is not None:
        _PHONE["charging"] = bool(charging)
    if battery is not None:
        try:
            _PHONE["battery"] = max(0, min(100, int(float(battery))))
        except (TypeError, ValueError):
            pass
    if name:
        _PHONE["name"] = str(name)[:40]
    _PHONE["source"] = source
    # L'horodatage est posé ICI, jamais pris dans le message : l'heure d'un téléphone n'a
    # pas à être crue, et c'est la fraîcheur qui compte, pas la date qu'il revendique.
    _PHONE["ts"] = time.time()
    if _PHONE["battery"] is not None:
        _PHONE["history"] = ([*_PHONE.get("history", []),
                              [_PHONE["ts"], _PHONE["battery"], _PHONE["charging"]]]
                             )[-_HISTORY_CAP:]
    _phone_save()


def _phone_trend(cfg: dict) -> dict | None:
    """Ce que fait la batterie entre le relevé le plus ancien de la fenêtre et le dernier.

    C'est le DÉMENTI du drapeau : « en charge » est un fait daté du branchement, que
    plus rien ne revérifie ; un câble qui lâche ou une multiprise éteinte ne le
    changent pas. Une batterie qui recule, si.

    Deux verdicts, et deux exigences différentes :
      • `falling` — un seul pour cent perdu suffit, une batterie qui charge ne recule
        pas. Quelques minutes d'écart, donc, moins que ne dure un soundcheck.
      • `hours_left` — demande beaucoup plus de recul : à 1 % près sur 3 minutes la
        pente vaut ±20 %/h. Sous `trend_autonomy_span_seconds`, on constate la baisse
        sans oser la chiffrer. Une estimation fausse serait pire que pas d'estimation.

    On compare toujours au plus ANCIEN point de la fenêtre, jamais au précédent : le
    bras de levier long est ce qui rend le chiffre honnête.
    """
    srv = cfg.get("server", {})
    now = time.time()
    pts = [p for p in _PHONE.get("history", [])
           if len(p) >= 2 and p[1] is not None
           and now - p[0] <= float(srv.get("trend_window_seconds", 10800))]
    if len(pts) < 2:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < float(srv.get("trend_min_span_seconds", 180)):
        return None
    delta = pts[-1][1] - pts[0][1]
    per_hour = delta / (span / 3600)
    out = {"per_hour": round(per_hour, 1), "span": round(span), "delta": delta,
           "from": pts[0][1], "to": pts[-1][1], "points": len(pts),
           "falling": delta <= -1, "rising": delta >= 1, "hours_left": None}
    if out["falling"] and span >= float(srv.get("trend_autonomy_span_seconds", 1800)):
        out["hours_left"] = round(pts[-1][1] / abs(per_hour), 1)
    return out


def phone_snapshot(cfg: dict) -> dict:
    """Le dernier rapport du téléphone, avec son âge et sa fraîcheur déjà tranchée.

    `fresh` est calculée ici et pas chez l'appelant : sans elle, chaque surface
    redécide ce qu'est « récent » et deux d'entre elles finissent par se contredire.
    """
    if not _PHONE["ts"]:
        return {"seen": False, "fresh": False, "age": None, "source": "",
                "charging": None, "battery": None, "name": "", "trend": None}
    age = round(time.time() - _PHONE["ts"], 1)
    stale = float(cfg.get("server", {}).get("phone_stale_seconds", 300))
    return {"seen": True, "fresh": age <= stale, "age": age,
            "charging": _PHONE["charging"], "battery": _PHONE["battery"],
            "name": _PHONE["name"], "source": _PHONE.get("source", ""),
            "trend": _phone_trend(cfg)}


def build_state(cfg: dict, with_audio: bool = True) -> dict:
    requested = _MODE["requested"] or cfg.get("mode", {}).get("default", "auto")
    mode = checks.resolve_mode(cfg, requested)
    # checks.run_all caches the slow system_profiler call internally, so polling is cheap.
    results = checks.run_all(cfg, mode, with_audio=with_audio, manual=_MANUAL,
                             phone=phone_snapshot(cfg))
    sc = _soundcheck_result(cfg, mode)
    if sc:
        results.append(sc)
    items = []
    for r in results:
        rem = remedy.resolve(cfg, r)
        items.append({
            **r.to_dict(),
            "group": _group_of(r.key),
            "glyph": _glyph_of(r.key),
            # Les vignettes d'un check : l'icône macOS réelle pour une app, un dessin
            # pour du matériel (voir readyset/devices/). Liste, parce qu'un check peut
            # porter sur DEUX objets — « Bome Network ↔ iPhone » en montre les deux.
            "icons": icons_for(cfg, r.key, r.label),
            "remedy": rem.label if (rem and r.status != checks.OK) else None,
            # Le remède ouvre-t-il seulement la porte ? Les surfaces qui RÉSUMENT ce
            # qu'un bouton va faire ont besoin de la nuance : sans elle, le menu-barre
            # compte « Ouvrir le réglage Accessibilité » parmi ce qu'il sait régler.
            "manual": bool(rem and rem.hands_on and r.status != checks.OK),
        })
    # Erreurs LIÉES : qui explique qui. Posé APRÈS que tous les items existent — un lien
    # se juge sur l'état de la chaîne entière, pas check par check (readyset/core/cascade.py).
    cascade.annotate(cfg, mode, items)
    status = checks.worst(results)
    return {
        "status": status,
        "mode": mode,
        "requested": requested,
        "fails": sum(1 for r in results if r.status == checks.FAIL),
        "warns": sum(1 for r in results if r.status == checks.WARN),
        # Compté à part : de l'optionnel absent, jamais un problème. Le menubar et la
        # pastille l'ignorent, mais le dashboard le montre pour qu'on sache que le check
        # a bien tourné et n'a pas été oublié.
        "infos": sum(1 for r in results if r.status == checks.INFO),
        "total": len(results),
        "items": items,
        # Servi à part des `items` pour que le panneau de fermeture ait le chemin exact
        # de chaque app (les items ne portent qu'un libellé). Presque gratuit : la liste
        # vient du mémo de readyset/apps, déjà rempli par le check.
        "unexpected": apps.unexpected(cfg),
    }


# Un battement toutes les 60 s, en plus de chaque changement. Deux raisons distinctes :
# le MIDI n'accuse pas réception, donc une surface allumée après le tableau de bord —
# l'ordre habituel sur scène — n'aurait jamais rien reçu ; et un flux régulier est ce qui
# permet à un tiers de conclure quelque chose du SILENCE. La touche elle-même ne le peut
# pas : un script trevligaspel réagit à un message qui arrive, rien ne se déclenche quand
# plus rien n'arrive. Le battement rend la surveillance POSSIBLE, il ne la fait pas.
# En SECONDES, mesurées — pas en nombre de tours. Compter les tours partait du principe
# qu'un tour dure REFRESH_EVERY ; il dure REFRESH_EVERY plus le temps des checks, et la
# sonde audio ou un hôte réseau qui ne répond pas l'allongent sans prévenir. Le battement
# dérivait donc silencieusement, ce qui est exactement ce qu'un battement ne doit pas faire.
#
# Quinze secondes, pas soixante. La cadence ne sert PAS à voir les changements — ceux-là
# partent à l'instant où ils arrivent. Elle fixe deux choses : en combien de temps une
# surface allumée en retard rattrape son affichage, et au bout de combien de silence un
# guetteur peut conclure que plus personne n'émet. Une minute rendait ce verdict lent
# alors qu'il ne coûte rien de l'accélérer : onze messages de trois octets sur un port
# qui ne va nulle part.
_GAUGE_BEAT_S = 15.0


def _rotate_log(path: Path) -> None:
    """Coupe le journal quand il dépasse LOG_CAP, en gardant sa FIN.

    Tronquer à zéro serait plus simple, mais on jetterait précisément la trace de ce qui
    vient de mal tourner — c'est presque toujours la fin qu'on lit. On réécrit donc les
    derniers LOG_KEEP octets, en repartant à une frontière de ligne pour ne pas laisser
    une demi-ligne en tête.

    Écriture EN PLACE (`r+`) et non renommage : launchd tient le descripteur ouvert en
    mode ajout, et renommer le fichier sous ses pieds le ferait écrire dans un fichier
    qui n'a plus de nom — le journal deviendrait invisible jusqu'au prochain redémarrage
    du service.
    """
    try:
        if not path.exists() or path.stat().st_size <= LOG_CAP:
            return
        with path.open("r+b") as f:
            f.seek(-LOG_KEEP, 2)
            tail = f.read()
            cut = tail.find(b"\n")
            tail = tail[cut + 1:] if cut != -1 else tail
            f.seek(0)
            f.write(b"--- journal tronque (garde les %d derniers Ko) ---\n" % (len(tail) // 1024))
            f.write(tail)
            f.truncate()
    except Exception as exc:
        print(f"[log] rotation impossible : {exc}", flush=True)


_ticks = [0]


def state_loop(cfg: dict) -> None:
    _rotate_log(config.REPO_DIR / "logs" / "dashboard.log")
    """Recalcule l'état sans fin, à cadence fixe, quoi que fassent les clients."""
    ticks = 0
    # Publier ne relève pas du même geste qu'alerter : on force le seul backend « midi »
    # plutôt que la liste de [monitor].alerts, sans quoi le tableau de bord doublerait
    # les notifications macOS et les push de « readyset monitor ».
    jauge = Alerter(cfg, ["midi"], log=lambda m: print(f"[jauge]{m}", flush=True))
    vues: list[str] | None = None
    dernier = 0.0
    while True:
        try:
            data = build_state(cfg)
            _STATE["data"], _STATE["ts"] = data, time.time()
            tombees = [it["key"] for it in data["items"] if it.get("status") == checks.FAIL]
            if tombees != vues or time.time() - dernier >= _GAUGE_BEAT_S:
                jauge.gauge(tombees)
                vues, dernier = tombees, time.time()
        except Exception as exc:                  # un check qui lève ne doit pas tuer la
            print(f"[state] {type(exc).__name__}: {exc}", flush=True)   # boucle entière
        ticks += 1
        # Le battement que le téléphone ne sait pas produire : c'est le Mac qui demande,
        # à cadence fixe, tant que l'appareil est appairé. Silencieux s'il ne l'est pas —
        # `read` rend None et on garde ce que le téléphone a publié de son côté.
        every = float(cfg.get("server", {}).get("idevice_poll_seconds", 60))
        if every > 0 and ticks % max(1, int(every / REFRESH_EVERY)) == 0:
            got = idevice.read(cfg)
            if got:
                phone_record(charging=got["charging"], battery=got["battery"],
                             source=f"mac:{got['via']}")
        time.sleep(REFRESH_EVERY)
