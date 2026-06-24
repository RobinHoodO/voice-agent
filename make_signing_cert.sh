#!/bin/bash
# One-time: create a STABLE self-signed code-signing identity in the login keychain.
#
# Why: build_app.sh used ad-hoc signing (`--sign -`), whose cdhash is content-derived
# and changes on every build — so macOS sees a "new" app each rebuild and DROPS the
# Input Monitoring / Accessibility (TCC) grants. A stable identity gives a constant
# designated requirement, so TCC grants survive every future rebuild. No Apple account
# needed (Gatekeeper still won't trust it for distribution — that's fine for local dev).
set -euo pipefail

IDENTITY="Thrivbe Voice Dev"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

# A self-signed cert isn't valid for the "codesigning" trust POLICY, so it never shows
# in `find-identity -p codesigning` — yet `codesign --sign` finds it by name and signs
# fine. So detect existence by the cert's common name, not the policy listing.
if security find-certificate -c "$IDENTITY" "$KEYCHAIN" >/dev/null 2>&1; then
  echo "Signing identity '$IDENTITY' already present — nothing to do."
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/cert.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions    = ext
prompt             = no
[ dn ]
CN = $IDENTITY
[ ext ]
basicConstraints   = critical, CA:false
keyUsage           = critical, digitalSignature
extendedKeyUsage   = critical, codeSigning
EOF

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/cert.cnf" >/dev/null 2>&1

# Apple's `security import` rejects (a) OpenSSL 3's default SHA-256 PKCS12 MAC and
# (b) an EMPTY-password p12. So: legacy ciphers (3DES/RC2) + a SHA-1 MAC + a throwaway
# password. The p12 is deleted immediately (TMP trap), so this password guards nothing.
P12PASS="thrivbe-voice-dev"
openssl pkcs12 -export -legacy -macalg sha1 -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -name "$IDENTITY" -out "$TMP/id.p12" -passout "pass:$P12PASS" >/dev/null 2>&1

# -A: let any app (incl. codesign) use the private key without a keychain prompt.
security import "$TMP/id.p12" -k "$KEYCHAIN" -P "$P12PASS" -A -T /usr/bin/codesign

echo "Created and imported '$IDENTITY' into the login keychain."
echo "build_app.sh will now sign with it; TCC grants will survive rebuilds."
