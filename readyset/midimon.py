"""Live MIDI monitor for the soundcheck / play-test.

Opens EVERY MIDI input passively (CoreMIDI fans a source out to all clients, so
this neither steals nor injects anything) and, in a background thread, records
what arrives. The soundcheck page polls a snapshot so checkmarks light up as you
play: sustain pedal (CC64), notes, breath, and per-port activity for the
controllers. The sound itself is not tracked: software cannot hear it, and asking
for a checkbox added a step for something you already know by listening.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

import mido

from .midi_lock import MIDI_LOCK

# Virtual routing / loopback / network ports carry no physical playing and — worse —
# opening the full set of ~17 CoreMIDI clients at once wedges rtmidi. We open only
# the physical controller inputs, so the soundcheck sees real gear and stays stable.
_EXCLUDE = ("loopback", "daw2", "2daw", "mackie", "xr18", "from max", "to max",
            "mpe", "netdevices", "réseau", "reseau", "session", "network", "rtp")
_MAX_PORTS = 12   # safety cap even after filtering

# Named CCs so the ShowMIDI-style view reads "Sustain=127" not "CC64=127".
CC_NAMES = {1: "Mod", 2: "Breath", 4: "Foot", 5: "Porta", 7: "Volume", 10: "Pan",
            11: "Expr.", 64: "Sustain", 65: "Porta", 66: "Sostenuto", 67: "Soft",
            71: "Reso", 74: "Cutoff", 91: "Reverb", 93: "Chorus",
            120: "All Off", 121: "Reset", 123: "Notes Off"}


def _physical_inputs(aussi_exclus: tuple[str, ...] = ()) -> list[str]:
    """The inputs where somebody really PLAYS.

    `aussi_exclus` receives the ports the config designates as non-instruments — the
    rig's alert port first and foremost. It carries no gesture: it carries what the rig
    tells itself, eleven messages every fifteen seconds ever since the gauge exists.
    Leaving it here drowned the soundcheck under traffic no finger ever produced, and its
    name contains none of the _EXCLUDE words — excluding it by keyword would have been a
    bet on its spelling, whereas the config already names it.
    """
    with MIDI_LOCK:
        names = sorted(set(mido.get_input_names()))
    mots = _EXCLUDE + tuple(x.lower() for x in aussi_exclus if x)
    keep = [n for n in names if not any(x in n.lower() for x in mots)]
    return keep[:_MAX_PORTS]


def sleep_stamp() -> tuple[int, int]:
    """(boot, last wake) in seconds — the fingerprint of a "machine session".

    Both together, because neither one is enough: `kern.waketime` is 0 as long as the Mac
    has not slept since it was switched on (which is the case here, switched on on 08-17
    and kept awake by Amphetamine), so on its own it would not see a reboot;
    `kern.boottime` does not move on wake. The pair changes in both cases, which is what
    we want.

    Why it matters: a soundcheck proves that the keyboard, the pedal and the breath were
    answering AT THAT MOMENT. After a sleep, USB may have re-enumerated differently, a
    port may have vanished, a device may not have come back — that is even the rig's most
    common failure mode. A pre-sleep test therefore proves nothing any more, and still
    showing it green would be a reassuring lie.
    """
    def sec(name: str) -> int:
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r"sec = (\d+)", out)
            return int(m.group(1)) if m else 0
        except Exception:
            return 0
    return sec("kern.boottime"), sec("kern.waketime")


class MidiMonitor:
    def __init__(self):
        # The breath chain, described in rig.toml. Set by the server through
        # configure(): the monitor is built before the config is read.
        self.chain: dict = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reset()

    def _reset(self) -> None:
        with self._lock:
            self.ports: dict[str, dict] = {}     # name -> {count, last, ts}
            self.events: list[dict] = []         # most-recent-first, capped
            self.flags = {"pedal_cc64": None, "notes": 0, "breath": None}
            # Head movements of the breath controller. The TEControl sends the breath on
            # CC2/CC11 and its tilt sensors on OTHER CCs, whose numbers are configurable
            # in its editor — impossible to hard-code without lying. So we learn them:
            # any non-breath CC coming from the "breath" port is an axis, its FIRST value
            # is the rest position, and we remember how far it departs from it on both
            # sides. One axis = two directions (below rest / above), which gives exactly
            # the four tilts asked for.
            self.motion: dict[int, dict] = {}   # cc -> {rest, min, max, count}
            self.stamp = sleep_stamp()          # the machine session of THESE results
            # Per gesture: seen at the SOURCE (the sensor moves) and seen at the OUTPUT
            # (Bome translated). The two kept apart, because the gap between them IS the
            # diagnosis — mute sensor, or mute translator.
            self.chain_seen: dict[str, dict] = {}
            self.state: dict[str, dict] = {}     # "port|ch" -> live per-channel state
            self._watched: list[str] = []        # physical ports actually opened
            self.started = time.time()

    def configure(self, cfg: dict) -> None:
        """Give the monitor the breath chain to watch (see rig.toml)."""
        self.chain = dict(cfg.get("checks", {}).get("breath_chain", {}) or {})
        # The alert port is not an instrument: we remove it from the listened-to list.
        self.non_instruments = (str(cfg.get("alerts", {}).get("midi", {}).get("port", "")),)

    def _chain_ports(self) -> list[str]:
        """Bome's OUTPUT port, which has to be listened to on top of the controllers.

        It is discarded by `_EXCLUDE` (it is a "loopback") and that is right for the list
        of physical controllers — nobody plays on it. But that is where Bome writes what
        Ableton will receive, so it is the only place where one can observe that the
        TRANSLATION did happen, and not merely that the sensor moves. A disabled Bome
        preset shows up exactly there: the gesture leaves, nothing arrives. So we add it
        by name, without opening the floodgate of the 17 ports again.
        """
        want = str(self.chain.get("out_port", "")).lower()
        if not want:
            return []
        with MIDI_LOCK:
            names = list(mido.get_input_names())
        return [n for n in names if isinstance(n, str) and want in n.lower()]

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start listening WITHOUT erasing what has already been observed.

        The `_reset()` that used to live here came from a time when one clicked "Start":
        a new test, a blank page. Ever since the listening switches itself on and off —
        it runs as long as something is missing, it stops when everything has arrived —
        that same reset wipes the work already done as soon as the monitor restarts for
        any reason whatsoever (stopped thread, a stop/restart close together). That is
        the defect reported on 2026-08-18: "note went green and disappeared, it should
        have stayed".

        One single thing invalidates a soundcheck, and that is the change of machine
        session (wake or reboot), handled in the loop and in snapshot(). A mere restart
        of the listening is not a hardware event: it proves nothing new, so it must
        destroy nothing.
        """
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- worker -----------------------------------------------------------
    _RESCAN_EVERY = 2.0   # seconds

    def _sync_ports(self, opened: list) -> list:
        """Open what has appeared, close what has vanished. Returns the up-to-date list.

        Without this rescan, the monitor saw ONLY the ports present at the second one
        pressed "Start listening". Yet the natural order on stage is the reverse: you
        open the soundcheck, THEN you plug the keyboard in — and there, with the pedal
        pressed down, nothing lights up, without the slightest error message. Reported
        on 2026-08-18: "press the sustain pedal, and when I press I get no feedback".
        The keyboard was indeed plugged in; it was simply that nobody was listening to it.

        Symmetrically, an unplugged port is closed again: keeping the object open on a
        device that has left makes `iter_pending()` raise in a loop.
        """
        want = set(_physical_inputs(getattr(self, 'non_instruments', ()))) | set(self._chain_ports())
        have = {p.name for p in opened}
        if want == have:
            return opened
        keep = []
        with MIDI_LOCK:
            for p in opened:                  # what has vanished from CoreMIDI
                if p.name in want:
                    keep.append(p)
                    continue
                try:
                    p.close()
                except Exception:
                    pass
            for name in want - have:          # what has just arrived
                try:
                    keep.append(mido.open_input(name))
                except Exception:
                    pass  # port held exclusively elsewhere — we will retry next round
        with self._lock:
            self._watched = sorted(p.name for p in keep)
        return keep

    def _run(self) -> None:
        opened: list = []
        next_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now >= next_scan:
                    # Wake or reboot → the previous results are worth nothing any more.
                    # We start again from a blank page rather than keeping green boxes
                    # that speak of a hardware state which may no longer exist.
                    if sleep_stamp() != self.stamp:
                        self._reset()
                    opened = self._sync_ports(opened)
                    next_scan = now + self._RESCAN_EVERY
                for port in opened:           # steady-state polling: no lock needed
                    try:
                        for msg in port.iter_pending():
                            self._record(port.name, msg)
                    except Exception:
                        # Device yanked out mid-read: the next rescan, in 2 s at most,
                        # will remove it cleanly. We do not kill the thread over an
                        # unplugged cable.
                        pass
                time.sleep(0.01)
        finally:
            with MIDI_LOCK:
                for p in opened:
                    try:
                        p.close()
                    except Exception:
                        pass

    def _match_chain(self, port: str, msg) -> None:
        """File a message into the breath chain, at the source or at the output."""
        ch = self.chain
        if not ch:
            return
        src, out = str(ch.get("source_port", "")).lower(), str(ch.get("out_port", "")).lower()
        low = port.lower()
        # Bome writes `Channel num="1"` = channel 2 in human numbering; mido returns a
        # 0-indexed channel. Hence the -1, and not an oversight.
        want_ch = int(ch.get("out_channel", 2)) - 1
        for g in ch.get("gestures", []):
            e = self.chain_seen.setdefault(g["name"], {"raw": False, "out": False})
            if src and src in low and msg.type == "control_change" and msg.control == g.get("raw_cc"):
                e["raw"] = True
            if out and out in low and getattr(msg, "channel", None) == want_ch:
                kind = g.get("out")
                if ((kind == "cc" and msg.type == "control_change" and msg.control == g.get("out_cc"))
                        or (kind == "pressure" and msg.type == "aftertouch")
                        or (kind == "pitchbend" and msg.type == "pitchwheel")):
                    e["out"] = True

    def _record(self, port: str, msg) -> None:
        desc = self._desc(msg)
        self._match_chain(port, msg)
        with self._lock:
            e = self.ports.setdefault(port, {"count": 0, "last": "", "ts": 0.0})
            e["count"] += 1
            e["last"] = desc
            e["ts"] = time.time()
            self.events.insert(0, {"port": port, "msg": desc})
            del self.events[60:]
            if msg.type == "control_change" and msg.control == 64:
                self.flags["pedal_cc64"] = {"port": port, "value": msg.value}
            if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
                self.flags["notes"] += 1
            # Breath: TEControl sends CC2 (breath) / CC11 (expression); also treat
            # any traffic on a port whose name mentions "breath" as the breath ctrl.
            if (msg.type == "control_change" and msg.control in (2, 11)) or "breath" in port.lower():
                self.flags["breath"] = {"port": port}
            if (msg.type == "control_change" and msg.control not in (2, 11)
                    and "breath" in port.lower()):
                m = self.motion.setdefault(msg.control, {"rest": msg.value,
                                                         "min": msg.value,
                                                         "max": msg.value, "count": 0})
                m["count"] += 1
                m["min"] = min(m["min"], msg.value)
                m["max"] = max(m["max"], msg.value)

            # Per-channel live state for the ShowMIDI-style view.
            ch = getattr(msg, "channel", None)
            if ch is not None:
                st = self.state.setdefault(f"{port}|{ch}", {
                    "port": port, "ch": ch + 1, "notes": {}, "cc": {},
                    "pitch": None, "prog": None, "at": None, "ts": 0.0})
                st["ts"] = e["ts"]
                if msg.type == "note_on" and msg.velocity > 0:
                    st["notes"][msg.note] = msg.velocity
                elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                    st["notes"].pop(msg.note, None)
                elif msg.type == "control_change":
                    st["cc"][msg.control] = msg.value
                elif msg.type == "pitchwheel":
                    st["pitch"] = msg.pitch
                elif msg.type == "program_change":
                    st["prog"] = msg.program
                elif msg.type == "aftertouch":
                    st["at"] = msg.value

    @staticmethod
    def _desc(msg) -> str:
        if msg.type in ("note_on", "note_off"):
            return f"{msg.type} {msg.note} v{getattr(msg, 'velocity', 0)}"
        if msg.type == "control_change":
            return f"CC{msg.control}={msg.value}"
        if msg.type in ("pitchwheel",):
            return f"pitch {msg.pitch}"
        return msg.type

    # Minimum deviation, out of 127, to tell a deliberate tilt from a tremble.
    _TILT_MIN = 15

    def _head(self) -> dict:
        """The four tilts, deduced from the learned axes. To be called under the lock.

        The axes are taken in the order in which they showed up: the first becomes
        gauche/droite (left/right), the second haut/bas (up/down). That order is a
        CONVENTION, not a measurement — nothing in MIDI says which is which. If they come
        out swapped on screen, it is the two labels that must be exchanged, not the sensor.
        """
        axes = sorted(self.motion.items(), key=lambda kv: -kv[1]["count"])[:2]
        names = [("gauche", "droite"), ("haut", "bas")]
        out = {}
        for (cc, m), (low, high) in zip(axes, names):
            out[low] = {"cc": cc, "hit": m["rest"] - m["min"] >= self._TILT_MIN,
                        "amp": m["rest"] - m["min"]}
            out[high] = {"cc": cc, "hit": m["max"] - m["rest"] >= self._TILT_MIN,
                         "amp": m["max"] - m["rest"]}
        return out

    _STAMP_TTL = 5.0
    _stamp_checked = 0.0

    def snapshot(self) -> dict:
        # The wake check lives HERE as well, not only in the loop: once the soundcheck is
        # complete, the monitor is stopped (no point holding the MIDI ports open for
        # nothing) — and a stopped monitor watches nothing any more. So it is the state
        # query that must take care of it, otherwise a wake would go unnoticed and leave
        # stale green boxes behind. The fingerprint is re-read every 5 s at most: two
        # sysctl calls on every poll (every 300 ms) would be a waste.
        now = time.time()
        if now - self._stamp_checked > self._STAMP_TTL:
            self._stamp_checked = now
            if sleep_stamp() != self.stamp:
                self._reset()
        with self._lock:
            ports = [{"name": n, **v} for n, v in
                     sorted(self.ports.items(), key=lambda kv: -kv[1]["ts"])]
            channels = []
            for st in sorted(self.state.values(), key=lambda s: -s["ts"]):
                channels.append({
                    "port": st["port"], "ch": st["ch"],
                    "notes": [{"n": n, "v": v} for n, v in sorted(st["notes"].items())],
                    "cc": [{"n": n, "v": v, "name": CC_NAMES.get(n, "")}
                           for n, v in sorted(st["cc"].items())],
                    "pitch": st["pitch"], "prog": st["prog"], "at": st["at"],
                })
            return {
                "running": self.is_running(),
                "watched": list(self._watched),
                "ports": ports,
                "channels": channels,
                "flags": dict(self.flags),
                "head": self._head(),
                "stamp": list(self.stamp),
                "chain": [{"name": g["name"], "icon": g.get("icon", ""),
                           **self.chain_seen.get(g["name"], {"raw": False, "out": False})}
                          for g in self.chain.get("gestures", [])],
                "events": self.events[:36],
            }
