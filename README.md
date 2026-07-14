# readyset

A config-driven **readiness manager** for a set of apps, devices and network endpoints
on macOS. Declare the state your setup should be in; the tool brings it up, checks it,
watches it, and shows you — at a glance — what's wrong.

Built for a live keyboard rig (its example config is one), but the engine is
domain-agnostic: it knows only generic concepts. Everything specific — which apps,
devices, hosts, commands, profiles, icons, topology — lives in **`rig.toml`**. The code
names nothing proprietary.

> **See it in context →** [**A live keyboardist with a laptop on stage**](docs/example-live-keyboardist.md)
> walks through the shipped example config as a real gig rig — the silent-keyboard /
> dead-audio / sleeping-laptop failures it catches, the DAW → router → audio-out signal
> chain, and how one command brings it all up and watches it. Read that if you play with a
> computer on stage; it's the concrete version of everything below.

```bash
cd readyset
cp rig.example.toml rig.toml     # then edit for your setup

./rig preflight     # launch the apps, run post-launch commands, open the project, verify
./rig check         # verify only (no launch) — a fast go/no-go checklist
./rig monitor       # watch continuously; alert the moment something breaks (and recovers)
./rig serve         # web dashboard: signal-flow diagram + per-item fix, on 127.0.0.1:8765
./rig alert-test    # fire a test alert through every active backend
```
Exit codes for `check`/`preflight`: `0` all-green, `1` warnings only, `2` a required
check failed — so you can gate a launcher or a button on it.

## What it can check (generic primitives)

Each is a config entry, not code:

- **apps** — a process is running (regex on the command line)
- **usb_devices** — a USB device is plugged (ioreg product-name match)
- **midi_required** / **keyboard** / **breath_port** — MIDI input ports present (the
  keyboard has ok/fallback tiers per profile)
- **hosts** — a named host answers, by `ip` (ping) or `mac` (ARP → ping)
- **links** — an ESTABLISHED TCP connection exists on a port
- **commands** — *any* condition: run a command, pass on exit 0 (or `expect_match`);
  optional `fix_cmd` gives a one-click remedy. This is the escape hatch — no code needed.
- **keepawake** — an anti-sleep app holds a power assertion (`pmset`)
- **output_probe** — a value read from an app's log matches an allowed set
- **audio_interface** / **default_output** — CoreAudio device present / default output
- **vpn** — inactive
- **manual_confirms** — things software can't detect → a human ticks them before playing

Failed-and-fixable items get a one-click remedy (relaunch an app, run a `fix_cmd`, …).

## Profiles & auto-detection

Define any number of **profiles** (`[modes.*]`) — e.g. `live` / `studio` — each with its
own required devices, severities and audio targets. `[mode].detect` picks one from the
environment: an ordered list of `{profile, criterion}` where criterion is `ping` (a host
answers), `interface` (the Mac holds an IP on a subnet) or `cmd` (a command exits 0);
first match wins, else `fallback`. The dashboard also has a manual toggle.

## Dashboard, soundcheck, alerts

- **`rig serve`** — a local (127.0.0.1) auto-refreshing page: a red/amber/green banner,
  a **signal-flow diagram** (nodes/edges from config, coloured by their checks), then the
  **problems first** with per-item fix buttons; everything OK collapses to chips.
- **Soundcheck** — a live MIDI monitor + a guided "play test" (press the pedal → see CC64
  arrive, play → notes, etc.), and an audio-signal meter.
- **Alerts** (`[monitor].alerts`): `macos` (notification), `push`
  ([ntfy](https://ntfy.sh)), `midi` (a note to a virtual port that lights a
  feedback-configured control-surface key — see
  [docs/midi-alert-protocol.md](docs/midi-alert-protocol.md)).
- A **menu-bar icon** (login agent) opens the dashboard in one click.

## Config

All of it lives in **`rig.toml`** (git-ignored; copy `rig.example.toml`, which is a
complete, realistic, anonymised example — annotated line by line as a live keyboard rig).
For the same example told as a story, see
[A live keyboardist with a laptop on stage](docs/example-live-keyboardist.md).
`riglib/config.py` holds only a neutral, non-personal skeleton. Requires Python 3.11 +
`mido` (`pip install mido python-rtmidi`); two optional Swift helpers (`audiolevel/`,
`menubar/`) build with `build.sh`.

### Note on the menu-bar app (code signing)

`menubar/RigMenuBar.app` is a **locally-built helper, not a notarized/Developer-ID
app**. `build.sh` gives it only an *ad-hoc* signature. In practice:

- **On the Mac that built it** it just runs — no Gatekeeper prompt (the bundle was
  never downloaded, so it carries no quarantine attribute), and the login agent
  starts it silently.
- **If you copy the `.app` to another Mac** (AirDrop, zip, download), macOS quarantines
  it and blocks the first launch as "unidentified developer" / "damaged". Fix there:
  right-click the app → **Open** once, or `xattr -dr com.apple.quarantine RigMenuBar.app`.

The intended path is to **rebuild from source** (`menubar/build.sh`) on each machine
rather than ship the binary. Real signing would need a paid Apple Developer ID +
notarization — deliberately out of scope for a personal login helper.
