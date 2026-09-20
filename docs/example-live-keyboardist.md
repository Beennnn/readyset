# Example — a live keyboardist with a laptop on stage

This is the concrete rig that `rig.example.toml` describes, told as a story. If you gig
with a computer as an instrument — your sounds live in a DAW, your backing tracks in an
app, your patches on a control surface — this is your world, and this is what readyset
is for. Everything below is anonymised (no real paths, IPs, MACs or push topic), but it
maps one-to-one to the runnable [`rig.example.toml`](../rig.example.toml).

## The problem it solves

On stage, your laptop *is* an instrument, and it has more ways to fail silently than a
piano does. The nightmares are specific:

- You hit the first chord and **nothing comes out** — the DAW picked the wrong audio
  output, or the interface wasn't detected at boot.
- Mid-song the **keyboard goes dead** — a USB hub dropped, or the MIDI port renamed itself.
- The **backing-track app crashed** between songs and you don't notice until the drummer
  counts in.
- The laptop **went to sleep** during a long talk-break, or the **battery drained** even
  though you thought it was plugged in.
- The **control surface** you rely on for patch changes is asleep or unplugged.
- Your phone (feeding network-MIDI, or just your setlist) **dropped off the stage Wi-Fi**.

None of these are hard to *check* — you just have to remember to check all of them, every
gig, under pressure, in a dark room. readyset is that checklist, automated: it brings the
readyset up in order, verifies every link, and then watches them for the whole set.

## The rig

| Piece | Role | How readyset knows it's OK |
|---|---|---|
| **DAW** (Ableton Live) opening a **gig set** (`Funk Set.als`) | your sound engine + the actual songs | app process running; project opened by preflight |
| **Audio interface** (RME-class USB) | sound out to the PA | CoreAudio device present; DAW's logged output device matches |
| **Main keyboard** (88-key stage piano) | what you play | its MIDI input port is present (with a fallback tier in studio) |
| **Breath controller** | expression for winds/leads | its MIDI input port is present |
| **MIDI router** (Bome Translator + Network) | glue between surfaces, phone and the DAW | both apps running; the loopback MIDI port exists |
| **Two Stream Decks** (XL + Plus) | patch changes, song navigation | both plugged (USB) and awake; app running |
| **Backing-track app** (Stage Traxx) | click + tracks the band plays to | app process running |
| **Phone** | network-MIDI / setlist over Wi-Fi | an ESTABLISHED TCP link to the router's port |
| **Stage modem + two smart lamps** | the local network + your stage lighting | modem answers by IP (blocking); lamps by MAC (warning) |
| **Anti-sleep app** (Amphetamine) | keeps the Mac awake all set | holds a power assertion (`pmset`) |

## The signal flow

readyset draws this as a live diagram on the dashboard, each box coloured by its own
checks. The same topology is the `[diagram]` block in the example config:

```
  iPhone ─┐
          ├─► Bome (router) ─┐
Stream ───┘                  ├─► Ableton Live ─► Audio out ─► PA
 Decks                       │      (DAW)
                             │
Keyboard ────────────────────┘
                             │
Stage Traxx ─────────────────┘
```

If any box goes red, you see *where* in the chain it broke — a dead "Audio out" is a very
different fix from a dead "Keyboard", and the diagram tells them apart at a glance.

## What readyset does for this rig

- **`readyset preflight`** — before doors, one command: launches Bome (the MIDI plumbing) first,
  then the Stream Decks, backing-track app and anti-sleep app, starts an Amphetamine
  session, opens the gig set in the DAW, then verifies everything above. Exit code `2` if a
  *required* thing is missing — so you know before the first song, not during it.
- **`readyset monitor`** — for the whole gig, re-checks every few seconds and **alerts the moment
  something breaks and again when it recovers**. If the interface drops mid-set you get a
  macOS notification, a phone push, *and* a Stream Deck key turns red — you don't have to be
  looking at the laptop.
- **Soundcheck** (`readyset serve` → 🎹 Soundcheck) — a guided play-test: press the sustain pedal
  and watch CC64 arrive, play and watch the notes, blow the breath controller, wiggle each
  controller — each step ticks green as the MIDI actually arrives. The final step, "is sound
  coming out?", is confirmed either by an [automatic audio-level meter](../audiolevel/README.md)
  or, if you haven't installed that helper, a manual tick.

## Two rigs, one config: live vs studio

The same laptop plays gigs and works at home, where the "correct" state differs — at home
the sound comes out of the MacBook speakers, a mini-keyboard is fine, and the Mac sleeping
is not a crisis. So the example defines two **profiles**, `live` and `studio`, with
different required keyboards, audio outputs and severities. `[mode].detect` picks
automatically: when the studio network is reachable it's `studio`, otherwise `live`. On
stage everything that matters is a blocking `fail`; at home the same gaps are gentle `warn`s.

## Make it yours

The story above is one rig; the engine knows none of it. Copy the example and swap in your
own apps, ports, devices and network — the code names nothing proprietary:

```bash
cp rig.example.toml rig.toml   # then edit for your setup
./bin/readyset preflight
```

Every real-world worry maps to a generic primitive (an app, a USB device, a MIDI port, a
host, a TCP link, a command, a manual tick). If yours isn't covered by a named check, the
`commands` escape hatch runs *any* shell condition — see the
[main README](../README.md#what-it-can-check-generic-primitives) for the full list.
