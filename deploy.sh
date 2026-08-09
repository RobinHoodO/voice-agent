#!/usr/bin/env bash
# One command, both surfaces — so "one edit lands on both" is literally true.
#
#   ./deploy.sh                  mac + server
#   ./deploy.sh mac              rebuild + relaunch the menubar app only
#   ./deploy.sh server           ship the headless surface to Thrivbe-1 only
#   ./deploy.sh --dry-run        print exactly what would happen, change nothing
#   ./deploy.sh --ref <sha|tag|stable>   deploy a specific ref to the server
#
# FORCE=1 skips the live-session guard (see live_session_guard.sh).
#
# WHAT IT DOES
#
#   gate    check_tool_drift.py via drift_gate.sh — refuses when the Mac and the server
#           surfaces expose different tools/prompts/high-stakes sets, or when either has
#           drifted from the kernel manifest. Runs BEFORE either leg, so a divergent
#           pair cannot reach even one machine.
#   mac     live_session_guard.sh → reload_app.sh (quit → build_app.sh → relaunch) →
#           then proves the edit actually landed by diffing every core/ and mac/ .py
#           against the copy inside Thrivbe Voice.app. Editing the source dir alone
#           changes nothing; a build that silently skipped a file is a deploy that lied.
#   server  the house way, not scp: commit → push to GitHub → /opt/thrivbe-ops/deploy.sh
#           on Thrivbe-1, pinned to the exact SHA that was pushed. The old bridge deploy
#           scp'd nine files by name; anything not on that list stayed behind, which is
#           one of the ways the two surfaces drifted apart in the first place.
#
# EXIT CODES
#   0  everything asked for succeeded
#   1  refused (drift, live session, dirty tree, missing precondition) or a leg failed
#
# The drift checker's own contract is preserved verbatim: 1 and 2 block, 3 (kernel
# tunnel down) warns and continues. See drift_gate.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/Thrivbe Voice.app"
BUNDLE_LIB="$APP/Contents/Resources/lib/python3.12"

# Thrivbe-1 side of the house deploy. The project must be checked out at
# /opt/Thrivbe-AI/projects/$SERVER_PROJECT for /opt/thrivbe-ops/deploy.sh to find it.
SERVER="thrivbe-1"
SERVER_PROJECT="voice-agent"
OPS_DEPLOY="/opt/thrivbe-ops/deploy.sh"

TARGET="all"
DRY=0
REF=""

for arg in "$@"; do
  case "$arg" in
    all|mac|server) TARGET="$arg" ;;
    --dry-run|-n)   DRY=1 ;;
    --ref=*)        REF="${arg#--ref=}" ;;
    -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "deploy.sh: unknown argument '$arg' (try --help)" >&2; exit 1 ;;
  esac
done

say()  { printf '\n=== %s\n' "$*"; }
would() { if [ "$DRY" = "1" ]; then printf 'DRY RUN would: %s\n' "$*"; else printf '+ %s\n' "$*"; fi; }
run()  { would "$*"; [ "$DRY" = "1" ] || eval "$@"; }

# ---------------------------------------------------------------------------------
# Gate — both legs, one policy. Read-only, so it runs for real even in a dry run:
# a dry run that skipped the gate would tell you nothing about whether you may deploy.
# ---------------------------------------------------------------------------------
say "drift gate (both surfaces + kernel manifest)"
"$ROOT/drift_gate.sh" || exit 1

# ---------------------------------------------------------------------------------
# Mac surface
# ---------------------------------------------------------------------------------
deploy_mac() {
  say "mac surface — Thrivbe Voice.app"
  # Asked here as well as inside reload_app.sh so that `deploy.sh` (which may go on to
  # push to GitHub) refuses before it does anything, not halfway through.
  "$ROOT/live_session_guard.sh" || exit 1
  run "'$ROOT/reload_app.sh'"

  if [ "$DRY" = "1" ]; then
    echo "DRY RUN would: verify every core/ and mac/ .py in the repo matches the bundle"
    return 0
  fi
  local stale=0
  for package in core mac; do
    while IFS= read -r relative; do
      local source="$ROOT/$package/$relative"
      local shipped="$BUNDLE_LIB/$package/$relative"
      if [ ! -f "$shipped" ]; then
        echo "NOT SHIPPED: $package/$relative is in the repo but not in the .app" >&2
        stale=1
      elif ! cmp -s "$source" "$shipped"; then
        echo "STALE: $package/$relative differs between the repo and the .app" >&2
        stale=1
      fi
    done < <(cd "$ROOT/$package" && find . -name '*.py' -not -path '*/__pycache__/*' | sed 's|^\./||' | sort)
  done
  if [ "$stale" != "0" ]; then
    echo "REFUSED: the build did not land — the running app is not this source tree." >&2
    exit 1
  fi
  echo "mac surface verified: every core/ and mac/ .py in the repo is byte-identical in the .app"
}

# ---------------------------------------------------------------------------------
# Server surface — push to GitHub, then the house deploy script on Thrivbe-1.
# ---------------------------------------------------------------------------------
server_preconditions() {
  if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    echo "REFUSED: uncommitted changes. The Mac deploys from the working tree and the" >&2
    echo "         server deploys from git — shipping now would put different code on" >&2
    echo "         the two surfaces, which is the exact failure this script prevents." >&2
    git -C "$ROOT" status --short >&2
    exit 1
  fi
  if ! git -C "$ROOT" remote get-url origin >/dev/null 2>&1; then
    echo "REFUSED: no 'origin' remote — the house deploy pulls from GitHub." >&2
    exit 1
  fi
  [ "${DEPLOY_PROBE:-1}" = "1" ] || return 0
  command -v fleet >/dev/null 2>&1 || {
    echo "REFUSED: the 'fleet' CLI is not on PATH; server ops go through it." >&2; exit 1; }
  local probe
  probe="$(fleet run "$SERVER" "test -d /opt/Thrivbe-AI/projects/$SERVER_PROJECT && echo project=ok || echo project=missing; grep -q '$SERVER_PROJECT)' $OPS_DEPLOY && echo restart=ok || echo restart=missing" 2>&1 || true)"
  echo "$probe"
  if echo "$probe" | grep -q 'project=missing'; then
    echo "REFUSED: /opt/Thrivbe-AI/projects/$SERVER_PROJECT does not exist on $SERVER." >&2
    echo "         Clone it there once, then this script is idempotent:" >&2
    echo "           fleet run $SERVER 'git -C /opt/Thrivbe-AI/projects clone <origin> $SERVER_PROJECT'" >&2
    exit 1
  fi
  if echo "$probe" | grep -q 'restart=missing'; then
    echo "WARN: $OPS_DEPLOY has no '$SERVER_PROJECT)' case, so it will pull the code and" >&2
    echo "      restart nothing. Harmless while server/ has no service; add the case when" >&2
    echo "      the headless surface gets a systemd unit." >&2
  fi
}

deploy_server() {
  say "server surface — Thrivbe-1 ($SERVER_PROJECT)"
  server_preconditions
  local branch sha ref
  branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  sha="$(git -C "$ROOT" rev-parse HEAD)"
  ref="${REF:-$sha}"
  # Push the branch so the SHA is fetchable, then deploy that exact SHA rather than
  # whatever main happens to be. The house script's default channel is main; pinning the
  # SHA is what makes "the server runs the commit I just built on the Mac" true.
  run "git -C '$ROOT' push origin '$branch'"
  run "fleet run $SERVER '$OPS_DEPLOY $SERVER_PROJECT $ref'"
  echo "server surface: $SERVER_PROJECT @ ${ref:0:12} (branch $branch)"
}

case "$TARGET" in
  mac)    deploy_mac ;;
  server) deploy_server ;;
  all)    deploy_mac; deploy_server ;;
esac

say "done"
if [ "$DRY" = "1" ]; then
  echo "(dry run — nothing was built, pushed, or restarted)"
fi
exit 0
