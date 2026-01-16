#!/bin/bash

# Setup iptables for WireGuard interface
# Usage: setup_iptables.sh <interface_name> <subnet>

INTERFACE=$1
SUBNET=$2

# Detect WAN interface (default route) and configure LAN blocking.
WAN_IF=${WAN_IF:-$(ip route show default 2>/dev/null | awk '{print $5}' | head -n1)}
# BLOCK_LAN_CIDRS: 1=block VPN -> private LAN ranges, 0=allow.
BLOCK_LAN_CIDRS=${BLOCK_LAN_CIDRS:-1}

if [ -z "$INTERFACE" ] || [ -z "$SUBNET" ]; then
    echo "Usage: $0 <interface_name> <subnet>"
    echo "Example: $0 wg0 10.8.1.0/24"
    exit 1
fi

if [ -z "$WAN_IF" ]; then
    echo "Failed to detect WAN interface (set WAN_IF env var)" >&2
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

if ! [[ "$WAN_IF" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
    echo "Invalid WAN interface name: $WAN_IF" >&2
    exit 1
fi

# Remove any existing rules for this interface to avoid duplicates
iptables -D INPUT -i "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D OUTPUT -o "$INTERFACE" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$INTERFACE" -o "$WAN_IF" -s "$SUBNET" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
iptables -t nat -D POSTROUTING -s "$SUBNET" -o eth+ -j MASQUERADE 2>/dev/null || true

# Remove any existing LAN-block rules
iptables -D FORWARD -s "$SUBNET" -d 192.168.0.0/16 -j DROP 2>/dev/null || true
iptables -D FORWARD -s "$SUBNET" -d 10.0.0.0/8 -j DROP 2>/dev/null || true
iptables -D FORWARD -s "$SUBNET" -d 172.16.0.0/12 -j DROP 2>/dev/null || true

# Allow traffic on the TUN interface
iptables -A INPUT -i "$INTERFACE" -j ACCEPT
# Drop traffic from $SUBNET to private LAN ranges (optional)
if [ "$BLOCK_LAN_CIDRS" = "1" ]; then
    iptables -A FORWARD -s "$SUBNET" -d 192.168.0.0/16 -j DROP
    iptables -A FORWARD -s "$SUBNET" -d 10.0.0.0/8 -j DROP
    iptables -A FORWARD -s "$SUBNET" -d 172.16.0.0/12 -j DROP
fi
iptables -A OUTPUT -o "$INTERFACE" -j ACCEPT

# Allow forwarding traffic only from the VPN
iptables -A FORWARD -i "$INTERFACE" -o "$WAN_IF" -s "$SUBNET" -j ACCEPT

# Allow established and related connections
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

# Enable NAT for VPN traffic
if [ -z "${ENABLE_NAT:-}" ] || [ "${ENABLE_NAT:-}" = "1" ]; then
    iptables -t nat -A POSTROUTING -s "$SUBNET" -o "$WAN_IF" -j MASQUERADE
    echo "NAT enabled for subnet $SUBNET"
else
    echo "NAT not enabled as per configuration"
fi

echo "iptables rules set up successfully for $INTERFACE"
