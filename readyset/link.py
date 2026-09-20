"""The rig's Ableton Link tempo, served by the dashboard (`/api/link`).

This module does NOT talk to Link. It drives `bin/linkbridge.py`, which is a separate
process — the reason is written at the top of that file, and it fits in one word:
**licence**. `aalink` embeds Ableton Link, which is GPL; the project has already decided
and written down that this code stays confined to an isolated process. Importing
`aalink` here would break that decision, not merely a style convention.

The pattern is the one of `spectrum.py` with ffmpeg, deliberately, so that there is only
one way of doing things in this repository: a subprocess producing a stream, a thread
reading it, one shared last state, a start on demand and an automatic shutdown when
nobody is watching any more.

THREE THINGS THAT READ BADLY IF THEY ARE NOT SPELLED OUT
=========================================================

1. **`peers == 0` is NOT a failure, and it is NOT a success either.** Link then answers
   a perfectly well-formed tempo — 120 by default — and a phase that keeps turning,
   without anything being synchronised with anything at all. Measured on 2026-08-23:
   Live 12 was running, and the bridge saw `peers=0` because Link was simply disabled in
   its settings. So we report `peers` as-is, together with `synced`, which frankly says
   whether there is anybody on the other side. **A key displaying "120" in that case
   would be lying**, and that would be our fault, not Link's.

2. **`age` matters as much as the values do.** The bridge can die (Link removed, `aalink`
   uninstalled, process killed) while leaving the last state in memory. Without the age,
   that corpse reads exactly like fresh data. Past `link_stale_seconds`, we say so:
   `fresh` turns false.

3. **Nobody samples the phase fast.** The phase is a deterministic function of the clock:
   the consumer recomputes it on its side from (`tempo`, `beat`, `age`). Polling this
   endpoint ten times a second would bring nothing more than once a second.

And the same guarantee as everywhere else: **nothing here must be able to bring the
dashboard down**. Any failure is turned into `available: false` and its reason in plain
words.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parent.parent / "bin" / "linkbridge.py"

# After a bridge that never produced a single tick (aalink missing, broken binary), we
# wait before retrying. Without that brake, every HTTP call relaunched a process that
# dies immediately — a Stream Deck key polling at 1 Hz would have launched 3600 of them
# per hour, and the caller would have read "starting up" without ever seeing the real
# reason, since a fresh bridge erases the previous one's error. Observed in testing.
_RETRY_AFTER_FAILURE = 5.0


class _Bridge:
    """The bridge subprocess + the thread reading it. One only, shared by every call."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.last: dict | None = None
        self.last_tick = 0.0
        self.error = ""
        self.last_read = 0.0
        # Reason of the last failed start + when. Deliberately survives `_open()`, which
        # resets `error` to zero: it is the only thing that makes it possible to answer
        # "aalink missing" rather than "starting up" forever.
        self.fatal = ""
        self.failed_at = 0.0
        self.ticks = 0

    def _open(self, quantum: float) -> bool:
        if not _BRIDGE.exists():
            self.error = f"pont introuvable : {_BRIDGE}"
            return False
        try:
            # `sys.executable` and not a frozen path: the bridge must run in THE
            # INTERPRETER THAT RUNS THE RIG, since that is where `aalink` is installed.
            # A `python3` from the PATH would be another interpreter, without the package.
            self.proc = subprocess.Popen(
                [sys.executable, str(_BRIDGE), str(quantum)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except Exception as exc:
            self.error = f"pont Link impossible à lancer : {exc}"
            return False
        self.error = ""
        return True

    def _pump(self, idle_stop: float) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue  # an unreadable tick is discarded, it does not kill the stream
                    with self.lock:
                        self.last, self.last_tick = data, time.time()
                    self.ticks += 1
                # Same choice as spectrum.py: the shutdown is decided in the reading
                # thread, not by a separate timer to keep alive and to stop.
                if time.time() - self.last_read > idle_stop:
                    break
        except Exception as exc:
            self.error = f"lecture du pont interrompue : {exc}"
        finally:
            self._collect_stderr()
            if self.ticks == 0:
                # The bridge died without ever producing anything: that is a start-up
                # failure, not a shutdown for inactivity. We keep the reason and forbid
                # ourselves from relaunching right away.
                self.fatal = self.error or "le pont Link s'est arrêté sans rien produire"
                self.failed_at = time.time()
            self.stop()

    def _collect_stderr(self) -> None:
        """Collect the reason written by the bridge — without it, a bridge that refuses
        to start reads like a mute bridge, which helps nobody."""
        p = self.proc
        if not p or not p.stderr:
            return
        try:
            tail = [l.strip() for l in p.stderr.read().splitlines() if l.strip()]
        except Exception:
            return
        # We keep only the last useful line: "aalink missing — install it…" is worth
        # more than three lines of nominal start-up.
        for l in reversed(tail):
            if not l.startswith("link bridge"):
                self.error = l
                return

    def start(self, quantum: float, idle_stop: float) -> bool:
        if self.thread and self.thread.is_alive():
            return True
        if self.fatal and time.time() - self.failed_at < _RETRY_AFTER_FAILURE:
            self.error = self.fatal
            return False
        self.ticks = 0
        if not self._open(quantum):
            return False
        self.last_read = time.time()
        self.thread = threading.Thread(target=self._pump, args=(idle_stop,), daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        with self.lock:
            # The last state DIES with the bridge: keeping it would pass a corpse off as
            # a reading, which is precisely the defect we want to avoid.
            self.last = None


_LINK = _Bridge()


def snapshot(cfg: dict) -> dict:
    """The current Link state. Never raises: at worst, `available` is false."""
    srv = cfg.get("server", {})
    quantum = float(srv.get("link_quantum", 4))
    idle_stop = float(srv.get("link_idle_stop_seconds", 20))
    stale_after = float(srv.get("link_stale_seconds", 2))

    _LINK.last_read = time.time()
    if not _LINK.start(quantum, idle_stop):
        return {"available": False, "reason": _LINK.error}

    with _LINK.lock:
        data, ts = _LINK.last, _LINK.last_tick
    if data is None:
        # The first call arrives before the bridge's first tick (~100 ms). We say so
        # rather than return zeros, which would read as a real tempo of 0.
        return {"available": False, "reason": _LINK.error or "pont Link en cours de démarrage"}

    age = time.time() - ts
    return {
        "available": True,
        "fresh": age <= stale_after,        # false = the bridge went quiet, do not trust the values
        "age": round(age, 2),
        "tempo": data.get("tempo"),
        "beat": data.get("beat"),
        "phase": data.get("phase"),
        "quantum": data.get("quantum", quantum),
        "peers": data.get("peers", 0),
        # `synced` is the question a key has to answer: is there anybody on the other
        # side? Without it we display Link's default tempo as if it came from Live.
        "synced": bool(data.get("peers", 0)),
        "playing": bool(data.get("playing", False)),
    }
