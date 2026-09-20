#!/usr/bin/env bash
# install-coreaudiod-sudoers.sh — pose une règle sudoers NOPASSWD limitée à UNE commande,
# `killall coreaudiod`, pour que le bouton « Relancer le service audio » du dashboard
# (et un bouton Stream Deck) relancent le service audio de macOS sans dialogue de mot de
# passe. À lancer UNE fois, à la main (il demande TON mot de passe, une seule fois).
#
# Pourquoi : coreaudiod tourne sous root. Quand il se fige (2026-09-12 : 9 h sans son, la
# faute à un pilote tiers qui lui faisait écrire ses réglages en boucle), le seul remède
# est de le tuer — launchd le relance dans la seconde. Sans cette règle, le correctif du
# dashboard retombe sur le dialogue de mot de passe macOS : ça marche, mais pas depuis
# une pédale ni un Stream Deck, et pas cinq secondes avant de jouer.
#
# Portée : `/usr/bin/killall coreaudiod` avec exactement cet argument — rien d'autre ne
# gagne root. Tuer coreaudiod est sans perte (il n'a pas d'état à sauver, launchd le
# ressuscite) ; c'est la commande la plus bénigne qu'on puisse donner sans mot de passe.
# Même schéma que surfshark-toggle/install.sh.
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

# visudo -c AVANT d'installer : un sudoers invalide bloque sudo pour tout le monde.
if ! sudo visudo -cf "$TMP" >/dev/null; then
  echo "❌ règle sudoers invalide — rien n'a été installé."
  exit 1
fi

sudo install -m 0440 -o root -g wheel "$TMP" "$RULE_FILE"
echo "✅ $RULE_FILE installé"
echo "   test :  sudo -n /usr/bin/killall coreaudiod   (ne doit PAS demander de mot de passe ;"
echo "           le son coupe 1 s puis revient)"
