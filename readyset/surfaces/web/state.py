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

# The rig's state, recomputed CONTINUOUSLY by a background thread and served as-is.
#
# Before, each GET /api/state re-ran every single check: between 3 and 7 seconds of
# ioreg, pings and log reads — and the page and the menu bar each did it on their own
# side. Polling every second, as asked, would therefore have meant two full passes per
# second: the stage Mac's CPU goes up in smoke to refresh a table.
#
# Decoupled, the cost no longer depends on how many readers there are nor on how fast
# they poll: one pass every REFRESH_EVERY seconds whatever happens, and an instant read
# for everyone. Clients can then poll once a second at no cost at all.
_STATE: dict = {"data": None, "ts": 0.0}
REFRESH_EVERY = 2.0
_MODE = {"requested": None}    # None → use cfg default; else "auto"|"live"|"studio"
_MANUAL = {"iphone_charge": False}  # manual confirmations (things the Mac can't detect)

# The phone's latest report (POST /api/phone). The Mac CANNOT see whether the iPhone is
# charging: it charges on its own charger and only talks to the rig over Wi-Fi. Until
# now we therefore ticked a box by hand — a declaration, not a measurement, which stays
# ticked after the phone has been unplugged. The phone, on the other hand, knows the
# answer: it publishes it, and the check becomes a real observation, timestamped.
_PHONE: dict = {"ts": 0.0, "charging": None, "battery": None, "name": "", "source": "",
                # The latest readings [ts, battery, charging], for the SLOPE. A single
                # point says nothing: it is comparing two readings that debunks a
                # "charging" gone stale. Deliberately short — beyond a few hours
                # yesterday's slope says nothing about tonight's wall socket.
                "history": []}
_HISTORY_CAP = 24
# The report is read back at startup, and rewritten on every publication. The phone
# speaks on EVENT — plugged in, unplugged — not on a regular heartbeat: without this
# file, a restart of the dashboard (or of the Mac, an hour before playing) would erase a
# fact that is still true, and one would have to unplug then replug the phone just to
# teach it that fact again. `logs/` is the only local folder already excluded from git.
_PHONE_FILE = config.REPO_DIR / "logs" / "phone-report.json"


def phone_load() -> None:
    try:
        d = json.loads(_PHONE_FILE.read_text())
    except Exception:
        return                      # never published, file missing or unreadable: too bad
    for k in ("ts", "charging", "battery", "name", "source", "history"):
        if k in d:
            _PHONE[k] = d[k]


def _phone_save() -> None:
    try:
        _PHONE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PHONE_FILE.write_text(json.dumps(_PHONE))
    except Exception:
        pass                        # an unpersisted report is better than a 500

# A check's TYPE pictogram. It does not replace the thumbnails of `gear.icons_for` (the
# real macOS icon, the drawing of the device): those are served over HTTP and make no
# sense where no image can be loaded — the alarm panel is drawn in AppKit and showed,
# for want of anything better, only a "•" bullet on each of its lines.
# The exact key first, the prefix second: the Mac's power supply deserves its plug, and
# it lives in the same "sys:" as the VPN.
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


LOG_CAP = 5 * 1024 * 1024      # beyond that, we cut
LOG_KEEP = 256 * 1024          # what we keep: the end, the only useful part


def cfg_tidy_after(cfg: dict) -> bool:
    """Is the tidy-up at the end of the setup enabled? (`[windows] after_preflight`)"""
    return bool(cfg.get("windows", {}).get("after_preflight", True))


def _group_of(key: str) -> str:
    for prefix, name in _GROUP.items():
        if key.startswith(prefix):
            return name
    return "Autre"


def _soundcheck_result(cfg: dict, mode: str) -> checks.Result | None:
    """The soundcheck, reported as a full-blown CHECK in /api/state.

    It only lived in /api/midi, which only the dashboard queries. Consequence observed
    on 2026-08-18: the page announced "NOT ready — soundcheck unverified" while
    /api/state answered "ok, 0 blocking" — so the menu bar, the floating badge and the
    screen border, which all read /api/state, quietly went out. Exactly the sort of
    contradiction from one level to the next that the rest of the dashboard strives to
    avoid.

    A single place must say whether the rig is ready. Here it is, and everyone reads it.
    """
    sev = cfg["modes"].get(mode, {}).get("soundcheck_severity", "fail")
    if sev == checks.OFF:
        return None
    snap = _MON.snapshot()
    # One icon per gesture: "2 gestures not received yet" does not say WHICH ones, and
    # even when named, a list of words is read word by word. A foot, a keyboard, a breath
    # are recognised at a glance — which is what one asks of a line looked at between two
    # songs. The two universal gestures are here; those of the breath chain carry theirs
    # in rig.toml, next to their name.
    items = [{"name": "Pédale", "ok": bool(snap["flags"].get("pedal_cc64")), "icon": "🦶"},
             {"name": "Notes", "ok": snap["flags"].get("notes", 0) > 0, "icon": "🎹"}]
    items += [{"name": g["name"], "ok": bool(g.get("raw") and g.get("out")),
               "icon": g.get("icon") or ""} for g in snap.get("chain", [])]
    missing = [p["name"] for p in items if not p["ok"]]
    if not missing:
        return checks.Result("sc:play", "Soundcheck joué", checks.OK,
                             f"{len(items)} gestes vérifiés", parts=items)
    # `parts` carries the gestures one by one: the text below lists them for whoever has
    # only one line (the dashboard, a notification); the list serves whoever can unfold.
    return checks.Result("sc:play", "Soundcheck joué", sev,
                         checks._hint(f"{len(missing)} geste(s) pas encore reçu(s) : "
                                      + ", ".join(missing),
                                      "les jouer une fois — la vérification est passive, "
                                      "rien à cocher"),
                         parts=items)


def phone_record(charging=None, battery=None, name=None, source: str = "phone") -> None:
    """Record a reading, wherever it comes from — the phone publishing, or the Mac
    polling. A single store for both: that is what lets the slope be computed over
    points of mixed provenance without anything having to know about it.
    What is null is ignored, never overwritten — a partial reading must not erase what
    was already known.
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
    # The timestamp is set HERE, never taken from the message: a phone's clock is not to
    # be trusted, and what counts is freshness, not the date it claims.
    _PHONE["ts"] = time.time()
    if _PHONE["battery"] is not None:
        _PHONE["history"] = ([*_PHONE.get("history", []),
                              [_PHONE["ts"], _PHONE["battery"], _PHONE["charging"]]]
                             )[-_HISTORY_CAP:]
    _phone_save()


def _phone_trend(cfg: dict) -> dict | None:
    """What the battery is doing between the oldest reading of the window and the last.

    This is the flag's REBUTTAL: "charging" is a fact dated from the moment it was
    plugged in, that nothing rechecks afterwards; a cable that gives up or a power
    strip left off does not change it. A battery going backwards does.

    Two verdicts, and two different requirements:
      • `falling` — a single percent lost is enough, a charging battery does not go
        backwards. A few minutes apart, then, less than a soundcheck lasts.
      • `hours_left` — needs far more distance: to within 1 % over 3 minutes the slope
        is worth ±20 %/h. Below `trend_autonomy_span_seconds`, we note the drop without
        daring to put a number on it. A wrong estimate would be worse than none.

    We always compare against the OLDEST point of the window, never the previous one:
    the long lever arm is what makes the figure honest.
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
    """The phone's latest report, with its age and its freshness already decided.

    `fresh` is computed here and not at the caller: without it, each surface re-decides
    what "recent" means and two of them end up contradicting each other.
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
            # A check's thumbnails: the real macOS icon for an app, a drawing for
            # hardware (see readyset/devices/). A list, because a check can bear on TWO
            # objects — "Bome Network ↔ iPhone" shows both of them.
            "icons": icons_for(cfg, r.key, r.label),
            "remedy": rem.label if (rem and r.status != checks.OK) else None,
            # Does the remedy only open the door? The surfaces that SUMMARISE what a
            # button is going to do need that nuance: without it, the menu bar counts
            # "Ouvrir le réglage Accessibilité" among what it knows how to fix.
            "manual": bool(rem and rem.hands_on and r.status != checks.OK),
        })
    # RELATED errors: which one explains which. Applied AFTER every item exists — a link
    # is judged on the whole chain's state, not check by check (readyset/core/cascade.py).
    cascade.annotate(cfg, mode, items)
    status = checks.worst(results)
    return {
        "status": status,
        "mode": mode,
        "requested": requested,
        "fails": sum(1 for r in results if r.status == checks.FAIL),
        "warns": sum(1 for r in results if r.status == checks.WARN),
        # Counted separately: optional gear that is absent, never a problem. The menubar
        # and the badge ignore it, but the dashboard shows it so one knows the check did
        # run and was not forgotten.
        "infos": sum(1 for r in results if r.status == checks.INFO),
        "total": len(results),
        "items": items,
        # Served apart from the `items` so the quit panel has the exact path of each app
        # (the items only carry a label). Almost free: the list comes from the memo of
        # readyset/apps, already filled in by the check.
        "unexpected": apps.unexpected(cfg),
    }


# One beat every 60 s, on top of every change. Two distinct reasons: MIDI does not
# acknowledge, so a surface switched on after the dashboard — the usual order on stage —
# would never have received anything; and a regular stream is what lets a third party
# conclude something from SILENCE. The key itself cannot do that: a trevligaspel script
# reacts to a message that arrives, nothing fires when nothing arrives any more. The
# beat makes monitoring POSSIBLE, it does not do the monitoring.
# In SECONDS, measured — not in a number of loops. Counting loops assumed that a loop
# lasts REFRESH_EVERY; it lasts REFRESH_EVERY plus the time of the checks, and the audio
# probe or a network host that does not answer stretch it without warning. The beat
# therefore drifted silently, which is exactly what a beat must never do.
#
# Fifteen seconds, not sixty. The cadence is NOT there to see the changes — those leave
# the instant they happen. It fixes two things: how long it takes a surface switched on
# late to catch its display up, and after how much silence a watcher can conclude that
# nobody is emitting any more. A minute made that verdict slow when speeding it up costs
# nothing: eleven three-byte messages on a port that goes nowhere.
_GAUGE_BEAT_S = 15.0


def _rotate_log(path: Path) -> None:
    """Cut the log when it goes past LOG_CAP, keeping its END.

    Truncating to zero would be simpler, but we would throw away precisely the trace of
    what has just gone wrong — it is almost always the end that one reads. So we rewrite
    the last LOG_KEEP bytes, restarting at a line boundary so as not to leave half a
    line at the top.

    Written IN PLACE (`r+`) rather than renamed: launchd holds the descriptor open in
    append mode, and renaming the file under its feet would make it write into a file
    that no longer has a name — the log would become invisible until the next restart
    of the service.
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
    """Recompute the state endlessly, at a fixed cadence, whatever the clients do."""
    # The docstring used to sit BELOW this call, where Python treats it as a dead string
    # expression rather than a docstring: `help()` and every doc tool saw nothing.
    _rotate_log(config.REPO_DIR / "logs" / "dashboard.log")
    ticks = 0
    # Publishing is not the same gesture as alerting: we force the "midi" backend alone
    # rather than the [monitor].alerts list, otherwise the dashboard would duplicate the
    # macOS notifications and the pushes of "readyset monitor".
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
        except Exception as exc:                  # a check that raises must not kill the
            print(f"[state] {type(exc).__name__}: {exc}", flush=True)   # whole loop
        ticks += 1
        # The heartbeat the phone cannot produce: it is the Mac that asks, at a fixed
        # cadence, as long as the device is paired. Silent if it is not — `read` returns
        # None and we keep what the phone published on its own side.
        every = float(cfg.get("server", {}).get("idevice_poll_seconds", 60))
        if every > 0 and ticks % max(1, int(every / REFRESH_EVERY)) == 0:
            got = idevice.read(cfg)
            if got:
                phone_record(charging=got["charging"], battery=got["battery"],
                             source=f"mac:{got['via']}")
        time.sleep(REFRESH_EVERY)
