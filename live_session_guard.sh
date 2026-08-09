#!/bin/bash
# Refuse to deploy on top of a conversation in progress.
#
# On 2026-08-08 a deploy quit the app while Robin was mid-triage; to him it read as a
# crash. A live session is one where the last "LIVE: starting" in the log is newer than
# the last stop. Override with FORCE=1.
#
# exit 0 = safe to deploy · exit 1 = a live session looks active.
#
# Extracted from reload_app.sh so deploy.sh can ask the same question and get the same
# answer. Two copies of "is she mid-conversation?" is how one of them ends up wrong.
set -uo pipefail

LOG="${THRIVBE_VOICE_LOG:-$HOME/Library/Logs/ThrivbeVoice/agent.log}"

[ "${FORCE:-0}" = "1" ] && exit 0
[ -f "$LOG" ] || exit 0

last_evt=$(grep -n "LIVE: starting\|LIVE: stopping\|realtime session closed" "$LOG" | tail -1 || true)
if [[ "$last_evt" == *"LIVE: starting"* ]]; then
  echo "REFUSED: a live voice session looks active (${last_evt##*:})." >&2
  echo "Finish the conversation, or re-run with FORCE=1 to deploy anyway." >&2
  exit 1
fi
exit 0
