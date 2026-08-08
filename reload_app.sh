#!/bin/bash
# Quit -> rebuild -> relaunch Thrivbe Voice.app in one step, for the dev loop after
# editing source. Permissions (Accessibility/Input Monitoring) survive because
# build_app.sh signs with the stable "Thrivbe Voice Dev" identity and no longer
# leaves a duplicate-bundle-id copy at dist/ to confuse System Settings.
set -euo pipefail
cd "$(dirname "$0")"

# Don't deploy on top of a conversation in progress. On 2026-08-08 a deploy quit the
# app while Robin was mid-triage; to him it read as a crash. A live session is one
# where the last "LIVE: starting" is newer than the last stop. Override: FORCE=1
LOG="$HOME/Library/Logs/ThrivbeVoice/agent.log"
if [ -f "$LOG" ] && [ "${FORCE:-0}" != "1" ]; then
  last_evt=$(grep -n "LIVE: starting\|LIVE: stopping\|realtime session closed" "$LOG" | tail -1 || true)
  if [[ "$last_evt" == *"LIVE: starting"* ]]; then
    echo "REFUSED: a live voice session looks active (${last_evt##*:})." >&2
    echo "Finish the conversation, or re-run with FORCE=1 to deploy anyway." >&2
    exit 1
  fi
fi

# Never hot-patch .pyc into the bundle by hand: it runs Python 3.12 and a .pyc built
# by any other interpreter fails at import with "bad magic number" — but the zip still
# looks valid, so the usual checks pass. build_app.sh pins 3.12; always go through it.
osascript -e 'tell application "Thrivbe Voice" to quit' >/dev/null 2>&1 || true
sleep 1

./build_app.sh

open "Thrivbe Voice.app"
sleep 2

echo "--- last log lines ---"
tail -5 ~/Library/Logs/ThrivbeVoice/agent.log 2>/dev/null || echo "(no log yet)"
