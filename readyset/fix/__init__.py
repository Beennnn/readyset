"""Fixes — for a red check, the action that brings it back.

  remedy  which key maps to which action, and whether that action really FIXES it
  launch  the bring-up sequence: apps in order, the set, then the scene

Every fix honours dry_run: it reports what it *would* do without doing it, so the
dashboard's dry-run toggle is real end to end.

WHAT IS NOT HERE. plugins/ ships small shell tools (audio-out, wifi-rejoin, lan-presence…)
that the README says checks and fixes "call by path". No code reads them today: there is
no [checks.fixes] executor. They are standalone and usable by hand; wiring them into this
package is open work, not an omission of this split.
"""

from __future__ import annotations

from .remedy import Remedy, resolve, resolve_key  # noqa: F401
from . import launch  # noqa: F401
