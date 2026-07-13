# `rig` — stage keyboard rig bring-up & live monitoring

Control tool for a live keyboard rig (macOS): one command puts it on stage,
one command watches it while I play. macOS, Python 3.11+ (`mido`), stdlib otherwise.

```bash
cd stage-rig
cp rig.example.toml rig.toml   # then edit — set your gig .als, alerts, etc.

./rig preflight     # launch Bome → Stream Deck → Stage Traxx → open the gig set, then verify
./rig check         # verify only (no launch) — a fast go/no-go checklist
./rig monitor       # watch continuously; alert the moment something dies
./rig serve         # web dashboard: signal-flow diagram + per-item fix/relaunch button
./rig alert-test    # fire a test alert through every active backend
```

## Dashboard (`rig serve`)

`./rig serve` opens a local page (127.0.0.1 only) with the whole rig at a glance:
a red/amber/green banner, then every check grouped **Apps / MIDI requis / Audio /
MIDI optionnel** with its status and detail. The state auto-refreshes (read-only).

Each **broken and fixable** row gets an action button — app down → *Relancer …*,
Ableton missing → *Ouvrir le set*, a required MIDI port gone → *Rouvrir le set* /
*Relancer Bome*. Hardware faults (audio interface unplugged) show the detail with no
button. Up top: **Préflight complet** (full bring-up) and a **mode simulation
(dry-run)** toggle that makes every action inert end to end. Flags: `--port`, `--no-open`.

## What it checks

| Check | Level | Why |
|---|---|---|
| Ableton / Stream Deck / Bome running | ❌ fail | the core apps — matched by process, version-agnostic |
| Virtual MIDI ports (`Ableton Loopback`, `Daw2Mackie`, `Mackie2XR18`, `XR182Mackie`) | ❌ fail | if these are gone, nothing routes |
| Optional MIDI gear (keyboard, breath, foot) | ⚠️ warn | absent = just unplugged, not a showstopper |
| Audio interface (`RME Fireface UCX` / `XR18`) | ❌ fail | no interface = no sound |

`preflight` / `check` exit **0** all-green, **1** warnings only, **2** a required check failed —
so you can gate a launcher or a Stream Deck button on the exit code.

## Monitoring & alerts

`monitor` prints a baseline checklist, then alerts **on transitions** (something dies →
alert; it comes back → recovery alert) plus a heartbeat so silence never looks like a hang.
If the rig is already broken when monitoring starts, it alerts immediately.

Pick backends in `rig.toml` (`[monitor].alerts`) or per-run (`--alerts macos,push`):

- **`macos`** — banner + sound via `osascript`. Zero setup; needs the laptop screen.
- **`push`** — HTTP POST to [ntfy.sh](https://ntfy.sh) (stdlib, no install). Set a private
  `[alerts.push].topic`, subscribe to it in the ntfy phone app → buzzes anywhere.
- **`streamdeck`** — emits a MIDI note on the dedicated `rig-alert` IAC port. Configure a
  Stream Deck key with **MIDI feedback** (note 60, ch 15) to light red on note-on / clear on
  note-off. Full spec + wiring: [docs/rig-alert-protocol.md](docs/rig-alert-protocol.md).
  *(This is why the alert doesn't use the trevligaspel plugin: that plugin is button → MIDI
  only and can't repaint a key from outside. MIDI feedback is the honest inbound path.)*

## Config

`riglib/config.py` holds baked defaults (real process names + virtual ports as of 2026-07).
`rig.toml` (git-ignored) overrides only what differs; `rig.example.toml` is the template.
Requires Python 3.11+ and `mido` (`pip install mido python-rtmidi`).
