#!/bin/bash
# Build RigMenuBar.app (Swift → menu-bar app bundle). Run once, or after edits.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/RigMenuBar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O "$DIR/rigmenubar.swift" -o "$APP/Contents/MacOS/RigMenuBar"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>RigMenuBar</string>
  <key>CFBundleIdentifier</key><string>com.benoit.rigmenubar</string>
  <key>CFBundleExecutable</key><string>RigMenuBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST
echo "✔ built $APP"
