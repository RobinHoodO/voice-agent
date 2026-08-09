#!/usr/bin/env bash
# One command, both surfaces — so "one edit lands on both" is literally true.
#
#   ./deploy.sh                  mac + server
#   ./deploy.sh mac              rebuild + relaunch the menubar app only
#   ./deploy.sh server           ship the headless surface to Thrivbe-1 only
#   ./deploy.sh --dry-run        print exactly what would happen, change nothing
#   ./deploy.sh --ref <sha|tag|stable>   deploy a specific ref to the server
#   ./deploy.sh --ref=<sha>              same thing, = form
#
# FORCE=1 skips the live-session guard (see live_session_guard.sh).
#
# WHAT IT DOES
#
#   gates   everything that can refuse runs BEFORE either leg touches anything:
#             1. check_tool_drift.py via drift_gate.sh — refuses when the Mac and the
#                server surfaces expose different tools/prompts/high-stakes sets, or
#                when either has drifted from the kernel manifest.
#             2. live_session_guard.sh — not mid-conversation (mac leg only).
#             3. the server preconditions (dirty tree, 'origin' remote, and — unless
#                DEPLOY_PROBE=0 — that Thrivbe-1 is actually ready), but only when a
#                server leg was asked for, so `deploy.sh mac` costs no ssh round-trip.
#           A gate that fires halfway through is not a gate: it would leave the Mac
#           rebuilt and the server on the old commit, which is the divergence this
#           script exists to prevent. Robin's normal mid-edit state is a dirty tree, so
#           that refusal in particular has to come before build_app.sh, not after.
#   mac     reload_app.sh (quit → build_app.sh → relaunch) → then proves the edit
#           actually landed by diffing every core/ and mac/ .py
#           against the copy inside Thrivbe Voice.app. Editing the source dir alone
#           changes nothing; a build that silently skipped a file is a deploy that lied.
#   server  the house way, not scp: commit → push to GitHub → /opt/thrivbe-ops/deploy.sh
#           on Thrivbe-1, pinned to the exact SHA that was pushed. The old bridge deploy
#           scp'd nine files by name; anything not on that list stayed behind, which is
#           one of the ways the two surfaces drifted apart in the first place.
#
# EXIT CODES
#   0  everything asked for succeeded
#   1  refused (drift, live session, dirty tree, bad --ref, missing precondition) or a
#      leg failed
#
# The drift checker's own contract is preserved verbatim: 1 and 2 block, 3 (kernel
# tunnel down) warns and continues. See drift_gate.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# THRIVBE_VOICE_APP is an override for tests/tests-of-the-gate only; the deploy path
# always uses the bundle sitting next to this script, the one reload_app.sh just built.
APP="${THRIVBE_VOICE_APP:-$ROOT/Thrivbe Voice.app}"
BUNDLE_LIB="$APP/Contents/Resources/lib/python3.12"

# Thrivbe-1 side of the house deploy. The project must be checked out at
# /opt/Thrivbe-AI/projects/$SERVER_PROJECT for /opt/thrivbe-ops/deploy.sh to find it.
SERVER="thrivbe-1"
SERVER_PROJECT="voice-agent"
OPS_DEPLOY="/opt/thrivbe-ops/deploy.sh"

TARGET="all"
DRY=0
REF=""

while [ $# -gt 0 ]; do
  case "$1" in
    all|mac|server) TARGET="$1" ;;
    --dry-run|-n)   DRY=1 ;;
    --ref)          shift
                    [ $# -gt 0 ] || { echo "deploy.sh: --ref needs a value" >&2; exit 1; }
                    REF="$1" ;;
    --ref=*)        REF="${1#--ref=}" ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "deploy.sh: unknown argument '$1' (try --help)" >&2; exit 1 ;;
  esac
  shift
done

# A ref reaches a remote shell, and it is also the thing that makes "the server runs the
# commit I just built" true. Anything that is not plausibly a git ref is refused rather
# than quoted-and-hoped: a ref that survives quoting but is not the ref you typed still
# breaks the pinning guarantee.
if [ -n "$REF" ] && ! [[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "REFUSED: --ref '$REF' is not a git ref (allowed: letters, digits, . _ / -)." >&2
  exit 1
fi

say()  { printf '\n=== %s\n' "$*"; }

# Readable, faithful rendering of an argv — used for both the dry-run transcript and the
# '+ ' echo. Nothing here is ever fed back to a shell; run() passes argv through verbatim.
shellquote() {
  local out='' arg
  for arg in "$@"; do
    if [[ "$arg" =~ ^[A-Za-z0-9._/=:@-]+$ ]]; then
      out+="$arg "
    else
      out+="'${arg//\'/\'\\\'\'}' "
    fi
  done
  printf '%s' "${out% }"
}
would() { if [ "$DRY" = "1" ]; then printf 'DRY RUN would: %s\n' "$*"; else printf '+ %s\n' "$*"; fi; }
# No eval. argv in, argv out — an operator-supplied value can never become shell syntax.
run()  { would "$(shellquote "$@")"; [ "$DRY" = "1" ] || "$@"; }

# ---------------------------------------------------------------------------------
# Mac surface
# ---------------------------------------------------------------------------------
# The live-session guard is a GATE (see below), not a step in here: reload_app.sh asks it
# again at the moment it actually quits the app, but `deploy.sh` has to refuse before it
# has done anything on either surface.
deploy_mac() {
  say "mac surface — Thrivbe Voice.app"
  run "$ROOT/reload_app.sh"

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
#
# Both preconditions run as GATES, before any leg — see the gate section below. The
# dirty-tree refusal in particular is worthless as a step inside deploy_server(): by the
# time `deploy.sh` (target `all`) reached it, the Mac had already been quit, rebuilt and
# relaunched on code the server was about to refuse to take.
# ---------------------------------------------------------------------------------
server_precheck() {
  # Pure and local: no ssh, no network, costs nothing.
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
}

server_probe() {
  # Costs one ssh round-trip, so it only runs when a server leg was actually asked for.
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
  local branch sha ref
  branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  sha="$(git -C "$ROOT" rev-parse HEAD)"
  ref="${REF:-$sha}"
  # Push the branch so the SHA is fetchable, then deploy that exact SHA rather than
  # whatever main happens to be. The house script's default channel is main; pinning the
  # SHA is what makes "the server runs the commit I just built on the Mac" true.
  run git -C "$ROOT" push origin "$branch"
  run fleet run "$SERVER" "$OPS_DEPLOY $SERVER_PROJECT $ref"
  echo "server surface: $SERVER_PROJECT @ ${ref:0:12} (branch $branch)"
}

# ---------------------------------------------------------------------------------
# Gates — every refusal, before every leg. Read-only, so they run for real even in a
# dry run: a dry run that skipped the gates would tell you nothing about whether you
# may deploy.
# ---------------------------------------------------------------------------------
say "drift gate (both surfaces + kernel manifest)"
"$ROOT/drift_gate.sh" || exit 1

case "$TARGET" in
  all|mac) "$ROOT/live_session_guard.sh" || exit 1 ;;
esac

case "$TARGET" in
  all|server)
    say "server preconditions"
    server_precheck
    server_probe
    echo "server preconditions ok: clean tree, 'origin' present"
    ;;
esac

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
