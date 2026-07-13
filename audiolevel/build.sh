#!/bin/bash
# Build the audiolevel helper (Swift → native binary). Run once, or after edits.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
swiftc -O "$DIR/audiolevel.swift" -o "$DIR/audiolevel"
echo "✔ built $DIR/audiolevel"
