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
# Equivalent to `htpasswd -bc`, without needing apache2-utils (and its vulnerable
# apr-util dependency): both emit the same $apr1$ hash that nginx auth_basic reads.
if ! hash=$(openssl passwd -apr1 "$NGINX_PASSWORD"); then
    echo "FATAL: failed to generate the nginx Basic Auth password hash" >&2
    exit 1
fi
printf '%s:%s\n' "$NGINX_USER" "$hash" > /etc/nginx/.htpasswd
# The nginx workers run as user `nginx` and must be able to read this file, so
# keep it group-readable rather than 0600/0640-root (which yields HTTP 500s).
chown root:nginx /etc/nginx/.htpasswd
chmod 640 /etc/nginx/.htpasswd

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

# The daemon's log level (AWG_LOG_LEVEL) is not exported here: the panel passes it to
# `awg-quick up` alone, so it never becomes the panel's own LOG_LEVEL.

# Start supervisord
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf