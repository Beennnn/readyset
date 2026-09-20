#!/usr/bin/env bash
# install-coreaudiod-sudoers.sh — installs a NOPASSWD sudoers rule limited to ONE command,
# `killall coreaudiod`, so that the dashboard's « Relancer le service audio » button
# (and a Stream Deck button) can restart the macOS audio service with no password
# dialog. To be run ONCE, by hand (it asks for YOUR password, a single time).
#
# Why: coreaudiod runs as root. When it freezes (2026-09-12: 9 h with no sound, the fault
# of a third-party driver that made it write its settings in a loop), the only remedy
# is to kill it — launchd relaunches it within the second. Without this rule, the
# dashboard's fix falls back on the macOS password dialog: it works, but not from a
# pedal nor from a Stream Deck, and not five seconds before playing.
#
# Scope: `/usr/bin/killall coreaudiod` with exactly that argument — nothing else gains
# root. Killing coreaudiod loses nothing (it has no state to save, launchd resurrects
# it); it is the most benign command one could grant without a password.
# Same pattern as surfshark-toggle/install.sh.
set -euo pipefail

USER_NAME="$(id -un)"
RULE_FILE="/etc/sudoers.d/rig-coreaudiod"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

cat > "$TMP" <<RULE
# Installed by readyset/bin/install-coreaudiod-sudoers.sh
# Allow $USER_NAME to restart the macOS audio server without a password — that one
# command only. launchd respawns coreaudiod within a second.
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/killall coreaudiod
RULE

# visudo -c BEFORE installing: an invalid sudoers blocks sudo for everyone.
if ! sudo visudo -cf "$TMP" >/dev/null; then
  echo "❌ règle sudoers invalide — rien n'a été installé."
  exit 1
fi

sudo install -m 0440 -o root -g wheel "$TMP" "$RULE_FILE"
echo "✅ $RULE_FILE installé"
echo "   test :  sudo -n /usr/bin/killall coreaudiod   (ne doit PAS demander de mot de passe ;"
echo "           le son coupe 1 s puis revient)"
