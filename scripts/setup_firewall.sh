#!/usr/bin/env bash
# Configures ufw for this project's exact port needs on a production VPS.
#
# Only opens: SSH, SIP (UDP 5060), and the RTP media range (UDP
# 10000-20000). Everything else this project uses — PostgreSQL (5432),
# Asterisk ARI (8088), Asterisk AMI (5038) — is already bound to
# 127.0.0.1 only (see asterisk/etc/http.conf, backend/app/core/config.py)
# and is explicitly denied here too, as defense in depth.
#
# SIP/RTP toll-fraud note: opening 5060/udp to the world lets anyone try
# to register or send SIP traffic at your Asterisk instance. If you know
# the SMG4004's IP (or your softphone testers' IPs) ahead of time, pass
# them as arguments to restrict SIP+RTP to just those sources instead of
# leaving it open to everyone:
#
#   sudo ./setup_firewall.sh 203.0.113.10 203.0.113.11
#
# With no arguments, SIP/RTP are opened to any source (0.0.0.0/0) — fine
# for initial softphone testing, NOT recommended once the SMG4004's real
# IP is known (re-run with that IP to tighten it).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (sudo ./setup_firewall.sh [trusted_ip ...])" >&2
    exit 1
fi

if ! command -v ufw >/dev/null 2>&1; then
    echo "ufw not found — install it first: apt-get install -y ufw" >&2
    exit 1
fi

echo "Allowing SSH..."
ufw allow OpenSSH

if [ "$#" -eq 0 ]; then
    echo "No trusted source IPs given — opening SIP/RTP to any source (0.0.0.0/0)."
    echo "Re-run with the SMG4004's IP once known to restrict this."
    ufw allow 5060/udp comment 'SIP - Sunway Gateway'
    ufw allow 10000:20000/udp comment 'RTP - Sunway Gateway'
else
    for ip in "$@"; do
        echo "Allowing SIP/RTP from $ip only..."
        ufw allow from "$ip" to any port 5060 proto udp comment 'SIP - Sunway Gateway'
        ufw allow from "$ip" to any port 10000:20000 proto udp comment 'RTP - Sunway Gateway'
    done
fi

echo "Explicitly denying internal-only services from external access (defense in depth — they're already bound to 127.0.0.1)..."
ufw deny 5432/tcp comment 'PostgreSQL - internal only'
ufw deny 8088/tcp comment 'Asterisk ARI - internal only'
ufw deny 5038/tcp comment 'Asterisk AMI - internal only'

echo "Enabling ufw..."
ufw --force enable

echo "Done. Current rules:"
ufw status verbose
