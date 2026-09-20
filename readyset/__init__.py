"""readyset — bring-up + live monitoring for a stage keyboard rig.

`rig` is the physical installation (keyboards, cables, the DAW, the deck).
`readyset` is this program. The two words never swap.

FOUR LAYERS, and the dependency arrow only ever points inwards:

  core/       config, cascade, and the Result type — depends on nothing
  checks/     one family per file: apps · audio · midi · network · phone · gear
  fix/        for a red check, the action that brings it back
  surfaces/   every place the state gets shown: web · menubar · alerts
  devices/    a CATALOGUE of hardware, as data — no model name lives in code

Alongside them, the macOS adapters the layers share: apps (the app inventory),
windows, vpn, midimon, spectrum, liveaudio, audiolevel, idevice, link. They are
neither checks nor fixes — they are how this program talks to the machine.
"""
