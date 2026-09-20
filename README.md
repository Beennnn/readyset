# readyset

**Declare the state your setup should be in. `readyset` brings it up, checks it,
watches it, and tells you at a glance what is wrong.**

Built for a live keyboard rig — the example config is one — but the engine knows
nothing about music. It knows apps, devices, hosts, links, commands. Everything
specific lives in `rig.toml`.

```bash
readyset preflight     # bring everything up, then verify it
readyset check         # verify only — no launching, no side effects
readyset watch         # keep watching while you work, and shout when it breaks
```

---

## The problem it solves

A setup that depends on eight applications, four MIDI devices, a network host and
a VPN has no single place that says *yes, you are ready*. You find out you are
not — on stage, mid-song, when a keyboard answers nothing.

The usual answer is a checklist you run by hand and stop running after a month.
`readyset` is that checklist as a file, run by a machine, before it matters.

## What it can check

Ten generic primitives. None of them names a product: your config does.

| | |
|---|---|
| `apps` | is it running, and did it start when asked |
| `usb_devices` | present on the bus, by name or by serial |
| `midi_required` | the MIDI ports that must exist |
| `hosts` | reachable over the network |
| `links` | symlinks that point where they should — the ones that break silently |
| `commands` | anything expressible as an exit code |
| `audio_interface` | the right interface, actually selected |
| `output_probe` | sound really comes out, verified by measurement |
| `keepawake` · `vpn` | the machine stays awake, the tunnel is where you want it |
| `manual_confirms` | what only a human can assert — *the pedal is plugged in* |

Results land in three places: the terminal, a **local web dashboard**, and a
**menu-bar app** that turns red before you do.

## Profiles

One setup is several setups. A rehearsal is not a gig, and a gig at home is not
one in a venue. Profiles say which checks apply where, and `readyset` detects
which one it is in — by the devices present, the network it is on, or what you
tell it.

## Configuration

Everything lives in `rig.toml`. Start from `rig.example.toml`, which is a real
rig described in full, and delete what is not yours.

```toml
[[apps]]
name = "My DAW"
bundle_id = "com.example.daw"
launch = true

[[midi_required]]
port = "Main Bus"
reason = "the port every controller speaks on"
```

The code never mentions an application, a serial number or a hostname. If it
does, that is a bug — and the reason this repository exists separately from the
one below.

## Where this code comes from

`readyset` is the **downstream** half of a pair, and that shapes what you find
here.

Upstream is a private repository holding one real installation: its serial
numbers, its service agents, its file paths, its venues. Everything starts
there, on hardware that has to work. When something proves itself over several
outings **and** turns out to name no product, it moves here.

The rule that keeps the two from drifting is one sentence: **nothing is born
here.** This repository receives; it does not break ground. It was split out on
2026-08-18, after the generic and the specific had grown about 2800 lines apart
inside a single codebase.

The practical consequence, if you are here to use it: what you get is the part
that has already survived somewhere else. And if you ever find an application
name, a serial number or a hostname in the code rather than in a config file,
that is a bug — it means the boundary leaked.

## Requirements

macOS, Python 3.11+. `mido` for the MIDI checks; standard library otherwise. The
menu-bar app is Swift and optional — everything works without it.

## Licence

MIT.
