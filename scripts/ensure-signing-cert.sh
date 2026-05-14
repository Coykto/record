#!/usr/bin/env bash
#
# Ensure a stable, self-signed code-signing certificate exists for the record
# capture binary, creating it (idempotently) in the login keychain if absent.
#
# Why this exists: macOS TCC keys permission grants (Microphone, Screen
# Recording, Accessibility) on the *signing identity* of the requesting code.
# An ad-hoc signature (`codesign -s -`) has no stable identity — its cdhash
# changes on every rebuild — so every `make swift` would orphan the Screen
# Recording and Accessibility grants and force the user to re-toggle them in
# System Settings. Signing with a fixed self-signed certificate gives a
# stable LOCAL identity, so TCC grants persist across rebuilds.
#
# This is deliberately NOT Developer ID / notarization: it produces an
# untrusted-but-stable identity, which is all TCC needs. Gatekeeper trust for
# distribution to other machines is a separate, deferred concern.
#
# Idempotent: a no-op when the certificate already exists.

set -euo pipefail

CERT_NAME="Record Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-certificate -c "$CERT_NAME" "$KEYCHAIN" >/dev/null 2>&1; then
    echo "signing cert '$CERT_NAME' already present — nothing to do"
    exit 0
fi

echo "creating self-signed code-signing certificate '$CERT_NAME'..."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Key in *traditional* RSA PEM format — macOS `security import -f openssl`
# rejects OpenSSL 3's default PKCS#8 output. (And PKCS#12 bundles from
# OpenSSL 3 fail macOS's MAC verification, so we import key + cert separately
# rather than bundling.)
openssl genrsa -traditional -out "$work/key.pem" 2048 >/dev/null 2>&1

# Self-signed leaf with the codeSigning extended key usage — the minimum
# `codesign` needs to accept it as a signing identity.
openssl req -x509 -new -key "$work/key.pem" -sha256 -days 3650 \
    -out "$work/cert.pem" \
    -subj "/CN=$CERT_NAME" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning" \
    >/dev/null 2>&1

# Import key and cert separately into the login keychain. `-T /usr/bin/codesign`
# puts codesign on the private key's access control list so signing doesn't
# prompt on every build.
security import "$work/key.pem" -f openssl -k "$KEYCHAIN" -T /usr/bin/codesign \
    >/dev/null
security import "$work/cert.pem" -f x509 -k "$KEYCHAIN" -T /usr/bin/codesign \
    >/dev/null

echo "signing cert '$CERT_NAME' created and imported into the login keychain"
echo "note: the first signed build may show a one-time keychain prompt —"
echo "      click 'Always Allow' to silence it for future builds"
