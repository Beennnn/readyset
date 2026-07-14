# audiolevel — automatic audio-signal probe

A Swift CoreAudio process-tap helper that measures whether audio **signal** is flowing
(mean RMS) without hearing it. Feeds the soundcheck so it can auto-confirm "sound is
out" instead of asking you the manual "J'entends le son" question.

## The design (why a separate long-lived process)

macOS gates audio taps behind **TCC** (the per-app audio permission). A subprocess spawned
by the dashboard does **not** inherit that grant, so a one-shot call from the server comes
back silent. The fix: run the probe as its **own** long-lived process (a LaunchAgent you
authorise once) that publishes the level to a file. The engine only **reads** that file —
no macOS-specific code in the engine, and any meter writing the same format works.

```
audiolevel --daemon ──writes──►  <file>  ──reads──►  readyset [audiolevel]  ──►  soundcheck
 (holds the TCC grant)     "<rms> <epoch>"          (generic file read)      (live meter / auto-confirm)
```

## Install (opt-in)

```bash
# 1. Enable in rig.toml:
#      [audiolevel]
#      file = "~/.cache/readyset/audiolevel"
# 2. Grant the audio permission ONCE, while you can see the prompt:
./audiolevel 1.5 ableton          # run by hand → macOS asks for audio access → Allow
# 3. Install as a login service (bundle substring optional; omit for global mix):
./install-daemon.sh ableton
```

If the level stays 0, the permission isn't granted yet — repeat step 2, then
`launchctl kickstart -k gui/$(id -u)/com.readyset.audiolevel`.

When `[audiolevel].file` is empty or the reading is stale, the soundcheck automatically
falls back to the manual confirm — the probe is a pure enhancement, never a requirement.

## Modes

```bash
./audiolevel 1.5 ableton                 # ONE-SHOT: measure 1.5s, print "RMS <n> TARGET <t> FRAMES <n>"
./audiolevel --daemon 1 <file> ableton   # DAEMON: keep the tap open, write "<rms> <epoch>" to <file> every 1s
```

## Troubleshooting

- Level always 0 → TCC not granted (see install step 2), or nothing is playing.
- Tap creation starts hanging → leaked taps from a killed run; `pkill -9 -f audiolevel/audiolevel`
  clears them, or `sudo killall coreaudiod` fully resets the HAL (interrupts all audio).
