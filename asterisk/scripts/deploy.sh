#!/usr/bin/env bash
# Deploys this repo's asterisk/etc/ configuration into the live
# /etc/asterisk/ directory, backing up whatever is already there first.
#
# Usage: sudo ./deploy.sh
set -euo pipefail

ASTERISK_ETC=/etc/asterisk
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ETC="$SCRIPT_DIR/../etc"
RECORDING_DIR=/var/spool/asterisk/recordings

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (sudo ./deploy.sh) — needs to write to $ASTERISK_ETC." >&2
    exit 1
fi

if [ ! -d "$ASTERISK_ETC" ]; then
    echo "$ASTERISK_ETC does not exist — is Asterisk installed?" >&2
    exit 1
fi

if [ ! -f "$REPO_ETC/pjsip/pjsip_auth.conf" ]; then
    echo "Missing $REPO_ETC/pjsip/pjsip_auth.conf — run generate_sip_secrets.sh first." >&2
    exit 1
fi

BACKUP_DIR="/var/backups/asterisk-config-$(date +%Y%m%dT%H%M%S)"
echo "Backing up existing $ASTERISK_ETC to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp -a "$ASTERISK_ETC/." "$BACKUP_DIR/"

echo "Deploying PJSIP config..."
cp "$REPO_ETC"/pjsip/pjsip.conf "$ASTERISK_ETC/pjsip.conf"
cp "$REPO_ETC"/pjsip/pjsip_transport.conf "$ASTERISK_ETC/pjsip_transport.conf"
cp "$REPO_ETC"/pjsip/pjsip_extensions.conf "$ASTERISK_ETC/pjsip_extensions.conf"
cp "$REPO_ETC"/pjsip/pjsip_auth.conf "$ASTERISK_ETC/pjsip_auth.conf"
chmod 640 "$ASTERISK_ETC/pjsip_auth.conf"
chown root:asterisk "$ASTERISK_ETC/pjsip_auth.conf" 2>/dev/null || true

echo "Deploying dialplan..."
cp "$REPO_ETC"/dialplan/extensions.conf "$ASTERISK_ETC/extensions.conf"
cp "$REPO_ETC"/dialplan/internal.conf "$ASTERISK_ETC/internal.conf"
cp "$REPO_ETC"/dialplan/ivr.conf "$ASTERISK_ETC/ivr.conf"
cp "$REPO_ETC"/dialplan/recording.conf "$ASTERISK_ETC/recording.conf"

echo "Deploying rtp.conf..."
cp "$REPO_ETC"/rtp.conf "$ASTERISK_ETC/rtp.conf"

echo "Deploying logger.conf..."
cp "$REPO_ETC"/logger.conf "$ASTERISK_ETC/logger.conf"

# This project uses PJSIP (chan_pjsip) exclusively. The legacy chan_sip
# driver ships enabled by default and binds UDP 5060 too, which conflicts
# with our PJSIP transport — disable it rather than run both stacks.
if ! grep -q '^noload => chan_sip.so' "$ASTERISK_ETC/modules.conf"; then
    echo "Disabling legacy chan_sip.so in modules.conf (conflicts with PJSIP on UDP 5060)..."
    echo "noload => chan_sip.so" >> "$ASTERISK_ETC/modules.conf"
fi
asterisk -rx "module unload chan_sip.so" || true

echo "Setting up recording directory ($RECORDING_DIR)..."
mkdir -p "$RECORDING_DIR"
chown -R asterisk:asterisk "$RECORDING_DIR" 2>/dev/null || true
chmod 750 "$RECORDING_DIR"

echo "Reloading Asterisk..."
asterisk -rx "module reload res_pjsip.so" || true
asterisk -rx "dialplan reload" || true
asterisk -rx "pjsip reload" || true
asterisk -rx "logger reload" || true

echo "Done. Verify with: asterisk -rx 'pjsip show endpoints' and 'dialplan show internal'"
