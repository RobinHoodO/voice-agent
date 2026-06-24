#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON312="/opt/homebrew/bin/python3.12"
APP_NAME="Thrivbe Voice.app"
BUNDLE_ID="com.thrivbe.voice-agent"

if [ ! -x "$PYTHON312" ]; then
  echo "Missing $PYTHON312"
  exit 1
fi

if [ ! -x ".venv/bin/python" ] || [ "$(.venv/bin/python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null || true)" != "3.12" ]; then
  if [ -d ".venv" ]; then
    mv ".venv" ".venv.backup.$(date +%Y%m%d%H%M%S)"
  fi
  "$PYTHON312" -m venv ".venv"
fi

".venv/bin/python" -m pip install --upgrade pip setuptools wheel
".venv/bin/python" -m pip install -r requirements.txt
".venv/bin/python" setup.py py2app

if [ -d "$APP_NAME" ]; then
  rm -rf "$APP_NAME"
fi
cp -R "dist/$APP_NAME" "$APP_NAME"

# Sign with the stable self-signed identity if present, so the designated requirement
# (and thus TCC grants for Input Monitoring / Accessibility) survive every rebuild.
# Falls back to ad-hoc (which breaks grants on each rebuild) if it isn't set up yet.
SIGN_IDENTITY="Thrivbe Voice Dev"
if security find-certificate -c "$SIGN_IDENTITY" >/dev/null 2>&1; then
  SIGN_AS="$SIGN_IDENTITY"
else
  echo "note: '$SIGN_IDENTITY' not found — run ./make_signing_cert.sh once so TCC grants survive rebuilds. Falling back to ad-hoc signing."
  SIGN_AS="-"
fi
codesign --force --deep --sign "$SIGN_AS" --identifier "$BUNDLE_ID" "$APP_NAME"
codesign --verify --deep --strict --verbose=2 "$APP_NAME"

echo "Built and signed $APP_NAME"
find "$APP_NAME/Contents/MacOS" -maxdepth 1 -type f -print -exec file {} \;
