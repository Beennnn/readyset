#!/bin/bash
# Build RigMenuBar.app (Swift → menu-bar app bundle). Run once, or after edits.
#
# NOT notarized / not Developer-ID signed. This is a locally-built helper, not a
# distributed app — so it carries only an *ad-hoc* signature (see codesign below).
# Consequences:
#   • Built + launched on THIS Mac → runs with no prompt (no quarantine attribute,
#     since it was never downloaded). launchd starts it silently at login.
#   • Copied to ANOTHER Mac (AirDrop/zip/download) → macOS sets the quarantine bit
#     and Gatekeeper blocks the first launch ("unidentified developer" / "damaged").
#     Fix on that Mac: right-click the .app → Open (once), OR strip quarantine:
#       xattr -dr com.apple.quarantine /path/to/RigMenuBar.app
# Signing it for real would need a paid Apple Developer ID + notarization — overkill
# for a personal login helper. Rebuild from source instead of shipping the binary.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/RigMenuBar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O "$DIR/rigmenubar.swift" -o "$APP/Contents/MacOS/RigMenuBar"
# Localisations: one .lproj per language, copied verbatim. English needs no file —
# it is the default value baked into every T() call, so a bundle with zero .lproj
# still runs, in English. A translation only overrides the keys it defines.
if compgen -G "$DIR/Resources/*.lproj" > /dev/null; then
  mkdir -p "$APP/Contents/Resources"
  cp -R "$DIR"/Resources/*.lproj "$APP/Contents/Resources/"
  echo "  langues : $(cd "$DIR/Resources" && ls -d *.lproj | sed 's/\.lproj//' | tr '\n' ' ')+ en (base)"
fi
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>RigMenuBar</string>
  <key>CFBundleIdentifier</key><string>com.readyset.menubar</string>
  <key>CFBundleExecutable</key><string>RigMenuBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <!-- Base language = English (it lives in the source, as T()'s default value).
       Every code listed here must have a matching .lproj under menubar/Resources/,
       otherwise macOS falls back to the development region. -->
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleLocalizations</key><array>
    <string>en</string>
    <string>fr</string>
  </array>
  <!-- Allow the plain-HTTP poll to the local dashboard (127.0.0.1:8765). ATS blocks
       cleartext by default; NSAllowsLocalNetworking whitelists loopback/.local/private IPs. -->
  <key>NSAppTransportSecurity</key><dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict></plist>
PLIST
# Ad-hoc signature ("-" = no identity): makes the bundle self-consistent so recent
# macOS doesn't flag it as "damaged" when launched locally. NOT a substitute for a
# Developer-ID signature — it grants no distribution trust (see header note).
codesign --force --deep --sign - "$APP" 2>/dev/null || true
echo "✔ built $APP (ad-hoc signed, not notarized — see header comment)"
