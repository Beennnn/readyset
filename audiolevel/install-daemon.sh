#!/bin/bash
# Install the audio-level probe as a per-user LaunchAgent (OPTIONAL, opt-in).
#
# The probe holds the macOS audio permission so the soundcheck can auto-confirm "sound is
# flowing" instead of asking you. It publishes the level to the file set in rig.toml under
# [audiolevel].file; the engine only reads that file (no macOS code in the engine).
#
#   audiolevel/install-daemon.sh [bundle-substring]
#
# bundle-substring (optional): only measure that app's output (e.g. "ableton"); omit to tap
# the global mix. Interval is 1s.
#
# IMPORTANT — grant the audio permission FIRST (one time), while you can see the prompt:
#   audiolevel/audiolevel 1.5 <bundle>    # run once by hand → macOS asks for audio access → Allow
# Then run this installer. TCC keys the grant to the binary, so the daemon inherits it.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
BUNDLE="${1:-}"
INTERVAL="1"
LABEL="com.readyset.audiolevel"
PLIST_SRC="$REPO/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_="$(id -u)"

# The engine + the daemon must agree on the file path → take it from rig.toml.
FILE="$(cd "$REPO" && python3 -c 'from readyset import config; print(config.load().get("audiolevel",{}).get("file","") or "")')"
if [ -z "$FILE" ]; then
  echo "✖ [audiolevel].file n'est pas défini dans rig.toml — renseigne-le d'abord, ex :"
  echo '    [audiolevel]'
  echo '    file = "~/.cache/readyset/audiolevel"'
  exit 1
fi
FILE_EXPANDED="${FILE/#\~/$HOME}"

[ -x "$DIR/audiolevel" ] || "$DIR/build.sh"

# Build the <string> args for the plist: interval, file, and optional bundle.
ARGS="        <string>$INTERVAL</string>
        <string>$FILE_EXPANDED</string>"
[ -n "$BUNDLE" ] && ARGS="$ARGS
        <string>$BUNDLE</string>"

mkdir -p "$REPO/logs" "$HOME/Library/LaunchAgents"
# Substitute placeholders. Use a python pass so the multi-line ARGS injects cleanly.
REPO="$REPO" ARGS="$ARGS" python3 - "$PLIST_SRC" "$PLIST_DST" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
t = open(src).read().replace("__RIG_DIR__", os.environ["REPO"]).replace("        __ARGS__", os.environ["ARGS"])
open(dst, "w").write(t)
PY

launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_" "$PLIST_DST"
echo "✔ sonde audiolevel installée (relancée au login)"
echo "  • écrit le niveau dans : $FILE_EXPANDED  (toutes les ${INTERVAL}s)"
[ -n "$BUNDLE" ] && echo "  • mesure la sortie de : *$BUNDLE*" || echo "  • mesure : mix global"
echo "  • log : $REPO/logs/audiolevel.log"
echo
echo "⚠️  Si le niveau reste à 0 : la permission audio n'est pas accordée. Lance une fois à la"
echo "    main pour déclencher la demande — audiolevel/audiolevel 1.5 $BUNDLE — puis Autorise,"
echo "    et redémarre la sonde : launchctl kickstart -k gui/$UID_/$LABEL"
