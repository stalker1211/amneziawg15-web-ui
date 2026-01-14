#!/bin/bash

# Cleanup iptables for WireGuard interface
# Usage: cleanup_iptables.sh <interface_name> <subnet>

INTERFACE=$1
SUBNET=$2

if [ -z "$INTERFACE" ] || [ -z "$SUBNET" ]; then
    echo "Usage: $0 <interface_name> <subnet>"
    echo "Example: $0 wg0 10.8.1.0/24"
    exit 1
fi

echo "Cleaning up iptables for interface $INTERFACE with subnet $SUBNET"

# Basic validation to avoid passing unexpected strings to iptables.
if ! [[ "$INTERFACE" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
    echo "Invalid interface name: $INTERFACE" >&2
    exit 1
fi

if ! [[ "$SUBNET" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
    echo "Invalid subnet CIDR (expected IPv4/CIDR): $SUBNET" >&2
    exit 1
fi

# Remove rules in reverse order
iptables -t nat -D POSTROUTING -s "$SUBNET" -o eth+ -j MASQUERADE 2>/dev/null || true
iptables -D FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$INTERFACE" -o eth+ -s "$SUBNET" -j ACCEPT 2>/dev/null || true
iptables -D OUTPUT -o "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D INPUT -i "$INTERFACE" -j ACCEPT 2>/dev/null || true

echo "iptables rules cleaned up successfully for $INTERFACE"