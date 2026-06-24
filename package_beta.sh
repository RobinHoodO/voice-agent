#!/bin/bash
# Package an UNSIGNED beta DMG to hand to friends (no Apple Developer account needed).
# Gatekeeper will warn on first open — INSTALL.md tells testers how to get past it.
# When you enroll in the Apple Developer Program, use build_release.sh instead
# (signed + notarized = no warnings).
set -euo pipefail
cd "$(dirname "$0")"

APP="Thrivbe Voice.app"
DMG="ThrivbeVoice-beta.dmg"

echo "==> Building bundle (py2app)…"
./build_app.sh                      # produces ./Thrivbe Voice.app (ad-hoc signed)

echo "==> Bundling install guide into the DMG…"
TMP="$(mktemp -d)"
cp -R "$APP" "$TMP/"
cp INSTALL.md "$TMP/READ ME FIRST.txt"
ln -s /Applications "$TMP/Applications"

echo "==> Creating $DMG…"
rm -f "$DMG"
hdiutil create -volname "Thrivbe Voice (beta)" -srcfolder "$TMP" -ov -format UDZO "$DMG"
rm -rf "$TMP"

echo "✅ $DMG ready. Send it to testers along with INSTALL.md."
echo "   (Unsigned: testers right-click the app → Open the first time. See INSTALL.md.)"
