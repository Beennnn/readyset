# audiolevel — EXPERIMENTAL, not wired into the tool

A Swift CoreAudio process-tap helper that measures whether audio **signal** is
flowing on Ableton Live's output (mean RMS), without hearing it. It **works** from
a clean state (silence → RMS 0.000, real audio → ~0.08) and correctly taps the
Ableton process.

## Why it's NOT integrated

Two macOS hurdles make it unsafe to call from the dashboard/monitor:

1. **TCC audio permission** — run directly from a terminal it works, but invoked as
   a subprocess from Python / the launchd server it hangs (the tap blocks on an
   audio-recording permission that a background process can't obtain).
2. **Killing a hung run wedges CoreAudio** — a timed-out run gets SIGKILLed before
   its cleanup (`AudioHardwareDestroyProcessTap` / `…AggregateDevice`) runs, leaking
   taps that jam further tap creation until the leaked processes die. A failed
   measure could disturb audio mid-preflight — unacceptable for a gig tool.

So the rig keeps the **manual "J'entends le son"** confirm in the soundcheck.

## Running it yourself (at your own risk)

```bash
./build.sh
./audiolevel 1.5 ableton     # window seconds, process bundle-id substring (or "global")
# → RMS <mean> TARGET <tapped> FRAMES <n>
```

Run it **directly in a terminal** (not through another process), and don't kill it
mid-run. If tap creation starts hanging, the leaked taps clear once the stuck
processes are killed (`pkill -9 -f audiolevel/audiolevel`); a `sudo killall
coreaudiod` fully resets the HAL but interrupts all audio.
