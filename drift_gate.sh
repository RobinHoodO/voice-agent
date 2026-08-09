#!/bin/bash
# The drift gate, in one place: run check_tool_drift.py and turn its exit code into a
# deploy decision.
#
#   0 → converged, go
#   1 → the Mac and the server surfaces disagree, or a surface disagrees with the kernel
#       manifest. A tool that drifts out of the manifest silently loses its high-stakes
#       confirm gate, so this blocks.
#   2 → the checker itself is misconfigured or cannot evaluate a surface. Blocks: a
#       blind check is worse than no check, because it looks green.
#   3 → the kernel tunnel is down, so only the manifest half could not run. Warn and
#       continue — the cross-surface half runs first and already passed.
#
# exit 0 = deploy may proceed · exit 1 = refuse.
#
# Extracted from reload_app.sh so deploy.sh gates both surfaces with the same policy.
set -uo pipefail
cd "$(dirname "$0")"

code=0
.venv/bin/python check_tool_drift.py "$@" || code=$?

if [ "$code" = "3" ]; then
  echo "WARN: kernel tunnel down — deploying without the manifest half of the drift check." >&2
  echo "      Cross-surface convergence ran first and is clean." >&2
  exit 0
elif [ "$code" != "0" ]; then
  echo "REFUSED: tool drift (or a broken checker, exit $code) — fix it before deploying." >&2
  exit 1
fi
exit 0
