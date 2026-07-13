#!/bin/bash
# Install the login agents:
#   com.stage-rig.dashboard — auto-starts `rig serve` (dashboard on :8765)
#   com.stage-rig.menubar   — 🎹 menu-bar icon that opens the dashboard
# Both are relaunched by launchd if they die (like the Bome keepalive).
#   ./launchd/install.sh          # build menubar + install/start both
#   ./launchd/install.sh remove   # stop + uninstall both
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABELS=(com.stage-rig.dashboard com.stage-rig.menubar)
UID_="$(id -u)"

if [ "${1:-}" = "remove" ]; then
  for L in "${LABELS[@]}"; do
    launchctl bootout "gui/$UID_/$L" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$L.plist"
  done
  echo "✔ agents retirés"
  exit 0
fi

mkdir -p "$REPO/logs"
# The menu-bar app is a native binary — build it before the agent references it.
bash "$REPO/menubar/build.sh"

for L in "${LABELS[@]}"; do
  # substitute the repo path placeholder so the plist works wherever you cloned it
  sed "s|__RIG_DIR__|$REPO|g" "$REPO/launchd/$L.plist" > "$HOME/Library/LaunchAgents/$L.plist"
  launchctl bootout "gui/$UID_/$L" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$HOME/Library/LaunchAgents/$L.plist"
done
echo "✔ agents installés :"
echo "  • dashboard → http://127.0.0.1:8765 (relancé au login)"
echo "  • 🎹 icône barre de menus → clic = ouvre le dashboard"
echo "  logs: $REPO/logs/{dashboard,menubar}.log"
