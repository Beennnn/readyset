"""The Result type — what every check returns, and the four levels it can return.

Lives in core/ rather than in checks/ because three different worlds depend on it and
none of them should have to import the checks to speak about their outcome: the CLI
prints Results, the web dashboard serialises them, and cascade.py reasons about them.

The checks themselves are in readyset/checks/, one family per file.
"""

from __future__ import annotations

from dataclasses import dataclass


# Four levels, from the calmest to the gravest. INFO sits BELOW the warning: it says
# « absent, and that is normal » — an optional piece of gear that simply was not plugged
# in tonight (ambiance lamps, the audio interface at the desk). It is the level that was
# missing: putting everything at WARN made things nobody cares about blink orange, and in
# the end you stop reading the oranges at all — including the real ones.
# An INFO check does NOT make the rig « not ready »: `worst()` ignores it, the exit code
# stays 0, and neither the dot nor the edge strip lights up.
OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
_ICON = {OK: "✅", INFO: "ℹ️ ", WARN: "⚠️ ", FAIL: "❌"}

# Special value accepted everywhere a severity is set (breath_severity,
# interface_severity, network_severity, lamp_severity, [[checks.audio_devices]].severity…):
# the check does NOT run and appears nowhere at all in that mode.
#
# Not to be confused with INFO. "info" says « absent, and that is normal » — the line is
# still displayed, so you know the question was asked. "off" says « the question makes no
# sense here »: on stage the sound comes out of the stage keyboard, the desk interface is
# not even in the bag, so showing « desk interface not detected » every night teaches
# nobody anything and lengthens the list to read before playing. Use it for that and
# nothing else — hiding a real problem behind "off" is blinding yourself.
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

    # The contents of a COMPOSITE check: its sub-items, each one seen or not seen yet.
    # A check stays ONE line — that is what keeps the list readable at a glance — but it
    # then enumerated its missing items inside its own text, where any narrow surface
    # truncates them (« … : Pédale, Notes, Souff… »). Detailed here, the surfaces that have
    # the room unfold them instead of cutting them. None when there is nothing to detail.
    # Each one: {"name": str, "ok": bool, "icon": str} — the icon illustrates the GESTURE to
    # perform, and is read before the word; it is optional, a bare name stays fully readable.
    parts: list[dict] | None = None

    def to_dict(self) -> dict:
        d = {"key": self.key, "label": self.label,
             "status": self.status, "detail": self.detail}
        if self.parts is not None:
            d["parts"] = [{"name": p["name"], "ok": bool(p.get("ok")),
                           "icon": p.get("icon") or ""} for p in self.parts]
        return d


def _ago(seconds: float | None) -> str:
    """« il y a 12 s » / « il y a 4 min » — the age of an observation, spelled out in words.

    A raw timestamp forces you to do the subtraction in your head, right before playing.
    """
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:                       # past an hour and a half, « 120 min » forces
        return f"{int(seconds // 60)} min"   # you to divide in your head to place it
    return f"{seconds / 3600:.0f} h"


def _hint(observed: str, advice: str) -> str:
    """Glue a piece of advice onto the observation: « what I see → what you can do ».

    A red check with no advice forces you to remember the gesture — and on stage, five
    minutes before playing, that is exactly what is missing. The observation stays in
    front (it is the part that is true), the advice follows. Reserved for hardware: when
    an automatic resolution exists, what is needed is a remedy.py button, not a
    sentence (nobody reads advice that a machine could have carried out).
    """
    # The line break is SEMANTIC, not decorative: every display renders it in its own
    # way. The dashboard is in `white-space: pre-line` and puts the advice on its own
    # line — before that, a `nowrap` + ellipsis cut the advice off mid-sentence, which
    # is worse than no advice at all. The terminal glues them back and wraps on its own.
    return f"{observed}\n→ {advice}"


def worst(results: list[Result]) -> str:
    """Worst level of the batch. INFO NEVER appears here: it is a per-line annotation,
    not a state of the rig — a rig whose optional gear is all unplugged is still « ready »."""
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status == WARN for r in results):
        return WARN
    return OK
