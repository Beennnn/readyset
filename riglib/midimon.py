"""Live MIDI monitor for the soundcheck / play-test.

Opens EVERY MIDI input passively (CoreMIDI fans a source out to all clients, so
this neither steals nor injects anything) and, in a background thread, records
what arrives. The soundcheck page polls a snapshot so checkmarks light up as you
play: sustain pedal (CC64), notes, breath, and per-port activity for the
controllers. Sound itself can't be heard by software — that stays a human confirm.
"""

from __future__ import annotations

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


def _physical_inputs() -> list[str]:
    with MIDI_LOCK:
        names = sorted(set(mido.get_input_names()))
    keep = [n for n in names if not any(x in n.lower() for x in _EXCLUDE)]
    return keep[:_MAX_PORTS]


class MidiMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reset()

    def _reset(self) -> None:
        with self._lock:
            self.ports: dict[str, dict] = {}     # name -> {count, last, ts}
            self.events: list[dict] = []         # most-recent-first, capped
            self.flags = {"pedal_cc64": None, "notes": 0, "breath": None}
            self.state: dict[str, dict] = {}     # "port|ch" -> live per-channel state
            self.audio_ok: bool | None = None    # human confirm ("I hear sound")
            self._watched: list[str] = []        # physical ports actually opened
            self.started = time.time()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def confirm_audio(self, ok: bool) -> None:
        with self._lock:
            self.audio_ok = ok

    # -- worker -----------------------------------------------------------
    def _run(self) -> None:
        opened = []
        with MIDI_LOCK:                       # serialise the open burst vs check threads
            for name in _physical_inputs():
                try:
                    opened.append(mido.open_input(name))
                except Exception:
                    pass  # a port already exclusively held elsewhere — skip it
        with self._lock:
            self._watched = [p.name for p in opened]
        try:
            while not self._stop.is_set():
                for port in opened:           # steady-state polling: no lock needed
                    for msg in port.iter_pending():
                        self._record(port.name, msg)
                time.sleep(0.01)
        finally:
            with MIDI_LOCK:
                for p in opened:
                    try:
                        p.close()
                    except Exception:
                        pass

    def _record(self, port: str, msg) -> None:
        desc = self._desc(msg)
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

    def snapshot(self) -> dict:
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
                "audio_ok": self.audio_ok,
                "events": self.events[:36],
            }
