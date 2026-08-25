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

## Ce dépôt, et son jumeau `rig-control` — deux rôles, pas deux copies

`readyset` est la **version publiable** de l'outil : générique, sous licence, avec
`plugins/`, sans rien qui appartienne à une installation particulière.

Son jumeau privé [`rig-control`](https://github.com/Beennnn/rig-control) est **le rig
réel de Benoît** : sa config, ses n° de série, ses agents launchd. C'est là que tout
arrive en premier, sur du matériel qui joue — et c'est de là que remonte ici ce qui est
généralisable, une fois prouvé.

Partage décidé le 2026-08-18, après que les deux eurent divergé de ~2800 lignes. La
règle qui évite que ça recommence : **rien ne naît ici**. Ce dépôt reçoit, il ne
défriche pas.

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

### Menu-bar alerts (RigMenuBar)

`menubar/RigMenuBar.app` puts a keyboard glyph in the menu bar and polls
`/api/state` every 5 s. It makes a non-ok rig **visible on the Mac without a Stream
Deck**, using only passive, silent signals (no repeating sound). Every mechanism is
toggleable from the glyph's **options menu** and the choice persists (UserDefaults):

| Mechanism | What it does |
|---|---|
| **Menu-bar glyph** | Recoloured by status: ok → discreet template, warn → orange, fail → red, dashboard unreachable → grey. |
| **Edge border** | A coloured frame around every screen (click-through, all Spaces, over full-screen apps). Warn/fail only. |
| **Flashing alarm** | *(default ON)* A red banner + thick frame **pulsing** on every screen when a check breaks **after** the screen was clean — the Mac losing AC power, a MIDI port dropping mid-set. It is the only signal that answers "something just BROKE" rather than "here is the state", so it triggers on a *regression* only, never on a rig that was already red at launch, and it keeps flashing until **that** failure is fixed (not until the rig is fully green — a permanent warning would flash all night and teach you to ignore it). Click-through like the border; silence it from the 🎹 menu. |
| **Floating pill** | A top-centre badge with the counts (`❌ N  ⚠ M  Rig`), one per screen. Warn/fail only. **Clickable** (see below). |
| **Expanded panel** | *(default ON)* The problem list ALWAYS unfolded in a HUD panel under the pill, laid out in **columns**: status icon (❌/⚠️) · short item name · short problem · a **fix button** per available remedy. A footer has **⚡ Lancer tous les correctifs** (runs every available fix) and **⚙️ Config** (opens the dashboard). One panel per screen. |
| **Hide warnings in the popup** | *(config toggle)* When off, the panel lists only blockers (`fail`) — warnings still count in the pill/glyph but don't clutter the list. |
| **Auto-fix on the fly** | *(off by default, ⚠️ labelled as potentially disruptive)* When on, each problem's remedy fires automatically as it appears, throttled to once per check per 60 s. |
| **One notification on change** | A single silent banner the moment the status *worsens* into a problem — never repeats, stays in Notification Center until dismissed. Off by default. |

**Pill interactions** (only the small pill + panel windows catch clicks — the border
and the rest of the screen stay click-through, so nothing is blocked mid-gig):

- **Single click** → with the expanded panel ON (default), folds/unfolds the panel. With
  it OFF, pops the same problem list as a menu.
- **Double click** → opens the web dashboard.
- **🔧 fix button** (panel or menu) → `POST`s the check's key to `/api/fix` and refreshes.
  A check with no remedy (`remedy: null`) is shown greyed / informational only.
- **Pop on new problem** *(toggle, default on)* → if a check that wasn't a problem before
  just became one, the panel auto-unfolds so it can't be missed. Turn it off to keep a
  folded panel folded.

"Unreachable" (dashboard stopped, often on purpose) stays quiet — just the grey glyph,
no border/pill — so stopping the server doesn't paint a permanent frame everywhere.
The one HTTP dependency (the loopback poll) needs `NSAllowsLocalNetworking` in the app's
`Info.plist`; `build.sh` sets it. Note: colouring the menu-bar glyph rebuilds the SF
Symbol image with a palette colour rather than setting `contentTintColor` — the menu bar
renders *template* glyphs in its own vibrant colour and ignores the tint.

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
