"""The dashboard's HTTP surface — routes, and nothing else.

Pure stdlib (http.server). What the rig's state IS lives next door in state.py; this
file only knows how to serve it. The page auto-refreshes the state (read-only checks);
fixes and the full preflight are explicit POSTs triggered by buttons, and honour the
dry-run toggle end to end. Binds to 127.0.0.1 by default; [server].host can widen that
to the local network so the phone can publish its own state (POST /api/phone) — there
is no authentication, so only on a network you trust.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import alerts
from ... import apps, audiolevel, checks, spectrum, windows
from ...devices import SVG, app_path_for
from ...fix import launch, remedy
from . import state
from .state import (_MANUAL, _MODE, _MON, _STATE, build_state, cfg_tidy_after,
                   phone_record, phone_snapshot)

# The page is a FILE, read at startup — not a Python string literal. Seven hundred lines
# of HTML inside a .py buy nothing and cost everything: no syntax highlighting, no
# formatter, and a stray triple quote in the markup breaks the whole module.
#
# The path is relative to THIS MODULE, never to the working directory: the launchd agent
# starts the dashboard without any `cd`, so a relative path would resolve against `/`.
PAGE = (Path(__file__).resolve().parent / "page.html").read_text(encoding="utf-8")


def set_requested_mode(mode: str | None) -> None:
    """Force the mode the dashboard reports on (`readyset serve --mode live`).

    A function rather than a reach into state._MODE from the CLI: the store is shared
    with the background loop, and a single writer is easier to trust than two.
    """
    _MODE["requested"] = mode


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
            svg = SVG.get(want)
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
            known |= {app_path_for(self.cfg, lbl)
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
        elif self.path.startswith("/api/audio/spectrum"):
            # Hors de build_state, délibérément : une touche Stream Deck rafraîchit une
            # dizaine de fois par seconde, alors que l'état complet se recalcule toutes les
            # 4 s. Les mêler ferait payer une capture audio à chaque tour d'état — et un
            # spectre vieux de 4 s ne ressemblerait à rien.
            self._json(spectrum.snapshot(self.cfg))
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
            phone_record(charging=body.get("charging"), battery=body.get("battery"),
                         name=body.get("name"), source="phone")
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
            # Compter les TENTATIVES comme des réparations était le pire défaut de cette
            # action : le 2026-08-22, tous les correctifs d'interface ont été refusés par
            # macOS (autorisation d'accessibilité manquante) et la mise en place a
            # néanmoins répondu « 🔧 2 correctif(s) appliqué(s) », ok: true, en vert.
            # Ableton tournait alors sur « No Device » — silence complet, annoncé comme
            # un succès. Un rapport qui se trompe dans CE sens-là est pire que pas de
            # rapport : il empêche d'aller regarder.
            fixed, failed = 0, []
            for r in todo:
                rem = remedy.resolve(self.cfg, r)
                if not rem:
                    continue
                ok, msg = rem.run(dry)
                if ok:
                    fixed += 1
                else:
                    failed.append(f"{rem.label} — {msg}")
                logs.append(f"  {'✔' if ok else '✖'} {rem.label} — {msg}")
            if fixed:
                logs.append(f"  🔧 {fixed} correctif(s) appliqué(s)")
            elif not failed:
                logs.append("  🔧 aucun correctif automatique à appliquer")
            # Le verdict passe EN TÊTE : c'est la première ligne lue, et souvent la seule.
            if failed:
                head = [f"⛔ {len(failed)} correctif(s) N'ONT PAS pu être appliqués :"]
                head += [f"   • {f}" for f in failed]
                head.append("")
                logs[:0] = head

            # ON RANGE EN DERNIER, pas au milieu (2026-08-22). `bring_up` finit par
            # tidy_windows, mais les correctifs tournent APRÈS lui : « Ouvrir le set »
            # relance Ableton, « Relancer Bome » rouvre une fenêtre — toutes arrivées
            # trop tard pour le rangement qui les précédait. Et une app qui vient de
            # démarrer crée souvent sa fenêtre après les 2 s de settle : Bome Network
            # s'est retrouvé VISIBLE et non réduit à la fin d'une mise en place, alors que
            # sa politique dit « minimize ». Un second passage est idempotent — il ne
            # coûte qu'une poignée de millisecondes quand il n'y a rien à ranger.
            if not dry and cfg_tidy_after(self.cfg):
                time.sleep(1.0)   # laisse aux fenêtres tardives le temps d'exister
                windows.tidy(self.cfg, log=logs.append, dry_run=False)

            # La fermeture des applis en trop n'est PAS ici : elle peut faire perdre un
            # document non enregistré, donc elle garde sa confirmation nommant chaque app.
            # Une action « magique » ne doit rien détruire sans qu'on l'ait vu venir.
            self._json({"ok": not failed, "message": "\n".join(logs)})
        else:
            self._json({"ok": False, "message": "route inconnue"}, code=404)


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


def _bonjour_name() -> str:
    """Le nom en .local sous lequel les autres machines trouvent ce Mac.

    Piège : `socket.gethostname()` rend le nom du SHELL — ici « macbook-pro-benoit »,
    qui ne résout que sur cette machine. Bonjour publie le LocalHostName, un troisième
    nom (macOS en tient trois : ComputerName, HostName, LocalHostName) — ici
    « MacBook-Pro-de-Benoit.local », le seul que le téléphone puisse joindre. Imprimer
    l'autre revenait à donner la seule URL qui ne marche pas.
    """
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if out:
            return f"{out}.local"
    except Exception:
        pass
    name = socket.gethostname()
    return name if "." in name else f"{name}.local"


# Un par navigateur, parce que leurs dictionnaires AppleScript diffèrent : Chrome
# sélectionne un onglet par son INDICE dans la fenêtre, Safari par l'objet lui-même.
_RETROUVER_ONGLET = [
    ("Chrome", '''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      set i to 0
      repeat with t in tabs of w
        set i to i + 1
        if URL of t starts with "%s" then
          set active tab index of w to i
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
end if
return "no"'''),
    ("Safari", '''
if application "Safari" is running then
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        if URL of t starts with "%s" then
          set current tab of w to t
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
end if
return "no"'''),
]


def serve(cfg: dict, port: int = 8765, open_browser: bool = True,
          host: str | None = None) -> None:
    host = host or str(cfg.get("server", {}).get("host", "127.0.0.1"))
    state.phone_load()
    threading.Thread(target=state.state_loop, args=(cfg,), daemon=True).start()
    handler = type("Handler", (_Handler,), {"cfg": cfg})
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        # Deux instances en collision, deux fois dans la meme journee. Le vrai degat
        # n'est pas l'echec : c'est qu'il est SILENCIEUX pour qui regarde ailleurs. La
        # seconde instance meurt, la premiere continue de servir la page, et si c'est
        # elle qui est perimee on cherche le probleme partout sauf la. Dire QUI tient le
        # port transforme une trace de pile en une ligne actionnable.
        tenant = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                capture_output=True, text=True).stdout.strip().splitlines()
        print(f"✖ le port {port} est deja pris — cette instance s'arrete ({exc})")
        for ligne in tenant[1:]:
            print(f"    tenu par : {ligne}")
        print("    → arrete l'autre instance avant de relancer celle-ci")
        raise SystemExit(1)
    # Le navigateur local passe toujours par la boucle locale, même quand on écoute plus
    # large : c'est l'adresse qui marche à coup sûr, y compris hors réseau.
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard rig → {url}  (Ctrl-C pour arrêter)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Ouvert au réseau : dire OÙ, sinon il faut aller chercher son IP à la main pour
        # configurer le téléphone. Et dire ce que ça implique — il n'y a pas de mot de
        # passe, quiconque est sur ce réseau peut déclencher les actions du dashboard.
        name = _bonjour_name()
        for label, u in ((".local", f"http://{name}:{port}/"),
                         ("IP    ", f"http://{_lan_ip()}:{port}/" if _lan_ip() else None)):
            if u:
                print(f"  réseau local ({label}) → {u}")
        print("  ⚠ ouvert au réseau local, sans authentification — réseau de confiance "
              "uniquement (le dashboard lance et ferme des apps).")
    # Le retour de la touche du rig : elle affiche un verdict, elle doit pouvoir en
    # montrer le détail. Sans ça, lire « 2 · NB » oblige à revenir au clavier — le geste
    # que la touche existait justement pour éviter.
    def _montrer() -> None:
        # Chercher l'onglet AVANT d'en ouvrir un. webbrowser.open() en crée un nouveau à
        # chaque appel : appuyer trois fois sur la touche laissait trois onglets du même
        # tableau de bord, et sur scène on appuie plutôt deux fois qu'une. Le navigateur
        # n'est interrogé que s'il tourne déjà — le réveiller pour chercher un onglet
        # qu'il n'a pas serait exactement le contraire du but.
        for nav, script in _RETROUVER_ONGLET:
            try:
                r = subprocess.run(["osascript", "-e", script % url],
                                   capture_output=True, text=True, timeout=5)
                if r.stdout.strip() == "ok":
                    print(f"[jauge]  (retour : onglet {nav} remis au premier plan)", flush=True)
                    return
                if r.returncode != 0:
                    # Le cas courant, et il est INVISIBLE sans cette ligne : un service
                    # lancé par launchd n'a pas l'autorisation d'automatiser un
                    # navigateur tant qu'elle n'a pas été accordée. Le repli ouvre alors
                    # un onglet de plus à chaque appui, sans que rien n'en dise la cause.
                    # → Réglages Système → Confidentialité → Automatisation.
                    print(f"[jauge]  ({nav} injoignable : {r.stderr.strip()[:120]})", flush=True)
            except Exception as exc:
                print(f"[jauge]  ({nav} : {exc})", flush=True)
        print(f"[jauge]  (retour : ouverture de {url})", flush=True)
        webbrowser.open(url)

    alerts.listen_for_open(cfg, _montrer,
                           log=lambda m: print(f"[jauge]{m}", flush=True))
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard arrêté.")
    finally:
        httpd.server_close()


# --- the page (self-contained: inline CSS + JS, no external requests) ----------
