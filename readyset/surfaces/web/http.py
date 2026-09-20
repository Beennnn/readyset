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
            if snap is None:                       # very first call: we compute it
                snap = build_state(self.cfg)
                _STATE["data"], _STATE["ts"] = snap, time.time()
            self._json({**snap, "age": round(time.time() - _STATE["ts"], 1)})
        elif self.path.startswith("/api/mode"):
            self._json({"requested": _MODE["requested"] or self.cfg.get("mode", {}).get("default", "auto"),
                        "resolved": checks.resolve_mode(self.cfg, _MODE["requested"] or self.cfg.get("mode", {}).get("default", "auto"))})
        elif self.path.startswith("/api/gear"):
            # Drawings served inline: no file, no network — the dashboard has to stay
            # whole on a stage network with no Internet.
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
            # The app's real icon rather than a generic emoji: in a list where one is
            # about to CLOSE things, WhatsApp is recognised by its logo well before its
            # name has been read. The path is revalidated against the apps that are
            # actually running — the server does not open an arbitrary file on request.
            want = (parse_qs(urlparse(self.path).query).get("path") or [""])[0].rstrip("/")
            # The RUNNING apps are not enough: Bome Network runs in the background and
            # never shows up in the list of apps with a screen presence. So we add the
            # apps the rig launches itself, and those gear.py resolved for the checks.
            # The allowlist stays closed — an arbitrary path is refused.
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
            # An app's icon does not change; without this the browser re-downloads it on
            # every refresh (every 4 s).
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(png)
        elif self.path.startswith("/api/phone"):
            self._json(phone_snapshot(self.cfg))
        elif self.path.startswith("/api/audio/spectrum"):
            # Outside build_state, deliberately: a Stream Deck key refreshes about ten
            # times a second, while the full state is recomputed every 4 s. Mixing them
            # would make every state pass pay for an audio capture — and a spectrum 4 s
            # old would look like nothing at all.
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
            # Published by the phone itself (an iOS shortcut, an automation), not by the
            # dashboard. Everything is optional: what is missing stays unknown rather
            # than being assumed false. The timestamp, though, is set HERE — the phone's
            # clock is not to be trusted, and what counts is freshness.
            phone_record(charging=body.get("charging"), battery=body.get("battery"),
                         name=body.get("name"), source="phone")
            self._json({"ok": True, **phone_snapshot(self.cfg)})
        elif self.path == "/api/audiolevel":
            # Signal measurement on demand — never in the refresh loop: each call creates
            # a native CoreAudio tap of about 1.5 s. See audiolevel/'s README for the two
            # macOS pitfalls this path works around.
            self._json(audiolevel.measure(float(body.get("seconds", 1.5)),
                                          str(body.get("target", "ableton"))))
        elif self.path == "/api/midi/start":
            _MON.configure(self.cfg)   # the breath chain comes from rig.toml
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
            # The confirmation is on the page side (ticked box + dialog); here we still
            # revalidate the selection against the list of "surplus" apps computed by
            # the server — see apps.quit_many.
            ok, msg = apps.quit_many(self.cfg, body.get("paths") or [], dry_run=dry)
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/windows":
            # `all` = tidy Ableton away too (its policy is "keep" the rest of the time).
            ok, msg = windows.tidy(self.cfg, log=lambda _: None, dry_run=dry,
                                   force=bool(body.get("all", False)))
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/preflight":
            # ONE action, that does everything. "Préflight", "Tout corriger" and "Ranger
            # les fenêtres" were three buttons and nobody could say which one did what —
            # the rig's owner, 2026-08-18: "we don't understand what they do, in the end
            # we'd want a single magic action that does everything". Here it is, in the
            # order in which things have to happen: first bring the rig up, then repair
            # what is still wrong, finally re-check to deliver an up-to-date verdict.
            logs: list[str] = []
            launch.bring_up(self.cfg, log=logs.append, dry_run=dry)

            # The fixes come AFTER the launch: most of the reds from before (apps not
            # running, set not opened) disappear on their own, and repairing what no
            # longer exists would make no sense at all.
            mode = checks.resolve_mode(self.cfg, _MODE["requested"]
                                       or self.cfg.get("mode", {}).get("default", "auto"))
            todo = [r for r in checks.run_all(self.cfg, mode, manual=_MANUAL)
                    if r.status in (checks.FAIL, checks.WARN)]
            # Counting ATTEMPTS as repairs was this action's worst flaw: on 2026-08-22,
            # every UI fix was refused by macOS (missing accessibility permission) and
            # the setup nevertheless answered "🔧 2 correctif(s) appliqué(s)", ok: true,
            # in green. Ableton was running on "No Device" at the time — complete
            # silence, announced as a success. A report that errs in THAT direction is
            # worse than no report at all: it stops you going to look.
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
            # The verdict goes FIRST: it is the first line read, and often the only one.
            if failed:
                head = [f"⛔ {len(failed)} correctif(s) N'ONT PAS pu être appliqués :"]
                head += [f"   • {f}" for f in failed]
                head.append("")
                logs[:0] = head

            # WE TIDY LAST, not in the middle (2026-08-22). `bring_up` ends with
            # tidy_windows, but the fixes run AFTER it: "Ouvrir le set" relaunches
            # Ableton, "Relancer Bome" reopens a window — all of them arriving too late
            # for the tidy-up that preceded them. And an app that has just started often
            # creates its window after the 2 s settle: Bome Network ended up VISIBLE and
            # not minimised at the end of a setup, even though its policy says
            # "minimize". A second pass is idempotent — it only costs a handful of
            # milliseconds when there is nothing to tidy away.
            if not dry and cfg_tidy_after(self.cfg):
                time.sleep(1.0)   # gives late windows the time to exist
                windows.tidy(self.cfg, log=logs.append, dry_run=False)

            # Closing the surplus apps is NOT here: it can lose an unsaved document, so
            # it keeps its own confirmation naming every app. A "magic" action must never
            # destroy anything without one having seen it coming.
            self._json({"ok": not failed, "message": "\n".join(logs)})
        else:
            self._json({"ok": False, "message": "route inconnue"}, code=404)


def _lan_ip() -> str | None:
    """This machine's IPv4 address on its network, without emitting anything.

    A "connected" UDP socket only picks a route: no packet leaves. The address aimed at
    is in TEST-NET-1 (192.0.2.0/24, RFC 5737), which is routed nowhere at all — it is
    impossible to reach anything by accident.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))
            return s.getsockname()[0]
    except Exception:
        return None


def _bonjour_name() -> str:
    """The .local name under which the other machines find this Mac.

    Pitfall: `socket.gethostname()` returns the SHELL name — say "my-macbook", which
    only resolves on this machine. Bonjour publishes the LocalHostName, a third name
    (macOS holds THREE: ComputerName, HostName, LocalHostName) — say
    "My-MacBook.local", the only one the phone can reach. Printing the other one
    amounted to handing out the one URL that does not work.
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


# One per browser, because their AppleScript dictionaries differ: Chrome selects a tab
# by its INDEX inside the window, Safari by the object itself.
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
        # Two instances colliding, twice in the same day. The real damage is not the
        # failure: it is that the failure is SILENT for anyone looking elsewhere. The
        # second instance dies, the first keeps serving the page, and if that one is the
        # stale one you look for the problem everywhere but there. Saying WHO holds the
        # port turns a stack trace into an actionable line.
        tenant = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                capture_output=True, text=True).stdout.strip().splitlines()
        print(f"✖ le port {port} est deja pris — cette instance s'arrete ({exc})")
        for ligne in tenant[1:]:
            print(f"    tenu par : {ligne}")
        print("    → arrete l'autre instance avant de relancer celle-ci")
        raise SystemExit(1)
    # The local browser always goes through the loopback, even when we listen wider:
    # that is the address that works for sure, off-network included.
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard rig → {url}  (Ctrl-C pour arrêter)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Open to the network: say WHERE, otherwise one has to go and fetch one's IP by
        # hand to configure the phone. And say what it implies — there is no password,
        # anyone on this network can trigger the dashboard's actions.
        name = _bonjour_name()
        for label, u in ((".local", f"http://{name}:{port}/"),
                         ("IP    ", f"http://{_lan_ip()}:{port}/" if _lan_ip() else None)):
            if u:
                print(f"  réseau local ({label}) → {u}")
        print("  ⚠ ouvert au réseau local, sans authentification — réseau de confiance "
              "uniquement (le dashboard lance et ferme des apps).")
    # The rig key's talk-back: it displays a verdict, so it must be able to show the
    # detail behind it. Without this, reading "2 · NB" forces a trip back to the
    # keyboard — the very gesture the key existed to avoid.
    def _montrer() -> None:
        # Look for the tab BEFORE opening one. webbrowser.open() creates a new one on
        # every call: pressing the key three times left three tabs of the same dashboard
        # behind, and on stage one presses twice rather than once. The browser is only
        # queried if it is already running — waking it up to look for a tab it does not
        # have would be exactly the opposite of the goal.
        for nav, script in _RETROUVER_ONGLET:
            try:
                r = subprocess.run(["osascript", "-e", script % url],
                                   capture_output=True, text=True, timeout=5)
                if r.stdout.strip() == "ok":
                    print(f"[jauge]  (retour : onglet {nav} remis au premier plan)", flush=True)
                    return
                if r.returncode != 0:
                    # The common case, and it is INVISIBLE without this line: a service
                    # started by launchd does not have permission to automate a browser
                    # until that permission has been granted. The fallback then opens one
                    # more tab on every press, with nothing saying why.
                    # → System Settings → Privacy → Automation.
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
