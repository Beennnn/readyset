"""Surfaces — every place the rig's state gets shown or announced.

  web      the local dashboard (HTTP + the page)
  menubar  the macOS menu-bar app, its floating badge and its alarm panel (Swift)
  alerts   macOS notifications, phone push, and the MIDI gauge a deck key reads

They all read the SAME state (surfaces/web/state.py). That is deliberate and it was
paid for: on 2026-08-18 the page said "not ready" while /api/state said "ok", so the
menu bar, the badge and the screen border quietly went green on a broken rig. One place
decides whether the rig is ready; everybody else reads it.
"""
