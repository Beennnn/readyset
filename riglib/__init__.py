"""riglib — bring-up + live monitoring for the stage keyboard rig.

One concern per module (per the repo's file-hygiene rule):
  config.py  — load rig.toml (+ example fallback + baked defaults)
  checks.py  — health checks (apps running, MIDI ports, audio interface)
  launch.py  — the bring-up sequence (launch apps in order, open the set)
  alerts.py  — alert backends (macOS notification, phone push, Stream Deck)
"""
