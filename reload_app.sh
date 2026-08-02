#!/bin/bash
# Quit -> rebuild -> relaunch Thrivbe Voice.app in one step, for the dev loop after
# editing source. Permissions (Accessibility/Input Monitoring) survive because
# build_app.sh signs with the stable "Thrivbe Voice Dev" identity and no longer
# leaves a duplicate-bundle-id copy at dist/ to confuse System Settings.
set -euo pipefail
cd "$(dirname "$0")"

osascript -e 'tell application "Thrivbe Voice" to quit' >/dev/null 2>&1 || true
sleep 1

./build_app.sh

open "Thrivbe Voice.app"
sleep 2

echo "--- last log lines ---"
tail -5 ~/Library/Logs/ThrivbeVoice/agent.log 2>/dev/null || echo "(no log yet)"
