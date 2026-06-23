#!/bin/bash
# Launch the self-contained Thrivbe voice agent app bundle.
cd "$(dirname "$0")"
if [ ! -d "Thrivbe Voice.app" ]; then
  echo "Thrivbe Voice.app is missing. Run ./build_app.sh first."
  exit 1
fi
if [ -f "Thrivbe Voice.app/Contents/MacOS/launcher.c" ]; then
  echo "Thrivbe Voice.app is the old external-Python launcher. Run ./build_app.sh first."
  exit 1
fi
exec /usr/bin/open -n "$(pwd)/Thrivbe Voice.app" "$@"
