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

codesign --force --deep --sign - --identifier "$BUNDLE_ID" "$APP_NAME"
codesign --verify --deep --strict --verbose=2 "$APP_NAME"

echo "Built and signed $APP_NAME"
find "$APP_NAME/Contents/MacOS" -maxdepth 1 -type f -print -exec file {} \;
