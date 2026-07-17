#!/usr/bin/env bash
# lan-presence — is a device (by MAC) present on the local network right now?
# macOS gap: needs an ARP lookup (+ a ping to warm it and confirm it answers). Exit 0 if present.
#   run.sh <mac>
set -uo pipefail
raw="${1:?usage: run.sh <mac>}"
mac=$(printf '%s' "$raw" | tr 'A-F' 'a-f' | tr '-' ':' | awk -F: '{for(i=1;i<=NF;i++){o=$i;if(length(o)<2)o="0"o;printf "%s%s",(i>1?":":""),o}print""}')
ip=$(arp -a -n 2>/dev/null | grep -i "$mac" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)
[ -n "$ip" ] || exit 1
ping -c1 -W600 "$ip" >/dev/null 2>&1 && echo "📶 présent : $ip"
