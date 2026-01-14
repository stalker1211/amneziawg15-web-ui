#!/bin/bash

# Setup iptables for WireGuard interface
# Usage: setup_iptables.sh <interface_name> <subnet>

INTERFACE=$1
SUBNET=$2

if [ -z "$INTERFACE" ] || [ -z "$SUBNET" ]; then
    echo "Usage: $0 <interface_name> <subnet>"
    echo "Example: $0 wg0 10.8.1.0/24"
    exit 1
fi

echo "Setting up iptables for interface $INTERFACE with subnet $SUBNET"

# Basic validation to avoid passing unexpected strings to iptables.
# Interface names are typically like wg0 / wg-xxxx.
if ! [[ "$INTERFACE" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
    echo "Invalid interface name: $INTERFACE" >&2
    exit 1
fi

# This UI currently uses IPv4 CIDRs (e.g. 10.8.1.0/24).
if ! [[ "$SUBNET" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
    echo "Invalid subnet CIDR (expected IPv4/CIDR): $SUBNET" >&2
    exit 1
fi

# Remove any existing rules for this interface to avoid duplicates
iptables -D INPUT -i "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D OUTPUT -o "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$INTERFACE" -o eth+ -s "$SUBNET" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
iptables -t nat -D POSTROUTING -s "$SUBNET" -o eth+ -j MASQUERADE 2>/dev/null || true

# Allow traffic on the TUN interface
iptables -A INPUT -i "$INTERFACE" -j ACCEPT
# Drop traffic from $SUBNET to 192.168.0.0/16
iptables -A FORWARD -s "$SUBNET" -d 192.168.0.0/16 -j DROP
iptables -A OUTPUT -o "$INTERFACE" -j ACCEPT

# Allow forwarding traffic only from the VPN
# Accept traffic from wg to eth+ from $SUBNET to NOT 192.168.0.0/16
iptables -A FORWARD -i "$INTERFACE" -o eth+ -s "$SUBNET" ! -d 192.168.0.0/16 -j ACCEPT

# Allow established and related connections
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

# Enable NAT for VPN traffic
if [ -z "${ENABLE_NAT:-}" ] || [ "${ENABLE_NAT:-}" = "1" ]; then
    iptables -t nat -A POSTROUTING -s "$SUBNET" -o eth+ -j MASQUERADE
    echo "NAT enabled for subnet $SUBNET"
else
    echo "NAT not enabled as per configuration"
fi

echo "iptables rules set up successfully for $INTERFACE"
