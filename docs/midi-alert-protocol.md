# MIDI feedback alert protocol (`midi` backend)

Goal: show on a control surface **how many checks are failing** — without the software
having to repaint the key (it can't, from outside the surface's own app). The trick is
**MIDI feedback**: the tool emits a MIDI message; a key configured to react to that
message displays the value and switches image.

## Overview

```
  rig monitor ──MIDI CC──►  virtual port  ──►  control-surface key (MIDI feedback)
   (emitter)               (dead-end IAC)       (receiver → shows the count)
```

- **Emitter** — the `midi` alert backend (`Alerter.gauge()` in `riglib/alerts.py`),
  driven by `rig monitor` and `rig alert-test`.
- **Transport** — a **dedicated virtual MIDI port** that nothing else in the rig reads,
  so an alert can never trigger a sound or an action elsewhere.
- **Receiver** — a control-surface key configured for **incoming MIDI feedback**.

## Message spec

| Event | MIDI message | Channel | CC | Value | Raw bytes |
|---|---|---|---|---|---|
| Rig healthy | Control Change | 15 | 111 | `0` | `BE 6F 00` |
| N checks failing | Control Change | 15 | 111 | `N` | `BE 6F <N>` |

The value is the count, clamped to 0..127 (a CC carries nothing wider). Port, channel
and CC number are config (`[alerts.midi]`). The channel is kept off the musical channels
so the alert never collides with playing.

## A count, not a lamp

This replaces an earlier design that sent Note On / Note Off on note 60 — one lamp, lit
or not. Two things were wrong with it, and both are what the count fixes:

- **A lamp cannot say how bad it is.** One dead check and five dead checks looked
  identical, so the key never justified a glance at the laptop.
- **An unlit lamp is ambiguous.** "Never lit", "recovered", and "the note-off was
  missed" all look the same. `0` says one thing only. MIDI has no acknowledgement, so
  an event-based protocol has no way to recover from a dropped message — a value-based
  one recovers on the next send.

The value is therefore **re-asserted on every change and on every heartbeat**, not sent
once per transition. A surface powered on after the monitor catches up by itself, which
is the common case on stage: the rig boots in whatever order the cables allow.

## Config

```toml
[alerts.midi]
port    = "rig-alert"    # a dedicated dead-end virtual MIDI port
channel = 15
cc      = 111
```

## Setup (once)

1. **Create the port** — a dead-end virtual MIDI port (on macOS: Audio MIDI Setup →
   IAC Driver → "+" → name it `rig-alert`). It must be one **nothing else routes**, so
   an alert goes nowhere harmful.
2. **Map a key** — on your control surface, add its MIDI action in **feedback / input**
   mode, listening for **Control Change, CC 111, channel 15** on that port. Show the
   value as the key's text, and switch to an alert image when it is not zero.

   With the trevligaspel Stream Deck plugin, the key's script is:

   ```
   [(init){text:RIG\n0}{state:0}]
   [(cc:15,111,*){text:RIG#IF(@e_ccvalue > 0, "\n"&@e_ccvalue, "\n0")#}
                 {state:#IF(@e_ccvalue > 0, 1, 0)#}]
   ```

   The script drives the **state**, not the image: state 0 and state 1 carry the healthy
   and alert images, set once in the Stream Deck UI. That keeps the artwork out of the
   script, so changing an icon never means editing code.

## Test

```bash
./rig alert-test --alerts midi   # → the key should read 3
./rig monitor                    # → puts the real count back
```
