#!/bin/sh
set -eu

LOG_FILE="${AWG_LOG_FILE:-/var/log/amnezia/amneziawg-go.log}"
LOG_LEVEL_VALUE="${LOG_LEVEL:-}"

mkdir -p "$(dirname "$LOG_FILE")"

# Ensure log file exists and is writable.
touch "$LOG_FILE" 2>/dev/null || true

{
	printf '%s [amneziawg-go-logged] starting: %s\n' "$(date -Iseconds)" "$*"
	if [ -n "$LOG_LEVEL_VALUE" ]; then
		printf '%s [amneziawg-go-logged] LOG_LEVEL=%s\n' "$(date -Iseconds)" "$LOG_LEVEL_VALUE"
	fi
} >>"$LOG_FILE" 2>/dev/null || true

exec /usr/bin/amneziawg-go "$@" >>"$LOG_FILE" 2>&1
