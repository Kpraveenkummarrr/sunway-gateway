#!/usr/bin/env bash
# Generates asterisk/etc/ari.conf with a random password for the
# "ai-agent" ARI user, from ari.conf.example. The output file is
# gitignored — never commit it. Print the password once so it can be
# copied into backend/.env as ASTERISK_ARI_PASSWORD.
#
# Usage: ./generate_ari_secret.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC_DIR="$SCRIPT_DIR/../etc"
OUT_FILE="$ETC_DIR/ari.conf"

if [ -f "$OUT_FILE" ]; then
    echo "Refusing to overwrite existing $OUT_FILE (delete it first if you want a new password)." >&2
    exit 1
fi

password="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 32)"

sed "s/CHANGE_ME/$password/" "$ETC_DIR/ari.conf.example" > "$OUT_FILE"

echo "Wrote $OUT_FILE"
echo "ARI user: ai-agent"
echo "ARI password (copy into backend/.env as ASTERISK_ARI_PASSWORD, not printed again): $password"
