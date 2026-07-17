#!/usr/bin/env bash
# wifi-rejoin — get the Mac back on its stage WiFi.
# macOS gap: `networksetup -setairportnetwork` needs the password in clear (error -3900 without),
# and there's no simple "rejoin the remembered network" CLI. Cycling WiFi power makes macOS
# auto-join the strongest REMEMBERED network in range, using its stored credentials. On stage
# the home network is out of range → auto-join lands on the stage SSID.
#   run.sh                → cycle WiFi (auto-join)
#   run.sh <ssid> [pw]    → force-join a specific network (needs its password)
set -uo pipefail
IF="${WIFI_IF:-en0}"
if [ -n "${1:-}" ]; then
  networksetup -setairportpower "$IF" on; sleep 1
  networksetup -setairportnetwork "$IF" "$1" "${2:-}" && echo "📶 rejoint '$1'"
else
  networksetup -setairportpower "$IF" off; sleep 2; networksetup -setairportpower "$IF" on
  echo "📶 WiFi cyclé → auto-join du réseau mémorisé le plus proche"
fi
