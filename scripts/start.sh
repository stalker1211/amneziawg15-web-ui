#!/bin/sh

mkdir -p /var/log/amnezia /var/log/nginx /var/log/supervisor
chmod 755 /var/log/amnezia /var/log/nginx /var/log/supervisor
chmod -R 755 /app/web-ui/

if [ -n "$NGINX_PORT" ] && [ "$NGINX_PORT" != "80" ]; then
    echo "Configuring nginx to listen on port $NGINX_PORT"
    sed -i "s/listen 80;/listen $NGINX_PORT;/g" /etc/nginx/http.d/default.conf
fi

: "${NGINX_USER:=admin}"
: "${NGINX_PASSWORD:=changeme}"
htpasswd -bc /etc/nginx/.htpasswd "$NGINX_USER" "$NGINX_PASSWORD"

nginx -t

echo "=== AmneziaWG runtime binaries ==="
echo "amneziawg-go: $(command -v amneziawg-go || echo '<missing>')"
echo "awg: $(command -v awg || echo '<missing>')"
echo "awg-quick: $(command -v awg-quick || echo '<missing>')"
echo "wg: $(command -v wg || echo '<missing>')"
echo "wg-quick: $(command -v wg-quick || echo '<missing>')"

# Version flags differ across forks/releases; try a few and never fail startup.
{ amneziawg-go --version 2>/dev/null || amneziawg-go -version 2>/dev/null || true; } | sed -n '1,3p' || true
{ awg --version 2>/dev/null || awg -v 2>/dev/null || true; } | sed -n '1,3p' || true
echo "=================================="

# Optional: enable amneziawg-go internal logs.
# Upstream uses LOG_LEVEL (debug/verbose/error/silent). When LOG_LEVEL is set,
# amneziawg-go keeps stdout/stderr attached; we additionally route output to a file
# by forcing awg-quick to use a wrapper.
if [ -n "${AWG_LOG_LEVEL:-}" ]; then
    case "${AWG_LOG_LEVEL}" in
        0|off|false|no)
            :
            ;;
        debug|verbose|error|silent)
            export LOG_LEVEL="${AWG_LOG_LEVEL}"
            export WG_QUICK_USERSPACE_IMPLEMENTATION="/usr/local/bin/amneziawg-go-logged"
            echo "AmneziaWG logs: enabled (LOG_LEVEL=${LOG_LEVEL}, file=${AWG_LOG_FILE:-/var/log/amnezia/amneziawg-go.log})"
            ;;
        *)
            echo "Warning: ignoring invalid AWG_LOG_LEVEL='${AWG_LOG_LEVEL}' (valid: debug|verbose|error|silent|off)"
            ;;
    esac
fi

# Start supervisord
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf