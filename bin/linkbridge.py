#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ableton Link bridge → one JSON line per tick on stdout.

WHY THIS FILE IS A SEPARATE PROCESS, AND NOT A MODULE OF `readyset`
===================================================================

**Licence.** `aalink` embeds [Ableton Link](https://github.com/Ableton/link), which is
**GPLv2-or-later** (dual licence: closed-source requires a commercial agreement with
Ableton). The project has already settled this point elsewhere and written it down: "GPL
(through aalink/Link) is confined to ONE process — `beatsync.py`" (README of
`~/dev/music/midi`). This file is the second occupant of that rule, not an exception to
it. The corollary matters most downstream: `readyset` itself carries a LICENSE and is
meant to be published — **never move this file up into it without handling the licence
first**, and never import `aalink` from `readyset/`.

**Event loop.** `aalink` mandates asyncio; the rig's server is a synchronous
`http.server` served by threads. Two concurrency models in the same process is a source
of deadlocks for zero gain.

**Life cycle.** A Link peer joining and leaving the session in a loop pollutes the
session of ALL the other peers, Live included. A dedicated process that is started and
stopped outright is a clean peer; a thread hooked onto the server's lifetime is not.

It is the same pattern as `readyset/spectrum.py` with ffmpeg: a subprocess producing a
stream, a thread reading it, one shared last state.

WHAT YOU NEED TO KNOW ABOUT LINK TO READ WHAT FOLLOWS
=====================================================

- `tempo` is the BPM of the **session**, not Live's: any peer can write it and everybody
  follows. This bridge **never writes it** — it watches, it does not lead. (`beatsync.py`
  does write; two writers would fight each other.)
- `beat` is a **continuous, monotonic** beat timeline that advances **even without any
  peer and even while stopped**. It is not the position inside Live's song.
- `phase` is the position within the bar, in `[0, quantum)`. It is what makes a tempo
  display readable while playing: without it we show a number that does not move.
- `peers` is the number of OTHER peers. **`peers == 0` is this module's trap**: Link then
  returns a perfectly well-formed tempo (120 by default) and a phase that keeps turning,
  while nothing is synchronised with anything. A display that does not tell that case
  apart is lying. We report it as-is and it is up to the consumer to handle it.
- `playing` is the shared transport (start/stop sync), an axis SEPARATE from the beat: a
  peer can be stopped while `beat` keeps advancing.

OUTPUT PROTOCOL
===============

One JSON line per tick on **stdout**, nothing else — the logs go to **stderr**, otherwise
they would mix into the stream the reader parses (a trap already met while testing a
home-made add-on). An unreadable tick is discarded without killing the stream.
"""

from __future__ import annotations

import asyncio
import json
import sys

# Emission rate. 10 Hz is far above what is needed: the consumer EXTRAPOLATES the phase
# locally from (tempo, beat, age) — the phase is a deterministic function of the clock,
# not a value that has to be sampled fast. What 10 Hz buys is the freshness of the tempo
# and of the peer count when they change.
TICK_SECONDS = 0.1

# Default quantum: 4 beats = one bar in 4/4. Link only guarantees phase alignment between
# peers of the SAME quantum, which is why it is configurable.
DEFAULT_QUANTUM = 4.0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _run(quantum: float) -> None:
    import aalink  # imported here so a missing package is a message, not a traceback

    # Without the `loop` parameter: aalink deprecates it and deduces it from the current loop.
    link = aalink.Link(120)
    link.quantum = quantum
    link.enabled = True
    _log(f"link bridge up (quantum={quantum})")
    try:
        while True:
            # Read on demand from the application thread: Link only requires an audio
            # callback for sample-accurate event placement, which is not our business
            # here.
            print(json.dumps({
                "tempo": round(link.tempo, 3),
                "beat": round(link.beat, 4),
                "phase": round(link.phase, 4),
                "quantum": link.quantum,
                "peers": link.num_peers,
                "playing": bool(link.playing),
            }), flush=True)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        # Leave the session cleanly rather than vanish: the other peers see the
        # departure right away instead of waiting for a timeout.
        link.enabled = False
        _log("link bridge down")


def main() -> int:
    quantum = DEFAULT_QUANTUM
    if len(sys.argv) > 1:
        try:
            quantum = float(sys.argv[1])
        except ValueError:
            _log(f"quantum illisible : {sys.argv[1]!r}")
            return 2
    try:
        asyncio.run(_run(quantum))
    except ModuleNotFoundError:
        # The most likely case, and it has a one-line answer — give it rather than leave
        # a Python traceback nobody will read in an add-on log.
        _log("aalink absent — installez-le : python3 -m pip install aalink")
        return 3
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — the bridge must never bring its parent down
        _log(f"pont Link interrompu : {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
