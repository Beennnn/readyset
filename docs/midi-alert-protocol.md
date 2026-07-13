# MIDI feedback alert protocol (`midi` backend)

Goal: light up a key on a control surface when a check fails — without the software
having to repaint the key (it can't, from outside the surface's own app). The trick is
**MIDI feedback**: the tool emits a MIDI note; a key configured to react to that note
lights up.

## Overview

```
  rig monitor ──MIDI note──►  virtual port  ──►  control-surface key (MIDI feedback)
   (emitter)                 (dead-end IAC)        (receiver → red / normal)
```

- **Emitter** — the `midi` alert backend (`riglib/alerts.py`), used by `rig monitor`
  and `rig alert-test`.
- **Transport** — a **dedicated virtual MIDI port** that nothing else in the rig reads,
  so an alert note can never trigger a sound or an action elsewhere.
- **Receiver** — a control-surface key configured for **incoming MIDI feedback** (any
  control surface / plugin that supports driving a key's state from incoming MIDI).

## Message spec

| Event | MIDI message | Channel | Note | Velocity | Raw bytes |
|---|---|---|---|---|---|
| **Alert** (a check went `fail`/`warn`) | Note On | 15 | 60 | 127 | `9E 3C 7F` |
| **Cleared** (check back to OK) | Note Off | 15 | 60 | 0 | `8E 3C 00` |

Channel/note/port are config (`[alerts.midi]`). The channel is kept off the musical
channels so the alert never collides with playing.

## Config

```toml
[alerts.midi]
port    = "rig-alert"    # a dedicated dead-end virtual MIDI port
channel = 15
note    = 60
```

## Setup (once)

1. **Create the port** — a dead-end virtual MIDI port (on macOS: Audio MIDI Setup →
   IAC Driver → "+" → name it `rig-alert`). It must be one **nothing else routes**, so
   an alert note goes nowhere harmful.
2. **Map a key** — on your control surface, add its MIDI action in **feedback / input**
   mode, listening for **Note On, note 60, channel 15** on that port; show a red image on
   note-on and a normal image on note-off. Any surface/plugin with MIDI-feedback support
   works.

## Test

```bash
./rig alert-test --alerts midi   # → the key should turn red
```

## Why this design

- Software cannot repaint a control-surface key from outside its app. Outgoing-only MIDI
  plugins (key → MIDI) can't help; **incoming** MIDI feedback is the honest inbound path.
- A **dedicated dead-end port** isolates the alert: every other MIDI port is likely wired
  into the live routing, where an injected note could trigger a sound or an action.
