"""Local web dashboard — a global rig-state view with per-item fix/relaunch.

Pure stdlib (http.server). The page auto-refreshes the state (read-only checks);
fixes and the full preflight are explicit POSTs triggered by buttons, and honour
the dry-run toggle end to end. Binds to 127.0.0.1 by default; [server].host can widen
that to the local network so the phone can publish its own state (POST /api/phone) —
there is no authentication, so only on a network you trust.
"""

from __future__ import annotations

import json
import socket
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import parse_qs, urlparse

from . import apps, audiolevel, checks, gear, launch, midimon, remedy, windows

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
_PHONE: dict = {"ts": 0.0, "charging": None, "battery": None, "name": ""}

_GROUP = {"app:": "Apps", "xapp:": "Apps en trop", "usb:": "Stream Deck",
          "kbd:": "Clavier & jeu",
          "net:": "Réseau", "lamp:": "Lampes", "sys:": "Système",
          "midi?:": "MIDI optionnel", "midi:": "MIDI requis", "audio": "Audio"}


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


def phone_snapshot(cfg: dict) -> dict:
    """Le dernier rapport du téléphone, avec son âge et sa fraîcheur déjà tranchée.

    `fresh` est calculée ici et pas chez l'appelant : sans elle, chaque surface
    redécide ce qu'est « récent » et deux d'entre elles finissent par se contredire.
    """
    if not _PHONE["ts"]:
        return {"seen": False, "fresh": False, "age": None,
                "charging": None, "battery": None, "name": ""}
    age = round(time.time() - _PHONE["ts"], 1)
    stale = float(cfg.get("server", {}).get("phone_stale_seconds", 300))
    return {"seen": True, "fresh": age <= stale, "age": age,
            "charging": _PHONE["charging"], "battery": _PHONE["battery"],
            "name": _PHONE["name"]}


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
            # Les vignettes d'un check : l'icône macOS réelle pour une app, un dessin
            # pour du matériel (voir riglib/gear.py). Liste, parce qu'un check peut
            # porter sur DEUX objets — « Bome Network ↔ iPhone » en montre les deux.
            "icons": gear.icons_for(cfg, r.key, r.label),
            "remedy": rem.label if (rem and r.status != checks.OK) else None,
        })
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
        # vient du mémo de riglib/apps, déjà rempli par le check.
        "unexpected": apps.unexpected(cfg),
    }


class _Handler(BaseHTTPRequestHandler):
    cfg: dict = {}

    def log_message(self, *_):  # silence default request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            snap = _STATE["data"]
            if snap is None:                       # tout premier appel : on calcule
                snap = build_state(self.cfg)
                _STATE["data"], _STATE["ts"] = snap, time.time()
            self._json({**snap, "age": round(time.time() - _STATE["ts"], 1)})
        elif self.path.startswith("/api/mode"):
            self._json({"requested": _MODE["requested"] or self.cfg.get("mode", {}).get("default", "auto"),
                        "resolved": checks.resolve_mode(self.cfg, _MODE["requested"] or self.cfg.get("mode", {}).get("default", "auto"))})
        elif self.path.startswith("/api/gear"):
            # Dessins servis en ligne : aucun fichier, aucun réseau — le dashboard doit
            # rester entier sur un réseau de scène sans Internet.
            want = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            svg = gear.SVG.get(want)
            if not svg:
                self._send(404, b"no gear", "text/plain")
                return
            body = svg.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/appicon"):
            # L'icône réelle de l'app plutôt qu'un emoji générique : dans une liste où l'on
            # s'apprête à FERMER des choses, on reconnaît WhatsApp à son logo bien avant
            # d'avoir lu son nom. Le chemin est revalidé contre les apps réellement
            # lancées — le serveur n'ouvre pas un fichier arbitraire parce qu'on le demande.
            want = (parse_qs(urlparse(self.path).query).get("path") or [""])[0].rstrip("/")
            # Les apps LANCÉES ne suffisent pas : Bome Network tourne en arrière-plan et
            # n'apparaît jamais dans la liste des apps à présence écran. On y ajoute donc
            # les apps que le rig lance lui-même, et celles que gear.py a résolues pour
            # les checks. L'allowlist reste fermée — un chemin arbitraire est refusé.
            known = {a["path"] for a in apps.running_gui_apps()}
            known |= set(self.cfg.get("launch", {}).get("apps", []))
            known |= {gear.app_path_for(self.cfg, lbl)
                      for lbl in ("Ableton", "Elgato Stream Deck", "Bome Network",
                                  "Bome MIDI Translator", "Stage Traxx")}
            known.discard(None)
            png = apps.icon_png(want) if want in known else None
            if not png:
                self._send(404, b"no icon", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            # L'icône d'une app ne change pas ; sans ça le navigateur la retélécharge à
            # chaque rafraîchissement (toutes les 4 s).
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(png)
        elif self.path.startswith("/api/phone"):
            self._json(phone_snapshot(self.cfg))
        elif self.path.startswith("/api/midi"):
            self._json(_MON.snapshot())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        body = self._read_body()
        dry = bool(body.get("dry", False))
        if self.path == "/api/mode":
            m = body.get("mode", "auto")
            _MODE["requested"] = m if m in ("auto", "live", "studio") else None
            self._json({"ok": True, "requested": _MODE["requested"]})
        elif self.path == "/api/manual":
            key = body.get("key", "")
            if key in _MANUAL:
                _MANUAL[key] = bool(body.get("value", False))
            self._json({"ok": True, key: _MANUAL.get(key)})
        elif self.path == "/api/phone":
            # Publié par le téléphone lui-même (un raccourci iOS, une automatisation),
            # pas par le dashboard. Tout est optionnel : ce qui manque reste inconnu
            # plutôt que d'être supposé faux. L'horodatage, lui, est posé ICI — l'heure
            # du téléphone n'a pas à être crue, et c'est la fraîcheur qui compte.
            if "charging" in body:
                _PHONE["charging"] = bool(body.get("charging"))
            if "battery" in body:
                try:
                    _PHONE["battery"] = max(0, min(100, int(float(body["battery"]))))
                except (TypeError, ValueError):
                    pass
            if body.get("name"):
                _PHONE["name"] = str(body["name"])[:40]
            _PHONE["ts"] = time.time()
            self._json({"ok": True, **phone_snapshot(self.cfg)})
        elif self.path == "/api/audiolevel":
            # Mesure de signal à la demande — jamais dans la boucle de rafraîchissement :
            # chaque appel crée un tap CoreAudio natif d'environ 1,5 s. Voir le README de
            # audiolevel/ pour les deux pièges macOS que ce chemin contourne.
            self._json(audiolevel.measure(float(body.get("seconds", 1.5)),
                                          str(body.get("target", "ableton"))))
        elif self.path == "/api/midi/start":
            _MON.configure(self.cfg)   # la chaîne du breath vient de rig.toml
            _MON.start()
            self._json({"ok": True})
        elif self.path == "/api/midi/stop":
            _MON.stop()
            self._json({"ok": True})
        elif self.path == "/api/fix":
            rem = remedy.resolve_key(self.cfg, body.get("key", ""))
            if rem is None:
                self._json({"ok": False, "message": "Aucune action automatique pour cet élément."})
                return
            ok, msg = rem.run(dry)
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/quit-apps":
            # La confirmation est côté page (case cochée + dialogue) ; ici on revalide
            # quand même la sélection contre la liste des apps « en trop » calculée par
            # le serveur — voir apps.quit_many.
            ok, msg = apps.quit_many(self.cfg, body.get("paths") or [], dry_run=dry)
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/windows":
            # `all` = ranger Ableton aussi (sa politique est "keep" le reste du temps).
            ok, msg = windows.tidy(self.cfg, log=lambda _: None, dry_run=dry,
                                   force=bool(body.get("all", False)))
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/preflight":
            # UNE action, qui fait tout. « Préflight », « Tout corriger » et « Ranger les
            # fenêtres » étaient trois boutons dont personne ne savait dire lequel faisait
            # quoi — Benoît, 2026-08-18 : « on ne comprend pas ce qu'ils font, au final on
            # voudrait une seule action magique qui fait tout ». La voici, dans l'ordre où
            # les choses doivent arriver : d'abord monter le rig, ensuite réparer ce qui
            # cloche encore, enfin re-vérifier pour rendre un verdict à jour.
            logs: list[str] = []
            launch.bring_up(self.cfg, log=logs.append, dry_run=dry)

            # Les correctifs viennent APRÈS le lancement : la plupart des rouges d'avant
            # (apps éteintes, set non ouvert) disparaissent d'eux-mêmes, et réparer ce qui
            # n'existe plus n'aurait aucun sens.
            mode = checks.resolve_mode(self.cfg, _MODE["requested"]
                                       or self.cfg.get("mode", {}).get("default", "auto"))
            todo = [r for r in checks.run_all(self.cfg, mode, manual=_MANUAL)
                    if r.status in (checks.FAIL, checks.WARN)]
            fixed = 0
            for r in todo:
                rem = remedy.resolve(self.cfg, r)
                if not rem:
                    continue
                ok, msg = rem.run(dry)
                fixed += 1
                logs.append(f"  {'✔' if ok else '✖'} {rem.label} — {msg}")
            logs.append(f"  🔧 {fixed} correctif(s) appliqué(s)" if fixed
                        else "  🔧 aucun correctif automatique à appliquer")

            # La fermeture des applis en trop n'est PAS ici : elle peut faire perdre un
            # document non enregistré, donc elle garde sa confirmation nommant chaque app.
            # Une action « magique » ne doit rien détruire sans qu'on l'ait vu venir.
            self._json({"ok": True, "message": "\n".join(logs)})
        else:
            self._json({"ok": False, "message": "route inconnue"}, code=404)


def _state_loop(cfg: dict) -> None:
    """Recalcule l'état sans fin, à cadence fixe, quoi que fassent les clients."""
    while True:
        try:
            data = build_state(cfg)
            _STATE["data"], _STATE["ts"] = data, time.time()
        except Exception as exc:                  # un check qui lève ne doit pas tuer la
            print(f"[state] {type(exc).__name__}: {exc}", flush=True)   # boucle entière
        time.sleep(REFRESH_EVERY)


def _lan_ip() -> str | None:
    """L'adresse IPv4 de cette machine sur son réseau, sans rien émettre.

    Un socket UDP « connecté » ne fait que choisir une route : aucun paquet ne part.
    L'adresse visée est dans TEST-NET-1 (192.0.2.0/24, RFC 5737), qui n'est routée nulle
    part — impossible de joindre quoi que ce soit par accident.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))
            return s.getsockname()[0]
    except Exception:
        return None


def serve(cfg: dict, port: int = 8765, open_browser: bool = True,
          host: str | None = None) -> None:
    host = host or str(cfg.get("server", {}).get("host", "127.0.0.1"))
    threading.Thread(target=_state_loop, args=(cfg,), daemon=True).start()
    handler = type("Handler", (_Handler,), {"cfg": cfg})
    httpd = ThreadingHTTPServer((host, port), handler)
    # Le navigateur local passe toujours par la boucle locale, même quand on écoute plus
    # large : c'est l'adresse qui marche à coup sûr, y compris hors réseau.
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard rig → {url}  (Ctrl-C pour arrêter)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Ouvert au réseau : dire OÙ, sinon il faut aller chercher son IP à la main pour
        # configurer le téléphone. Et dire ce que ça implique — il n'y a pas de mot de
        # passe, quiconque est sur ce réseau peut déclencher les actions du dashboard.
        # Le nom court seul ne résout QUE sur cette machine : c'est en .local, via
        # Bonjour, que le téléphone trouvera le Mac — et ce nom-là survit à un
        # changement d'adresse, contrairement à l'IP juste en dessous.
        name = socket.gethostname()
        if "." not in name:
            name += ".local"
        for label, u in ((".local", f"http://{name}:{port}/"),
                         ("IP    ", f"http://{_lan_ip()}:{port}/" if _lan_ip() else None)):
            if u:
                print(f"  réseau local ({label}) → {u}")
        print("  ⚠ ouvert au réseau local, sans authentification — réseau de confiance "
              "uniquement (le dashboard lance et ferme des apps).")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard arrêté.")
    finally:
        httpd.server_close()


# --- the page (self-contained: inline CSS + JS, no external requests) ----------
PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🎹 Rig — état</title>
<style>
  :root{color-scheme:dark;--bg:#0d0f14;--card:#161a22;--line:#242a36;--tx:#e7ebf2;--mut:#8a93a3;
        --ok:#2ecc71;--info:#5aa9e6;--warn:#f4b942;--fail:#ff5468;--accent:#4a9eff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{position:sticky;top:0;background:linear-gradient(#0d0f14,#0d0f14ee);padding:16px 20px;border-bottom:1px solid var(--line);z-index:5}
  h1{margin:0 0 4px;font-size:19px}
  .sub{color:var(--mut);font-size:13px}
  .banner{margin:12px 0 0;padding:12px 16px;border-radius:10px;font-weight:600;display:flex;gap:10px;align-items:center}
  .banner.ok{background:#123322;color:var(--ok)} .banner.warn{background:#332a12;color:var(--warn)} .banner.fail{background:#3a1620;color:var(--fail)}
  .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{border:none;border-radius:0;background:var(--card);padding:8px 14px}
  .seg button.on{background:var(--accent);color:#03122b;font-weight:600}
  /* Auto ne dit pas seulement « je décide tout seul », il dit CE QU'IL A DÉCIDÉ, par sa
     couleur : bleu = studio, orange = live — les mêmes teintes que les deux boutons
     voisins. Répéter « mode : Studio (auto) » en toutes lettres à côté d'un sélecteur
     qui montrait déjà le même état était une ligne pour rien. */
  .seg button.on.res-studio{background:var(--accent);color:#03122b}
  .seg button.on.res-live{background:var(--warn);color:#2b1e03}
  .seg button[data-m="live"].on{background:var(--warn);color:#2b1e03}
  button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--tx);padding:8px 14px;border-radius:9px;cursor:pointer}
  button:hover{border-color:var(--accent)} button:active{transform:translateY(1px)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#03122b;font-weight:600}
  button.fix{background:transparent;border-color:var(--fail);color:var(--fail);padding:6px 12px;font-size:13px}
  button.fix:hover{background:var(--fail);color:#2a0009}
  label.dry{display:flex;gap:6px;align-items:center;color:var(--mut);font-size:13px;user-select:none}
  /* Pleine largeur : les 820 px dataient de la version en UNE colonne, où une ligne de
     texte trop longue devient illisible. Avec quatre zones côte à côte, la contrainte
     ne protégeait plus rien — elle laissait juste deux bandes noires sur les côtés et
     forçait à faire défiler ce qui aurait tenu à l'écran. Le plafond de 2200 px n'est
     là que pour un écran très large, où des zones de 900 px de large redeviendraient
     pénibles à lire. */
  main{padding:8px 22px 40px;max-width:2200px;margin:0 auto}
  .grp{margin-top:22px} .grp h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}
  .row{display:flex;align-items:center;gap:11px;background:var(--card);border:1px solid var(--line);border-left-width:4px;border-radius:9px;padding:8px 13px;margin-bottom:6px}
  .row.ok{border-left-color:var(--ok)} .row.info{border-left-color:var(--info)} .row.warn{border-left-color:var(--warn)} .row.fail{border-left-color:var(--fail)}
  .allok{padding:14px;background:#123322;color:var(--ok);border-radius:10px;font-weight:600;text-align:center}
  #okwrap{margin-top:14px} #okwrap summary{cursor:pointer;color:var(--mut);font-size:12px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px;list-style:none}
  #okwrap summary::-webkit-details-marker{display:none}
  .chip{display:inline-flex;align-items:center;gap:5px;background:#11211a;border:1px solid #1f3a2b;color:#9fe0b8;border-radius:20px;padding:3px 11px;font-size:12px;margin:0 5px 5px 0}
  .chip.warn{background:#231d10;border-color:#3a3016;color:var(--warn)}
  .ic{font-size:20px;width:26px;text-align:center;flex:none}
  .dot{width:12px;height:12px;border-radius:50%;flex:none}
  .dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.fail{background:var(--fail)} .dot.neutral{background:var(--mut)}
  #diagram{overflow-x:auto;margin:14px 0 6px;padding:14px 10px;background:var(--card);border:1px solid var(--line);border-radius:14px}
  #diagram svg{width:100%;min-width:620px;height:auto;display:block}
  #diagram rect,#diagram text,#diagram circle{cursor:help}
  .difoot{color:var(--mut);font-size:12px;text-align:center;margin-top:6px}
  .ic .gic{width:22px;height:22px;object-fit:contain;vertical-align:middle}
  .ic .gic+.gic{margin-left:3px}
  .lab{flex:1;min-width:0} .lab .t{font-weight:500} .lab .d{color:var(--mut);font-size:13px;white-space:pre-line;overflow-wrap:anywhere}
  /* Panneau « apps en trop » : la liste qu'on relit avant de fermer quoi que ce soit. */
  #extra .box{margin-top:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  #extra h3{margin:0 0 4px;font-size:14px} #extra .why{color:var(--mut);font-size:13px;margin-bottom:10px}
  #extra .apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin-bottom:12px}
  #extra .app{display:flex;align-items:center;gap:9px;background:#10141c;border:1px solid var(--line);border-radius:10px;padding:7px 10px;cursor:pointer;color:var(--tx);font:inherit;text-align:left;width:100%}
  #extra .app:hover{border-color:var(--accent)}
  #extra .app img{width:28px;height:28px;flex:none;border-radius:6px}
  #extra .app .noimg{width:28px;height:28px;flex:none;display:flex;align-items:center;justify-content:center;font-size:18px}
  #extra .app .n{flex:1;min-width:0;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* La marque bleue EST l'état sélectionné : un liseré et une pastille de la couleur
     d'accent, visibles d'un coup d'œil sur toute la vignette. */
  /* Idem : le marqueur latéral dit « cochée pour fermeture », sans repeindre le bouton.
     La pastille de gauche disparaît — le liseré la remplace et fait le même travail
     avec moins d'encre. */
  #extra .app{border-left-width:4px}
  #extra .app .mark{display:none}
  #extra .app.on{border-left-color:var(--accent)}
  #extra .acts{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  #log{white-space:pre-wrap;background:#0a0c11;border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:18px;font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--mut);max-height:200px;overflow:auto;display:none}
  /* Soundcheck intégré : des pastilles denses, pas les grandes tuiles de la page dédiée —
     elles doivent tenir sous les checks sans repousser le reste hors de l'écran. */
  #scdone.all{color:var(--ok)}
  .sctiles{display:flex;flex-wrap:wrap;gap:7px;margin-top:11px}
  .sctile{display:flex;align-items:center;gap:7px;background:#10141c;border:1px solid var(--line);
          border-radius:9px;padding:6px 10px;font-size:13px}
  /* Pas vérifié = ROUGE, pas neutre. Une pastille grise se lit « pas encore », donc
     « ce n'est pas grave » ; or c'est exactement ce qui manque pour savoir si le rig
     joue. La couleur doit dire la même chose que le cadre de la zone et que la bannière,
     sinon le tableau se contredit d'un niveau à l'autre. */
  /* Même langage que les lignes de « Configuration du rig » : un liseré à GAUCHE porte
     l'état, le reste de la pastille reste neutre. Tout colorer en rouge saturait le
     panneau au point de ne plus rien hiérarchiser — huit pastilles rouges vives crient
     autant qu'un bloquant réel, alors qu'elles disent seulement « pas encore joué ». */
  .sctile{border-left-width:4px;border-left-color:var(--fail)}
  .sctile.hit{border-left-color:var(--ok)}
  .sctile .sn{color:var(--mut);font-size:12px}
  .scbtn{padding:2px 8px;font-size:12px;border-radius:6px}
  .prow{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);border-left:4px solid var(--ok);border-radius:10px;padding:9px 13px;margin-bottom:7px}
  .prow .n{flex:1;font-weight:500} .prow .c{color:var(--mut);font-size:12px} .prow .l{color:var(--accent);font-size:12px;font-family:ui-monospace,Menlo,monospace}
  .empty{color:var(--mut);font-size:13px;padding:8px 2px}
  .smgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
  .smpanel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
  .smhead{font-weight:600;font-size:13px;margin-bottom:8px} .smhead .ch{color:var(--accent)}
  .notes{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px;min-height:4px}
  .note{background:#123322;border:1px solid var(--ok);color:var(--ok);border-radius:6px;padding:2px 7px;font-size:12px;font-family:ui-monospace,Menlo,monospace}
  .ccrow{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
  .ccname{width:62px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ccbar{flex:1;height:8px;background:#0a0c11;border-radius:4px;overflow:hidden}
  .ccfill{height:100%;background:var(--accent)} .ccval{width:30px;text-align:right;font-family:ui-monospace,Menlo,monospace}
  .smmeta{color:var(--mut);font-size:12px;margin-top:6px}
  .sch3{margin:16px 0 7px;font-size:13px;color:var(--mut);font-weight:600}
  /* Le flux brut s'appelle #midilog et NON #log : la page principale a déjà son propre
     #log, le journal des actions cliquées. Deux éléments de même id sur une page, c'est
     le second qui devient invisible à getElementById — un bug muet. */
  #midilog{white-space:pre-wrap;background:#0a0c11;border:1px solid var(--line);border-radius:10px;
    padding:11px;font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--mut);max-height:170px;overflow:auto}
  .scaudio{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px;
           padding-top:10px;border-top:1px solid var(--line)}
  .scaudio button{padding:5px 11px;font-size:12.5px}
  .scaudio span{color:var(--mut);font-size:12px;flex:1;min-width:180px}
  .scaudio.sig span{color:var(--ok)} .scaudio.mute span{color:var(--warn)}
  .scfoot{margin-top:10px;color:var(--mut);font-size:12px}
  .scfoot a{color:var(--accent);text-decoration:none}
  /* Deux colonnes au-dessus de 900 px, une seule en dessous : cette page se consulte
     aussi sur un téléphone, debout, cinq minutes avant de jouer. */
  /* Colonnes CSS et NON une grille : une grille aligne les zones d'une même rangée sur
     la plus haute, donc chaque zone courte creuse un trou sous elle — c'est exactement
     ce qui gâchait la place. Ici les zones s'empilent en remplissant chaque colonne de
     haut en bas, chacune à sa hauteur réelle, sans blanc entre elles.
     `break-inside:avoid` interdit qu'une zone soit coupée en deux d'une colonne à
     l'autre, ce que le multi-colonnes ferait volontiers. */
  .cols{column-gap:14px}
  .cols>.zone{break-inside:avoid;-webkit-column-break-inside:avoid;margin:0 0 14px}
  @media(min-width:820px){.cols{column-count:2}}
  @media(min-width:1450px){.cols{column-count:3}}
  @media(min-width:2000px){.cols{column-count:4}}
  .zone{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 15px;
        border-left-width:5px;border-left-color:var(--line)}
  .zone.z-ok{border-left-color:var(--ok)} .zone.z-warn{border-left-color:var(--warn)}
  .zone.z-fail{border-left-color:var(--fail)} .zone.z-info{border-left-color:var(--info)}
  .zh{display:flex;align-items:baseline;gap:10px;margin-bottom:9px}
  .zt{font-weight:600;font-size:14px} .zs{color:var(--mut);font-size:12.5px;margin-left:auto}
  /* Le résumé de la zone prend la couleur du cadre, info comprise : lire « 3 ouvertes en
     trop » en gris à côté d'un liseré bleu, c'est deux signaux pour un seul état. */
  .zone.z-ok .zs{color:var(--ok)} .zone.z-info .zs{color:var(--info)}
  .zone.z-warn .zs{color:var(--warn)} .zone.z-fail .zs{color:var(--fail)}
  #z-flow #diagram svg{min-width:0}
  #scdet summary{cursor:pointer;color:var(--mut);font-size:12.5px;margin-top:10px}
  .stamp{color:var(--mut);font-size:12px}
  @media(max-width:520px){.row{flex-wrap:wrap}}
</style></head><body>
<header>
  <h1>🎹 Rig — état global</h1>
  <div class="sub">Vue auto-rafraîchie (lecture seule). Les actions sont des clics explicites.</div>
  <div id="banner" class="banner warn">…chargement</div>
  <div class="bar">
    <span class="seg" id="modeseg">
      <button data-m="auto" onclick="setMode('auto')">Auto</button>
      <button data-m="live" onclick="setMode('live')">🎤 Live</button>
      <button data-m="studio" onclick="setMode('studio')">🎧 Studio</button>
    </span>
    <button class="primary" onclick="preflight()"
      title="LA seule action à connaître. Dans l'ordre : lance les 5 apps du rig → démarre la session anti-veille → ouvre le set du concert dans Ableton → range les fenêtres (seul Ableton reste visible) → applique tous les correctifs disponibles → re-vérifie tout.&#10;&#10;Ne ferme PAS les applis en trop : ça peut perdre un document non enregistré, donc ça garde sa confirmation dans le panneau dédié.">✨ Tout préparer</button>
    <button onclick="const l=document.getElementById('log');l.style.display=l.style.display==='block'?'none':'block'"
      title="Le détail de ce que les actions ont fait, ligne par ligne">📜 Journal</button>
  </div>
</header>
<main>
  <!-- Quatre zones, deux colonnes : tout tient sans faire défiler, et chaque zone porte
       la couleur du PIRE de son groupe. On voit donc d'un coup d'œil OÙ ça coince avant
       même de lire une ligne — sur scène, la question n'est pas « quel check » mais
       « quel domaine ». -->
  <div class="cols">
    <section class="zone" id="z-flow">
      <div class="zh"><span class="zt">Flux du rig</span><span class="zs" id="zs-flow"></span></div>
      <div id="diagram"></div>
    </section>
    <section class="zone" id="z-sc">
      <div class="zh"><span class="zt">🎹 Soundcheck</span><span class="zs" id="scdone"></span></div>
      <div class="sctiles" id="checks"></div>
      <!-- La mesure de signal n'est PAS une case du soundcheck, et la ranger parmi elles
           n'a produit que de la confusion (« pourquoi c'est rouge, j'entends le son ? »).
           Les autres cases CONSTATENT passivement ce qui arrive ; celle-ci PROVOQUE une
           mesure de 1,5 s qu'il faut demander. Rouge, elle accusait à tort ; comptée,
           elle rendait le soundcheck impossible à finir sans cliquer. Elle vit donc à
           part, en gris, avec ce qu'elle apporte écrit noir sur blanc. -->
      <div class="scaudio">
        <button onclick="scMeasure()" id="scmeasbtn">🔊 Mesurer le signal</button>
        <span id="scaudioinfo">le schéma dit sur QUEL périphérique Ableton est réglé ;
          ceci mesure s'il en sort vraiment du son</span>
      </div>
      <details id="scdet">
        <summary>Détail MIDI — contrôleurs, canaux, flux brut</summary>
        <div class="sch3">Contrôleurs actifs</div>
        <div id="ports"><div class="empty">Aucune activité — joue quelque chose.</div></div>
        <div class="sch3">Vue MIDI en direct, par canal</div>
        <div id="showmidi"><div class="empty">Rien reçu pour l'instant.</div></div>
        <div class="sch3">Flux brut</div>
        <div id="midilog">—</div>
      </details>
      <div class="scfoot" id="scstatus"></div>
    </section>
    <section class="zone" id="z-cfg">
      <div class="zh"><span class="zt">Configuration du rig</span><span class="zs" id="zs-cfg"></span></div>
      <div id="problems"></div>
      <details id="okwrap"><summary id="oksum"></summary><div id="okchips"></div></details>
    </section>
    <section class="zone" id="z-apps">
      <div class="zh"><span class="zt">Applis ouvertes</span><span class="zs" id="zs-apps"></span></div>
      <div id="extra"></div>
    </section>
  </div>
  <div id="log"></div></main>
<script>
const IC={ok:"✅",warn:"⚠️",fail:"❌"};
// Le sens des couleurs, écrit UNE fois et réutilisé partout. C'est la question posée par
// Benoît devant le schéma — « à quoi correspond le vert ? » — et elle vaut pour chaque
// ligne, chaque pastille et chaque cadre : une couleur qu'il faut deviner ne renseigne
// personne, et deux explications différentes du même vert seraient pires que rien.
const TIP={
  ok:"✅ Vert — vérifié à l'instant, rien à faire.",
  info:"ℹ️ Bleu — absent, et c'est normal ici (matériel optionnel, ou réseau de scène au bureau). Ne compte PAS comme un problème : le rig reste prêt.",
  warn:"⚠️ Orange — jouable, mais quelque chose mérite un coup d'œil avant de monter sur scène.",
  fail:"❌ Rouge — bloquant : en l'état, le rig ne fera pas ce qu'on attend de lui."};
// La bannière est le SEUL endroit qu'on regarde en montant sur scène, donc elle doit
// intégrer le soundcheck : afficher « tout est vert » pendant que la zone Soundcheck est
// rouge, c'est se contredire à 20 cm d'écart. Les checks viennent de /api/state, le
// soundcheck de /api/midi — deux sondages distincts, une seule phrase de synthèse.
let lastState=null;
function paintBanner(){
  const s=lastState;if(!s)return;
  const b=document.getElementById("banner");
  b.title="Synthèse de TOUT : les checks (zone Configuration) et le soundcheck (les gestes joués).\n"
    +"Elle ne passe au vert que si les deux le sont — un rig vérifié sur le papier mais jamais joué n'est pas un rig prêt.";
  b.className="banner "+s.status;
  b.textContent=s.status==="ok"?"✅ Rig prêt — tout est vert, soundcheck compris."
    :s.status==="warn"?`⚠️ Rig jouable — ${s.warns} avertissement(s).`
    :`❌ Rig PAS prêt — ${s.fails} bloquant(s), ${s.warns} avertissement(s).`;
}
// Une zone porte la couleur du PIRE de son groupe : c'est la seule synthèse qui ne ment
// pas (une zone verte doit vouloir dire « rien à voir ici », sans exception cachée).
// INFO compte comme VERT pour la couleur d'une zone, exactement comme `worst()` l'ignore
// côté serveur : « absent, et c'est normal » (le réseau de scène au bureau, les lampes
// d'ambiance) ne rend pas le rig moins prêt. Le teinter autrement qu'en vert ferait
// croire qu'il reste quelque chose à faire dans une zone où il n'y a rien à faire.
// INFO a sa PROPRE couleur, le bleu : « absent, et c'est normal » n'est ni « rien à
// signaler » (vert) ni « à regarder » (orange), c'est une troisième chose. Je l'avais
// d'abord fondu dans le vert pour corriger un cadre « Flux du rig » bleu alors que tout
// y était vert — mais le vrai défaut était ailleurs : ce cadre se calculait sur TOUS les
// checks au lieu des siens. La cause corrigée, la couleur peut redevenir juste.
const SEV={ok:0,info:1,warn:2,fail:3},SEVN=["ok","info","warn","fail"];
function worstOf(items){return SEVN[items.reduce((m,i)=>Math.max(m,SEV[i.status]??0),0)];}
const ZONETIP={
  "z-flow":"Le chemin du signal, de l'iPhone et du clavier jusqu'à la sortie audio.\nChaque bloc prend la couleur du pire de SES checks — survole un bloc pour voir lesquels.",
  "z-sc":"Ce qu'il faut avoir JOUÉ pour prouver que le rig répond : pédale, notes, souffle, morsure, inclinaisons de tête.\nRien ici n'est déclaratif — chaque case attend un vrai geste.\nUn réveil du Mac remet le tout à zéro : après une veille, l'USB peut avoir changé.",
  "z-cfg":"L'état du matériel et des réglages, vérifié en continu et sans rien toucher.\nLes lignes vertes sont repliées en bas ; ne restent en haut que celles qui demandent quelque chose.",
  "z-apps":"Les applis ouvertes dont le rig n'a pas besoin. Une notification qui passe devant le set, du CPU pris à Ableton, un updater qui se réveille — sur scène, chacune est un risque.\nJamais bloquant : une app en trop fragilise le rig, elle ne l'empêche pas de jouer."};
function zone(id,st,txt){
  const el=document.getElementById(id);if(!el)return;
  el.className="zone z-"+st;
  // Le cadre porte la couleur du PIRE de la zone : l'infobulle le dit, sinon on peut
  // croire qu'une zone rouge est rouge en entier.
  const h=el.querySelector(".zh");
  if(h)h.title=(ZONETIP[id]||"")+`\n\nLe cadre prend la couleur du plus grave de la zone.\n${TIP[st]||""}`;
  if(txt!==undefined){const z=el.querySelector(".zs");if(z)z.textContent=txt;}
}
// --- soundcheck intégré ---------------------------------------------------------
let scTimer=null;
// L'écoute suit l'ÉTAT du soundcheck, plus le dépliage d'un panneau : tant qu'il reste
// quelque chose à recevoir, on écoute ; dès que tout est arrivé, on arrête (inutile de
// tenir les ports MIDI du rig ouverts en permanence). Un réveil du Mac remet le
// soundcheck à zéro côté serveur, ce que le sondage lent voit tout seul et qui relance
// l'écoute — c'est ce qui rend « non vérifié = erreur » vivable plutôt qu'agaçant.
async function scEnsure(running,complete){
  if(!complete&&!running) await fetch("/api/midi/start",{method:"POST"});
  if(complete&&running)   await fetch("/api/midi/stop",{method:"POST"});
}
function scItems(s){
  const f=s.flags||{},h=s.head||{};
  // Chaque geste du breath compte DOUBLE : vu au capteur, et vu après traduction par
  // Bome. C'est l'écart entre les deux qui dit où ça casse — capteur muet (rien des
  // deux côtés) ou préréglage Bome éteint (source oui, sortie non). Un seul témoin
  // n'aurait jamais distingué les deux, et c'est justement la panne la plus vicieuse :
  // le contrôleur va bien, et pourtant rien n'arrive dans Ableton.
  const chain=(s.chain||[]).map(g=>({
    icon:"🌬️",lbl:g.name,hit:g.raw&&g.out,
    sn:g.raw&&g.out?"capteur + Bome":g.raw?"capteur OK, RIEN ne sort de Bome":g.out?"sortie seule":"pas de signal"}));
  return [
    {icon:"🦶",lbl:"Pédale",hit:!!f.pedal_cc64,sn:f.pedal_cc64?`CC64=${f.pedal_cc64.value}`:""},
    {icon:"🎵",lbl:"Notes",hit:f.notes>0,sn:f.notes>0?`${f.notes}`:""},
    ...chain,
  ];
}
const NN=["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
function noteName(n){return NN[n%12]+(Math.floor(n/12)-1);}
function renderShowMidi(chs){
  const sm=document.getElementById("showmidi");
  if(!chs||!chs.length){sm.innerHTML='<div class="empty">Rien reçu pour l\'instant.</div>';return;}
  sm.innerHTML='<div class="smgrid"></div>';const grid=sm.firstChild;
  for(const c of chs){
    const notes=c.notes.map(x=>`<span class="note">${noteName(x.n)} <small>${x.v}</small></span>`).join("");
    const ccs=c.cc.map(x=>`<div class="ccrow"><span class="ccname">${x.name||("CC"+x.n)}</span>
      <span class="ccbar"><span class="ccfill" style="width:${Math.round(x.v/127*100)}%"></span></span>
      <span class="ccval">${x.v}</span></div>`).join("");
    let meta=[];if(c.pitch!=null&&c.pitch!==0)meta.push("pitch "+c.pitch);
    if(c.prog!=null)meta.push("prog "+c.prog);if(c.at!=null&&c.at!==0)meta.push("aftertouch "+c.at);
    const d=document.createElement("div");d.className="smpanel";
    d.innerHTML=`<div class="smhead">${c.port} · <span class="ch">Ch ${c.ch}</span></div>
      <div class="notes">${notes||'<span class="smmeta">aucune note tenue</span>'}</div>
      ${ccs}${meta.length?`<div class="smmeta">${meta.join(" · ")}</div>`:""}`;
    grid.appendChild(d);
  }
}
// Le son ne se devine pas : on MESURE. Un tap CoreAudio sur la sortie d'Ableton pendant
// 1,5 s dit s'il y a du signal ou du silence — pas ce qu'on entend, mais « ça sort ou
// pas », ce qui est justement la question qu'un check ne savait pas trancher. Sur
// demande uniquement, d'où le bouton : chaque mesure ouvre un tap natif.
let scAudio=null;
async function scMeasure(){
  scAudio={pending:true};scRender();
  try{
    const r=await(await fetch("/api/audiolevel",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({seconds:1.5})})).json();
    scAudio=r;
  }catch(e){scAudio={error:String(e)};}
  scRender();
}
let scLast=null;
function scRender(){
  const box=document.querySelector(".scaudio"),info=document.getElementById("scaudioinfo");
  if(!box||!info)return;
  box.className="scaudio";
  if(!scAudio){info.textContent="le schéma dit sur QUEL périphérique Ableton est réglé ; ceci mesure s'il en sort vraiment du son";}
  else if(scAudio.pending){info.textContent="mesure en cours (1,5 s)…";}
  else if(scAudio.error){info.textContent=scAudio.error;}
  else if(scAudio.signal){box.className="scaudio sig";
    info.textContent=`✅ du son sort bien d'Ableton (RMS ${scAudio.rms.toFixed(3)})`;}
  else{box.className="scaudio mute";
    info.textContent="⚠️ silence mesuré — Ableton est réglé sur le bon périphérique mais rien n'en sort (volume, piste en solo, câble ?)";}
  if(scLast)scDraw(scLast);
}
async function scPoll(){
  let s;try{s=await(await fetch("/api/midi")).json();}catch(e){return;}
  scLast=s;scDraw(s);
}
function scDraw(s){
  const items=scItems(s),host=document.getElementById("checks");
  host.innerHTML="";
  for(const it of items){
    const d=document.createElement("div");
    d.className="sctile"+(it.hit?" hit":"");
    d.title=`${it.lbl}\n`+(it.hit
      ? "✅ Reçu — le geste est arrivé jusqu'ici."
      : "❌ Pas encore reçu. Ce n'est pas une panne constatée : c'est une preuve qui manque. Fais le geste, la case verdit seule.")
      +(it.sn?`\n\n${it.sn}`:"")
      +(it.lbl.match(/Tête|Souffle|Morsure/)
        ? "\n\nTesté aux DEUX bouts : au capteur (le BBC bouge) et en sortie de Bome MIDI Translator (la traduction a bien eu lieu). « capteur OK, RIEN ne sort de Bome » = préréglage Bome éteint ou projet non chargé."
        : "");
    d.innerHTML=`<span>${it.hit?"✅":it.icon}</span><span>${it.lbl}</span>`+
                (it.sn?`<span class="sn">${it.sn}</span>`:"");
    if(it.btn)d.innerHTML+=`<button class="scbtn" onclick="scMeasure()">${it.btn}</button>`;
    host.appendChild(d);
  }
  const got=items.filter(i=>i.hit).length,complete=got===items.length;
  document.getElementById("scdone").textContent=
    complete?`✅ vérifié — ${got}/${items.length}`:`❌ ${items.length-got} sur ${items.length} pas encore vérifié(s)`;
  // Non vérifié = ROUGE, au même titre qu'un check en panne. Un soundcheck pas fait
  // n'est pas « en attente » : c'est un rig dont personne ne sait s'il joue.
  zone("z-sc",complete?"ok":"fail");
  scEnsure(!!s.running,complete);
  const host2=document.getElementById("ports");
  if(!s.ports||!s.ports.length){host2.innerHTML='<div class="empty">Aucune activité — joue quelque chose.</div>';}
  else{host2.innerHTML="";for(const p of s.ports){const d=document.createElement("div");d.className="prow";
    d.innerHTML=`<div class="n">${p.name}</div><div class="l">${p.last||""}</div><div class="c">${p.count} msg</div>`;host2.appendChild(d);}}
  renderShowMidi(s.channels);
  document.getElementById("midilog").textContent=(s.events||[]).map(e=>`${e.port.padEnd(22).slice(0,22)}  ${e.msg}`).join("\n")||"—";
  document.getElementById("scstatus").textContent=
    s.running?`● écoute — ${(s.watched||[]).length} ports (${(s.watched||[]).join(", ")||"—"})`:"écoute arrêtée";
}
async function setMode(m){await fetch("/api/mode",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:m})});refresh();}
async function manualSet(key,val){await fetch("/api/manual",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,value:val})});refresh();}
function iconFor(k){
  if(k.startsWith("xapp:"))return"🧹";
  if(k.startsWith("app:Ableton"))return"🎵";
  if(k.startsWith("app:Stream"))return"🎛️";
  if(k.startsWith("app:Bome"))return"🔀";
  if(k.startsWith("app:Stage"))return"▶️";
  if(k.startsWith("usb:"))return"🎛️";
  if(k.startsWith("lamp:"))return"💡";
  if(k.startsWith("kbd:breath"))return"🌬️";
  if(k.startsWith("kbd:"))return"🎹";
  if(k.startsWith("net:stage"))return"🌐";
  if(k.startsWith("net:"))return"📱";
  if(k.startsWith("sys:vpn"))return"🔒";
  if(k.startsWith("sys:output"))return"💻";
  if(k.startsWith("sys:amphetamine"))return"☕";
  if(k.startsWith("sys:macpower"))return"🔌";
  if(k.startsWith("sys:iphonecharge"))return"🔋";
  if(k.startsWith("audio:live"))return"🎚️";
  if(k==="audio")return"🔊";
  if(k.startsWith("midi?:"))return"🎹";
  if(k.startsWith("midi:"))return"🔌";
  return"•";
}
// --- signal-flow diagram: nodes coloured by the checks that feed them ----------
const stc=s=>({ok:"#2ecc71",info:"#5aa9e6",warn:"#f4b942",fail:"#ff5468"}[s]||"#55607a");
function nstat(keys,map){let s="neutral";for(const k of keys){const v=map[k];
  if(v==="fail")return"fail";if(v==="warn")s="warn";else if(v==="ok"&&s==="neutral")s="ok";}return s;}
const DNODES=[
  // Le schéma doit couvrir TOUT le rig, sinon la zone « Configuration » devient la vraie
  // liste et le schéma un ornement. Le réseau de scène et les lampes y entrent donc :
  // ce sont des maillons, pas des à-côtés — le modem distribue les adresses dont vivent
  // l'iPhone et les lampes, et une lampe éteinte se voit sur scène.
  {e:"📡",l:"Réseau scène",k:["net:stage"],x:80,y:40},
  // « iPhone en charge » rejoint le bloc iPhone : c'est le même objet physique, et le
  // séparer obligeait à croiser deux lignes pour savoir si le téléphone tiendra le set.
  {e:"📱",l:"iPhone",k:["net:iphone","sys:iphonecharge"],x:80,y:118},
  {e:"🎛️",l:"Stream Deck",k:["app:Stream Deck","usb:"],x:80,y:196},
  {e:"🎹",l:"Clavier",k:["kbd:keyboard","audio:Port audio"],x:80,y:274},
  {e:"💡",l:"Lampes",k:["lamp:"],x:300,y:40},
  {e:"🔀",l:"Bome",k:["app:Bome MIDI Translator","app:Bome Network","kbd:breath"],x:300,y:157},
  {e:"▶️",l:"Stage Traxx",k:["app:Stage Traxx"],x:300,y:274},
  {e:"🎵",l:"Ableton Live",k:["app:Ableton","midi:Ableton Loopback"],x:516,y:196},
  {e:"🔊",l:"Sortie audio",k:["audio:live","sys:output"],x:684,y:196},
];
// Le réseau alimente l'iPhone (Bome Network passe par le WiFi) et les lampes.
const DEDGES=[[0,1],[0,4],[1,5],[2,5],[3,7],[5,7],[6,7],[7,8]];
function renderDiagram(items){
  const map={};for(const it of items)map[it.key]=it.status;
  const N=DNODES.map(n=>({...n,s:nstat(n.k,map)}));
  let e="";
  for(const [a,b] of DEDGES){const A=N[a],B=N[b];
    const dx=B.x-A.x,dy=B.y-A.y,d=Math.hypot(dx,dy),ux=dx/d,uy=dy/d;
    const x1=A.x+ux*76,y1=A.y+uy*30,x2=B.x-ux*80,y2=B.y-uy*30;
    e+=`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#3a4356" stroke-width="2" marker-end="url(#ar)"/>`;}
  let g="";
  // Une boîte verte ne dit pas POURQUOI elle est verte : elle résume plusieurs checks
  // dont on ne voit ni le nom ni la valeur. L'infobulle les déplie — un check par ligne,
  // avec son état et son détail — pour qu'un survol réponde sans aller chercher la zone
  // « Configuration » plus bas.
  const byKey={};for(const it of items)byKey[it.key]=it;
  for(const n of N){const c=stc(n.s);
    const feeds=items.filter(it=>n.k.some(k=>it.key.startsWith(k)));
    const tip=feeds.length
      ? `${n.l} — ${feeds.length} check(s)\n`+feeds.map(it=>
          `${IC[it.status]||"ℹ️"} ${it.label}${it.detail?" — "+it.detail.replace(/\n→ /g," → "):""}`).join("\n")
      : `${n.l} — aucun check ne l'alimente`;
    // Le <title> est répété DANS le rectangle, et pas seulement sur le groupe : en SVG,
    // l'infobulle ne se déclenche qu'au survol d'une forme réellement peinte, et le
    // groupe n'en est pas une. Le texte et la pastille, eux, ne couvrent que quelques
    // pixels — d'où un survol qui semblait ne rien faire sur le bloc iPhone.
    const T=`<title>${tip.replace(/&/g,"&amp;").replace(/</g,"&lt;")}</title>`;
    g+=`<g>${T}`
      +`<rect x="${n.x-72}" y="${n.y-25}" width="144" height="50" rx="12" fill="#12161e" stroke="${c}" stroke-width="2">${T}</rect>`
      +`<text x="${n.x-56}" y="${n.y+7}" font-size="21">${n.e}${T}</text>`
      +`<text x="${n.x-28}" y="${n.y+5}" fill="#e7ebf2" font-size="12.5" font-weight="600">${n.l}${T}</text>`
      +`<circle cx="${n.x+58}" cy="${n.y-14}" r="4.5" fill="${c}">${T}</circle></g>`;}
  document.getElementById("diagram").innerHTML=
    `<svg viewBox="0 0 764 320" xmlns="http://www.w3.org/2000/svg">`
    +`<defs><marker id="ar" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">`
    +`<path d="M0,0 L7,3 L0,6 Z" fill="#3a4356"/></marker></defs>${e}${g}</svg>`
    +`<div class="difoot">Flux du rig — chaque bloc prend la couleur de ses checks. Survole un bloc pour voir lesquels.</div>`;
}
// --- panneau « apps en trop » : lister avec les icônes, puis demander confirmation -----
// La sélection est mémorisée entre deux rafraîchissements (la page se redessine toutes
// les 4 s) : sans ça, décocher une app serait annulé une seconde plus tard.
let extraSel=null;
function renderExtra(list){
  const host=document.getElementById("extra");
  if(!list||!list.length){host.innerHTML="";extraSel=null;return;}
  const paths=list.map(a=>a.path);
  if(extraSel===null)extraSel=new Set(paths);            // tout coché au premier affichage
  else for(const p of paths)if(!extraSel.has(p)&&!extraSel.__seen?.has(p))extraSel.add(p);
  extraSel.__seen=new Set(paths);
  const n=[...extraSel].filter(p=>paths.includes(p)).length;
  host.innerHTML=`<div class="box"><h3>🧹 ${list.length} appli(s) ouverte(s) dont le rig n'a pas besoin</h3>
    <div class="why" title="Une app en trop, c'est une notification qui passe devant le set, du CPU pris à Ableton, un updater qui se réveille au mauvais moment.">Décoche celles à garder, puis confirme.</div>
    <div class="apps"></div>
    <div class="acts">
      <button class="fix" id="quitbtn" onclick="quitApps()">Quitter les ${n} appli(s) cochées</button>
      <button onclick="extraAll(true)">Tout cocher</button>
      <button onclick="extraAll(false)">Tout décocher</button>
    </div></div>`;
  const grid=host.querySelector(".apps");
  for(const a of list){
    // Un vrai bouton, sélectionné ou non, plutôt qu'une case à cocher posée à côté d'un
    // libellé : la cible de clic devient la vignette entière (l'icône, le nom, la marge),
    // au lieu d'un carré de 16 px. Et l'état se lit à la MARQUE BLEUE, pas à une coche
    // grise qu'il fallait chercher. `aria-pressed` porte l'état pour qui n'y voit pas.
    const el=document.createElement("button");
    el.className="app"+(extraSel.has(a.path)?" on":"");
    el.setAttribute("aria-pressed",extraSel.has(a.path)?"true":"false");
    el.innerHTML=`<span class="mark"></span>
      <img src="/api/appicon?path=${encodeURIComponent(a.path)}" alt=""
           onerror="this.outerHTML='<span class=&quot;noimg&quot;>📦</span>'">
      <span class="n" title="${a.path}">${a.name}</span>`;
    el.onclick=()=>{
      const on=!extraSel.has(a.path);
      on?extraSel.add(a.path):extraSel.delete(a.path);
      el.classList.toggle("on",on);el.setAttribute("aria-pressed",on?"true":"false");
      const c=[...extraSel].filter(p=>paths.includes(p)).length;
      document.getElementById("quitbtn").textContent=`Quitter les ${c} appli(s) cochées`;};
    grid.appendChild(el);
  }
}
function extraAll(on){const l=lastUnexpected||[];extraSel=new Set(on?l.map(a=>a.path):[]);renderExtra(l);}
let lastUnexpected=[];
async function quitApps(){
  const paths=(lastUnexpected||[]).map(a=>a.path).filter(p=>extraSel&&extraSel.has(p));
  if(!paths.length){logline("aucune appli cochée");return;}
  const names=(lastUnexpected||[]).filter(a=>paths.includes(a.path)).map(a=>a.name);
  // Deuxième garde-fou, volontairement moche : fermer une app peut faire perdre du
  // travail non enregistré. On nomme ce qu'on va fermer, en toutes lettres.
  if(!confirm(`Quitter ${paths.length} appli(s) ?\n\n${names.join("\n")}\n\nChacune est priée de quitter proprement : une app avec un document non enregistré posera sa propre question.`))return;
  logline(`🧹 fermeture de ${paths.length} appli(s)…`);
  const r=await(await fetch("/api/quit-apps",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({paths,dry:false})})).json();
  logline((r.ok?"✔ ":"✖ ")+(r.message||"").replace(/\n/g,"  |  "));
  extraSel=null;setTimeout(refresh,900);
}
function logline(t){const l=document.getElementById("log");l.style.display="block";l.textContent=(new Date().toLocaleTimeString()+"  "+t+"\n"+l.textContent).slice(0,4000);}
async function refresh(){
  const y=window.scrollY;
  let s;try{s=await(await fetch("/api/state")).json();}catch(e){return;}
  lastState=s;paintBanner();
  document.querySelectorAll("#modeseg button").forEach(b=>{
    b.classList.toggle("on",b.dataset.m===s.requested);
    // Seul Auto porte la couleur du mode résolu : sur Live ou Studio choisis à la main,
    // le bouton EST déjà la réponse.
    b.classList.toggle("res-live",b.dataset.m==="auto"&&s.mode==="live");
    b.classList.toggle("res-studio",b.dataset.m==="auto"&&s.mode==="studio");
    if(b.dataset.m==="auto")b.title=`Auto — actuellement ${s.mode==="live"?"🎤 Live":"🎧 Studio"}`;
  });
  // Chaque zone prend la couleur du pire de SON groupe, et son résumé dit combien.
  const xapps=s.items.filter(i=>i.key.startsWith("xapp:"));
  const cfg=s.items.filter(i=>!i.key.startsWith("xapp:")&&i.key!=="sc:play");
  const nbad=cfg.filter(i=>i.status==="fail"||i.status==="warn").length;
  // Chaque zone sur SES items, celle-ci comprise : les blocs du schéma savent quelles
  // clés ils couvrent, on reprend exactement la même liste.
  const flowKeys=DNODES.flatMap(n=>n.k);
  const flow=s.items.filter(i=>flowKeys.some(k=>i.key.startsWith(k)));
  const fbad=flow.filter(i=>i.status==="fail").length;
  zone("z-flow",worstOf(flow),fbad?`${fbad} bloquant(s)`:"rien à signaler");
  zone("z-cfg",worstOf(cfg),nbad?`${nbad} à corriger`:"rien à corriger");
  zone("z-apps",xapps.length?"info":"ok",
       xapps.length?`${xapps.length} ouverte(s) en trop`:"aucune app en trop");
  renderDiagram(s.items);
  lastUnexpected=s.unexpected||[];
  renderExtra(lastUnexpected);
  // Tout ce qui n'est pas "ok" reste en ligne pleine, TRIÉ fail > warn > info ; seuls les
  // "ok" se replient en pastilles. Les "info" sont donc visibles par défaut, en bas de la
  // liste : c'est de l'optionnel débranché, on veut le voir sans le confondre avec un
  // problème — et surtout savoir que le check a tourné plutôt que de le croire oublié.
  const RANK={fail:0,warn:1,info:2};
  // Les apps en trop ont déjà leur panneau juste au-dessus, avec leurs icônes, leurs
  // cases et le bouton qui les ferme. Les répéter ici en lignes d'info ajoutait autant
  // de lignes que d'apps ouvertes, pour dire deux fois la même chose et sans rien
  // d'actionnable. Elles restent dans les checks — le terminal, lui, n'a pas de panneau
  // et en a besoin ; c'est seulement le doublon À L'ÉCRAN qu'on retire.
  // « Soundcheck joué » sort de la liste comme les apps en trop : il a sa propre zone,
  // qui dit la même chose en détaillant chaque geste. Il RESTE dans /api/state — c'est
  // lui qui fait rougir la barre de menus, la pastille et le liseré ; c'est seulement le
  // doublon à l'écran qu'on retire, pas le check.
  const shown=s.items.filter(it=>!it.key.startsWith("xapp:")&&it.key!=="sc:play");
  const bad=shown.filter(it=>it.status!=="ok").sort((a,b)=>(RANK[a.status]??3)-(RANK[b.status]??3));
  const good=shown.filter(it=>it.status==="ok");
  const prob=document.getElementById("problems");
  if(!bad.length){prob.innerHTML='<div class="allok">✅ Tout est vert — rien à corriger.</div>';}
  else{
    // Bandeau rassurant quand il ne reste QUE de l'info : la liste n'est pas vide, mais
    // il n'y a rien à corriger — sans ce mot, une liste non vide se lit comme un problème.
    prob.innerHTML = bad.some(it=>it.status!=="info") ? ""
      : '<div class="allok">✅ Rien à corriger — seulement de l\'optionnel non branché.</div>';
    for(const it of bad){
    const row=document.createElement("div");row.className="row "+it.status;
    row.title=`${it.label}\n${TIP[it.status]||""}`
      +(it.detail?`\n\n${it.detail.replace(/\n→ /g,"\n→ ")}`:"")
      +(it.remedy?`\n\nCorrectif proposé : ${it.remedy}`:"");
    // Les vraies vignettes quand on en a une, l'emoji de famille sinon : un check sans
    // objet matériel ni app (l'alimentation, le réseau) n'a rien de mieux à montrer.
    const vign=(it.icons&&it.icons.length)
      ? it.icons.map(u=>`<img src="${u}" alt="" class="gic" loading="lazy">`).join("")
      : iconFor(it.key);
    row.innerHTML=`<div class="ic">${vign}</div>
      <div class="lab"><div class="t">${it.label}</div>${it.detail?`<div class="d">${it.detail}</div>`:""}</div>
      <div class="dot ${it.status}"></div>`;
    if(it.remedy){const btn=document.createElement("button");btn.className="fix";btn.textContent=it.remedy;
      btn.onclick=()=>fix(it.key,it.remedy);row.appendChild(btn);}
    if(it.key==="sys:iphonecharge"){const b=document.createElement("button");b.className="fix";
      b.textContent="✓ Confirmer en charge";b.onclick=()=>manualSet("iphone_charge",true);row.appendChild(b);}
    prob.appendChild(row);
  }}
  document.getElementById("oksum").textContent=good.length?`▸ ${good.length} checks OK (déplier)`:"";
  const okc=document.getElementById("okchips");okc.innerHTML="";
  for(const it of good){const c=document.createElement("span");c.className="chip";
    c.innerHTML=`${iconFor(it.key)} ${it.label}`;
    c.title=`${it.label}\n${TIP[it.status]||""}`+(it.detail?`\n\n${it.detail}`:"");
    if(it.key==="sys:iphonecharge"){c.style.cursor="pointer";c.title="cliquer pour réinitialiser";
      c.onclick=()=>manualSet("iphone_charge",false);}
    okc.appendChild(c);}
  document.getElementById("stamp").textContent="maj "+new Date().toLocaleTimeString();
  window.scrollTo(0,y);
}
async function fix(key,label){
  logline(`→ ${label}…`);
  const r=await(await fetch("/api/fix",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({key,dry:false})})).json();
  logline((r.ok?"✔ ":"✖ ")+(r.message||"").replace(/\n/g,"  |  "));
  setTimeout(refresh,800);
}
// Ranger les fenêtres : les apps continuent de tourner, elles disparaissent juste de
// l'écran. QUI est rangé est une décision de CONFIG (`[windows.apps]` dans rig.toml, où
// Ableton est en "keep" parce que le set EST l'écran de scène) — pas une case à cocher
// qui pouvait contredire le réglage. Pour tout ranger, Ableton compris : `rig tidy --all`.
async function tidy(){
  logline(`🪟 rangement des fenêtres…`);
  const r=await(await fetch("/api/windows",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({all:false,dry:false})})).json();
  logline((r.ok?"✔ ":"✖ ")+(r.message||"").replace(/\n/g,"  |  "));
}
async function preflight(){
  logline(`▶ préflight…`);
  const r=await(await fetch("/api/preflight",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({dry:false})})).json();
  logline((r.message||"").replace(/\n/g,"  |  "));
  setTimeout(refresh,1200);
}
// 1 s : le serveur sert un instantané déjà calculé, la lecture ne coûte rien.
refresh();setInterval(refresh,1000);
// Le soundcheck se sonde en continu — même « vérifié », il faut voir le moment où un
// réveil du Mac le remet à zéro côté serveur (voir midimon.sleep_stamp).
scPoll();setInterval(scPoll,700);
</script></body></html>"""


