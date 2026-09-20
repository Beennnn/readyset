"""Core — what the rest of the program is built on, and what depends on nothing.

  config   reads rig.toml over the built-in defaults
  cascade  which red explains which other red
  result   the Result type and its four levels

Nothing here imports from checks/, fix/ or surfaces/: the dependency arrow only ever
points inwards. That is what lets a check, a fix and a display speak about the same
Result without any of them having to know the other two exist.
"""

from __future__ import annotations

from .result import OK, INFO, WARN, FAIL, OFF, Result, worst, _hint, _ago  # noqa: F401
