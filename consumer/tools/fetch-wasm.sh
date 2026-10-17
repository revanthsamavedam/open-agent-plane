#!/usr/bin/env bash
# Fetch the stock sql.js 1.10.3 sql-wasm.wasm into consumer/assets/ and
# verify its SHA-256 (see consumer/assets/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."
EXPECTED="d7e61b828523001f26ce0b3f88dabcf6c12e5e6edf80eb4f08b26ac7b946ff88"
URL="https://cdn.jsdelivr.net/npm/sql.js@1.10.3/dist/sql-wasm.wasm"
TMP="$(mktemp)"
curl -fsSL "$URL" -o "$TMP"
ACTUAL="$(sha256sum "$TMP" | awk '{print $1}')"
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "SHA-256 mismatch: got $ACTUAL, expected $EXPECTED" >&2
  rm -f "$TMP"; exit 1
fi
mv "$TMP" assets/sql-wasm.wasm
echo "assets/sql-wasm.wasm OK (sha256 verified)"
