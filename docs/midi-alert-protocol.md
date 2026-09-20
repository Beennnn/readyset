# MIDI feedback alert protocol (`midi` backend)

Goal: show on a control surface **how many checks are failing** — without the software
having to repaint the key (it can't, from outside the surface's own app). The trick is
**MIDI feedback**: the tool emits a MIDI message; a key configured to react to that
message displays the value and switches image.

## Overview

```
  readyset monitor ──MIDI CC──►  virtual port  ──►  control-surface key (MIDI feedback)
   (emitter)               (dead-end IAC)       (receiver → shows the count)
```

- **Emitter** — the `midi` alert backend (`Alerter.gauge()` in `readyset/surfaces/alerts/`),
  driven by `readyset monitor` and `readyset alert-test`.
- **Transport** — a **dedicated virtual MIDI port** that nothing else in the rig reads,
  so an alert can never trigger a sound or an action elsewhere.
- **Receiver** — a control-surface key configured for **incoming MIDI feedback**.

## Message spec

Every send is a burst of ten Control Changes on channel 15: one total, then one per
family of checks. Values are counts, clamped to 0..127 (a CC carries nothing wider).

| CC | Letter | Carries |
|---|---|---|
| **111** | — | total number of failing checks — `0` means all good |
| 112 | **A** | applications of the rig that are not running |
| 113 | **M** | missing MIDI ports |
| 114 | **K** | keyboard, breath controller |
| 115 | **S** | sound: interface, devices, Ableton's output |
| 116 | **N** | stage network |
| 117 | **D** | Stream Deck |
| 118 | **L** | stage lamps |
| 119 | **Y** | system: power, sleep, accessibility, the Mac's default output |
| 120 | **X** | an application that should not be running |

The families are **the prefix check keys already carry** (`app:Ableton`, `midi:P-Series`,
`sys:macpower`), so a new check joins its family by itself, with no table to keep in
step. The *order* fixes the CC numbers and must never be re-sorted: a letter that moved
to another CC between two versions would silently make a key display something else,
which is worse than displaying nothing.

Port, channel and the base CC are config (`[alerts.midi]`). The channel is kept off the
musical channels so the alert never collides with playing.

## Counts, not a lamp

This replaces an earlier design that sent Note On / Note Off on note 60 — one lamp, lit
or not. Three things were wrong with it, and the counts fix all three:

- **A lamp cannot say how bad it is.** One dead check and five dead checks looked
  identical, so the key never justified a glance at the laptop.
- **Nor what broke.** Walking to the laptop was the only way to find out — which on
  stage is the one thing you cannot do. The letters answer it from where you stand.
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
   [(init){text:0}{state:0}]
   [(cc:15,111,*){@l_n:#@e_ccvalue#}{state:#IF(@e_ccvalue > 0, 1, 0)#}]
   [(cc:15,112,*){@l_a:#IF(@e_ccvalue > 0, "A", "")#}]
   [(cc:15,113,*){@l_m:#IF(@e_ccvalue > 0, "M", "")#}]
   [(cc:15,114,*){@l_k:#IF(@e_ccvalue > 0, "K", "")#}]
   [(cc:15,115,*){@l_s:#IF(@e_ccvalue > 0, "S", "")#}]
   [(cc:15,116,*){@l_r:#IF(@e_ccvalue > 0, "N", "")#}]
   [(cc:15,117,*){@l_d:#IF(@e_ccvalue > 0, "D", "")#}]
   [(cc:15,118,*){@l_l:#IF(@e_ccvalue > 0, "L", "")#}]
   [(cc:15,119,*){@l_y:#IF(@e_ccvalue > 0, "Y", "")#}]
   [(cc:15,120,*){@l_x:#IF(@e_ccvalue > 0, "X", "")#}]
   [(@l_n:*)(@l_a:*)(@l_m:*)(@l_k:*)(@l_s:*)(@l_r:*)(@l_d:*)(@l_l:*)(@l_y:*)(@l_x:*)
      {text:#@l_n&"\n"&@l_a&@l_m&@l_k&@l_s&@l_r&@l_d&@l_l&@l_y&@l_x#}]
   ```

   The key then reads `3` over `AMN`: three checks down, one application, one MIDI port,
   the stage network. The word RIG comes from the image, the way the held-notes key
   already works.

   The count's variable is `@l_n` and the network's letter lives in `@l_r`, on purpose:
   `N` is the letter shown, but the name `@l_n` was already taken by the count, and two
   meanings in one variable is how a display starts lying.

   The script drives the **state**, not the image: state 0 and state 1 carry the healthy
   and alert artwork, set once in the Stream Deck UI, so changing an icon never means
   editing code.

## Test

```bash
./bin/readyset alert-test --alerts midi   # → the key should read 3 over AMN
./bin/readyset monitor                    # → puts the real count back
```
