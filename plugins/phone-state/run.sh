#!/usr/bin/env bash
# phone-state.sh — read a phone-pushed state from an ntfy topic.
#
#   phone-state.sh <topic> <key> [max_age_seconds]
#
# The iPhone can't be queried over the LAN (iOS exposes no battery/app API to peers), so
# instead IT pushes its own state: a Shortcuts personal automation POSTs a message to an
# ntfy topic on each change — "charging=1" when plugged in, "charging=0" when unplugged,
# "stagetraxx=1" when the app opens, etc. See docs/phone-shortcuts.md.
#
# This reads the LATEST message for <key> and exits 0 IFF its value is "1" AND it is fresher
# than max_age. Anything else — value 0, no message, stale, network error — exits 1, so the
# rig treats "no fresh confirmation" as a WARNING ("à confirmer"), never a hard "definitely
# off" (a dropped push must not become a false negative).
#
# Result is cached ~25 s so a fast-polling dashboard doesn't hammer ntfy.
set -uo pipefail

TOPIC="${1:?usage: phone-state.sh <topic> <key> [max_age_s]}"
KEY="${2:?key required}"
MAX_AGE="${3:-21600}"                        # freshness window, default 6 h
now=$(date +%s)
CACHE="/tmp/rig-phone-${KEY}.cache"          # holds "<value> <epoch>"

read_cached() { [ -f "$CACHE" ] && [ $(( now - $(stat -f %m "$CACHE" 2>/dev/null || echo 0) )) -lt 25 ]; }

if read_cached; then
    read -r val t < "$CACHE"
else
    since=$(( now - MAX_AGE ))
    json=$(curl -s --max-time 5 "https://ntfy.sh/${TOPIC}/json?poll=1&since=${since}" 2>/dev/null) || json=""
    latest=$(printf '%s\n' "$json" | grep -F "\"message\":\"${KEY}=" | tail -1)
    if [ -n "$latest" ]; then
        val=$(printf '%s' "$latest" | sed -E "s/.*\"message\":\"${KEY}=([^\"]*)\".*/\1/")
        t=$(printf '%s' "$latest" | sed -E 's/.*"time":([0-9]+).*/\1/')
    else
        val="" ; t=0
    fi
    [ -n "$t" ] || t=0
    printf '%s %s\n' "${val:-_}" "$t" > "$CACHE"
    [ "$val" = "" ] && val="_"
fi

[ "$val" = "1" ] && [ "$t" -gt 0 ] && [ $(( now - t )) -le "$MAX_AGE" ]
