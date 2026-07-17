#!/usr/bin/env bash
# surfshark-off — turn OFF the currently-connected VPN(s) and keep them off.
#
# macOS gap: `scutil --nc stop` only drops an on-demand VPN for ~0.5s; the on-demand engine
# recomposes the tunnel on the next packet. The reliable parade is to DISABLE the network
# SERVICE (on-demand can't connect a service that's administratively off). See the writeup at
# github.com/Beennnn/surfshark-toggle. Needs that repo's scoped NOPASSWD sudoers rule:
#   <user> ALL=(root) NOPASSWD: /usr/sbin/networksetup -setnetworkserviceenabled *
set -uo pipefail

scutil --nc list | grep '(Connected)' | grep VPN | sed -E 's/.*"([^"]+)".*/\1/' | while IFS= read -r svc; do
  [ -n "$svc" ] || continue
  sudo /usr/sbin/networksetup -setnetworkserviceenabled "$svc" off
  id=$(scutil --nc list | grep -F "\"$svc\"" | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27}' | head -1)
  [ -n "$id" ] && /usr/sbin/scutil --nc stop "$id" 2>/dev/null || true
  echo "🔒 VPN OFF — \"$svc\" (service désactivé, l'on-demand ne peut plus le reconnecter)"
done
