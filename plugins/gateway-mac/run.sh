#!/usr/bin/env bash
# gateway-mac — print the default gateway's MAC, or exit 0 if it matches <expected>.
# macOS gap: telling "which network am I on" apart by router MAC (unique to the hardware) vs its
# IP (192.168.1.1 is a near-universal default). Used to distinguish home from stage.
#   run.sh              → prints the gateway MAC
#   run.sh <expected>   → exit 0 iff the gateway MAC equals <expected>
set -uo pipefail
norm() { printf '%s' "$1" | tr 'A-F' 'a-f' | tr '-' ':' | awk -F: '{for(i=1;i<=NF;i++){o=$i;if(length(o)<2)o="0"o;printf "%s%s",(i>1?":":""),o}print""}'; }
gw=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')
[ -n "$gw" ] || exit 1
ping -c1 -W500 "$gw" >/dev/null 2>&1
mac=$(norm "$(arp -n "$gw" 2>/dev/null | grep -oE '([0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}' | head -1)")
[ -n "$mac" ] || exit 1
if [ -n "${1:-}" ]; then [ "$mac" = "$(norm "$1")" ]; else echo "$mac"; fi
