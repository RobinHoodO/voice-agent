#!/bin/bash
# Quit -> rebuild -> relaunch Thrivbe Voice.app in one step, for the dev loop after
# editing source. Permissions (Accessibility/Input Monitoring) survive because
# build_app.sh signs with the stable "Thrivbe Voice Dev" identity and no longer
# leaves a duplicate-bundle-id copy at dist/ to confuse System Settings.
set -euo pipefail
cd "$(dirname "$0")"

# Don't deploy on top of a conversation in progress, and don't ship a tool set that has
# drifted. Both guards live in their own scripts now (live_session_guard.sh,
# drift_gate.sh) so ./deploy.sh — which ships this surface AND the server one — applies
# exactly the same policy instead of a second copy of it that can rot.
# FORCE=1 still skips the live-session guard.
./live_session_guard.sh || exit 1
./drift_gate.sh || exit 1

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
