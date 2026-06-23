#!/bin/bash
# Build a SHIPPABLE Thrivbe Voice: py2app bundle -> Developer ID sign (hardened
# runtime) -> notarize -> staple -> DMG. This is the App-Store-alternative path;
# the agentic core can't be sandboxed for MAS (see PRODUCT.md §1,§5).
#
# Prereqs (yours to supply — the script fails loudly if missing):
#   DEVELOPER_ID   "Developer ID Application: Your Name (TEAMID)"   (codesign identity)
#   NOTARY_PROFILE name of a stored notarytool keychain profile, created once via:
#       xcrun notarytool store-credentials NOTARY_PROFILE \
#            --apple-id you@example.com --team-id TEAMID --password <app-specific-pw>
#
# Usage:  DEVELOPER_ID="Developer ID Application: … (ABCDE12345)" \
#         NOTARY_PROFILE=thrivbe ./build_release.sh
set -euo pipefail
cd "$(dirname "$0")"

APP="Thrivbe Voice.app"
DMG="ThrivbeVoice.dmg"
ENTITLEMENTS="entitlements.plist"

: "${DEVELOPER_ID:?Set DEVELOPER_ID to your 'Developer ID Application: …' codesign identity}"
: "${NOTARY_PROFILE:?Set NOTARY_PROFILE to a stored notarytool profile (see header)}"

echo "==> 1/6 Building bundle (py2app)…"
./build_app.sh                      # produces ./Thrivbe Voice.app (ad-hoc); we re-sign below

echo "==> 2/6 Signing nested code (inside-out: dylibs/.so first, then the app)…"
# Hardened runtime requires every Mach-O signed. --deep is unreliable for this, so
# sign inner binaries first, then the outer bundle.
find "$APP/Contents" \( -name "*.dylib" -o -name "*.so" \) -print0 \
  | xargs -0 -I{} codesign --force --timestamp --options runtime -s "$DEVELOPER_ID" {}
# Embedded Python framework / interpreter, if present
find "$APP/Contents" -type f -perm +111 -name "Python*" -print0 2>/dev/null \
  | xargs -0 -I{} codesign --force --timestamp --options runtime -s "$DEVELOPER_ID" {} || true

echo "==> 3/6 Signing the app with entitlements + hardened runtime…"
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" -s "$DEVELOPER_ID" "$APP"
codesign --verify --strict --verbose=2 "$APP"

echo "==> 4/6 Packaging DMG…"
rm -f "$DMG"
TMP="$(mktemp -d)"; cp -R "$APP" "$TMP/"; ln -s /Applications "$TMP/Applications"
hdiutil create -volname "Thrivbe Voice" -srcfolder "$TMP" -ov -format UDZO "$DMG"
rm -rf "$TMP"

echo "==> 5/6 Notarizing (submitting DMG, waiting)…"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait

echo "==> 6/6 Stapling the ticket…"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl -a -t open --context context:primary-signature -v "$DMG" || true

echo "✅ Done -> $DMG (signed, notarized, stapled). Ready to distribute."
