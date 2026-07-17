#!/bin/bash
# Install the login agents:
#   com.readyset.dashboard — auto-starts `rig serve` (dashboard on :8765)
#   com.readyset.menubar   — 🎹 menu-bar icon that opens the dashboard
# Both are relaunched by launchd if they die.
#   ./launchd/install.sh          # build menubar + install/start both
#   ./launchd/install.sh remove   # stop + uninstall both
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABELS=(com.readyset.dashboard com.readyset.menubar)
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
# The menu-bar app is a native binary — build it (output in the repo), then DEPLOY it to the
# standard apps folder so it lives like any installed app; the LaunchAgent points to /Applications.
bash "$REPO/menubar/build.sh"
APP_DEST="/Applications/RigMenuBar.app"
rm -rf "$APP_DEST"
cp -R "$REPO/menubar/RigMenuBar.app" "$APP_DEST"
echo "✔ RigMenuBar.app déployé dans /Applications"

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
echo "  note: RigMenuBar.app est ad-hoc signé (pas notarisé). Sur CE Mac = OK ;"
echo "        copié ailleurs → clic-droit ▸ Ouvrir (voir menubar/build.sh)."
