# readyset plugins

Small, self-contained tools that **fill a gap macOS doesn't cover out of the box**. Each one
is shareable on its own, and the rig's checks/fixes call them by path instead of inlining shell.

## Unified API

A plugin is a directory:

```
plugins/<name>/
  plugin.toml   # manifest: name, summary, macos_gap, requires, usage
  run.sh        # the executable — `run.sh [args]`, exit 0 on success, one-line status on stdout
```

Contract:
- `run.sh` is executable, takes **positional args** documented in the manifest's `usage`.
- **Exit 0 = success/true**, non-zero = failure/false (so a plugin doubles as a check).
- Prints a short human line on stdout; errors to stderr.
- No hidden state; anything machine-specific comes from args or env.

Call it from `rig.toml`:
```toml
[checks.fixes."sys:output"]
label = "Sortie → Mac"
cmd = "$HOME/dev/music/readyset/plugins/audio-out/run.sh mac"
```

## Plugins

| Plugin | macOS gap it fills |
|---|---|
| **surfshark-off** | `scutil stop` can't hold an on-demand VPN off → disable the network service |
| **audio-out** | no CLI to set the default sound output device |
| **wifi-rejoin** | no simple "rejoin my remembered WiFi" (`setairportnetwork` wants the password) |
| **phone-state** | read a state an iPhone pushed via ntfy (charging, app-open…) |
| **lan-presence** | "is this device (by MAC) on the LAN right now?" in one call |
| **gateway-mac** | identify the network by its **router MAC** (unique) vs its IP (universal default) |

Each is usable standalone (`plugins/<name>/run.sh --help`-ish via its `plugin.toml`), so they can
be copied out or split into their own repos to share.
