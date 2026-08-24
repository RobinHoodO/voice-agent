#!/bin/bash
# The weekly drift check, as a scheduled job — the half `drift_gate.sh` does not do.
#
# `drift_gate.sh` turns check_tool_drift.py's exit code into a DEPLOY decision, and a
# human is standing there reading it. This wrapper turns the same exit code into a
# REPORT, because on a Monday morning nobody is standing there. Until 2026-08-10 the
# reporting half lived on Thrivbe-1 (`/opt/voice-bridge/tool-drift-check.sh`, which
# guarded the phone bridge's schemas and pinged Sentry on divergence). The bridge is
# retired; this is where that behaviour lands, pointed at the surfaces that still exist.
#
# WHY IT MATTERS, and it is not schema hygiene: the kernel manifest's `highStakes` flags
# decide which tools require a spoken confirmation before they run. A tool that drifts out
# of the manifest loses its gate SILENTLY — it still works, it just stops asking. So a
# check that fails quietly is worse than no check, and the launchd job it replaces wrote
# its only output to a log file nobody opens.
#
# House cron rule: failures go to Sentry (project mac-ops via SENTRY_DSN_OPS), never
# Telegram. Success is silent — the rule allows a job on a machine that is not the kernel
# host to stay quiet on success, and a weekly "still converged" notification is noise.
#
# Exit codes from check_tool_drift.py:
#   0  converged                     → silent, exit 0
#   1  surfaces or manifest disagree → Sentry error (a gate may be missing)
#   2  the checker cannot evaluate   → Sentry error (a blind check looks green)
#   3  kernel tunnel down            → Sentry warning (only the manifest half was skipped)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source /Users/robinsverd/Thrivbe-AI/Thrivbe-OS/execution/scripts/sentry-notify.sh
SENTRY_SERVICE=voice-agent-tool-drift

# The venv interpreter, not system python3: /usr/bin/python3 is 3.9 and cannot import
# core/tools.py (PEP 604 unions), so the checker would fall back to AST parsing and skip
# the highStakes cross-check — green, and blind.
out="$("$HERE/.venv/bin/python" "$HERE/check_tool_drift.py" 2>&1)"; code=$?
[ "$code" -eq 0 ] && exit 0

# One line of context beats the whole dump: sentry-notify fingerprints on the first line.
detail="$(printf '%s' "$out" | grep -E 'DRIFT|drift|Error|error' | head -6)"
[ -n "$detail" ] || detail="$(printf '%s' "$out" | head -6)"

if [ "$code" -eq 3 ]; then
  sentry_warning "voice-agent tool drift: kernel unreachable, manifest half skipped
$detail"
  exit 0   # not a failure of the thing being guarded; the cross-surface half passed
fi

sentry_error "voice-agent tool drift (exit $code)
$detail"
exit "$code"
