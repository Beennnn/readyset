"""The web surface — the local dashboard, and the state store behind it.

  state  the rig's state, recomputed in the background and read instantly
  http   the routes that serve it
  page.html  the page itself, a real file rather than a string inside a .py
"""

from __future__ import annotations

from .http import PAGE, serve, set_requested_mode  # noqa: F401
from . import state  # noqa: F401
