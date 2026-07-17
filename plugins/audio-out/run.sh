#!/usr/bin/env bash
# audio-out — set the macOS default OUTPUT device by name substring.
# macOS gap: no built-in CLI to change the default sound output. Wraps SwitchAudioSource.
#   run.sh <substring>   e.g. run.sh mac
set -uo pipefail
want="${1:?usage: run.sh <name-substring>}"
SAS=$(command -v SwitchAudioSource || echo /opt/homebrew/bin/SwitchAudioSource)
[ -x "$SAS" ] || { echo "SwitchAudioSource introuvable (brew install switchaudio-osx)" >&2; exit 1; }
dev=$("$SAS" -a -t output | grep -i "$want" | head -1)
[ -n "$dev" ] || { echo "aucun périphérique de sortie ne matche '$want'" >&2; exit 1; }
"$SAS" -t output -s "$dev" && echo "🔊 sortie par défaut → $dev"
