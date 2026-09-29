#!/bin/sh

mkdir -p /var/log/amnezia /var/log/nginx /var/log/supervisor
chmod 755 /var/log/amnezia /var/log/nginx /var/log/supervisor
chmod -R 755 /app/web-ui/

if [ -n "$NGINX_PORT" ] && [ "$NGINX_PORT" != "80" ]; then
    echo "Configuring nginx to listen on port $NGINX_PORT"
    sed -i "s/listen 80;/listen $NGINX_PORT;/g" /etc/nginx/http.d/default.conf
fi

# The Basic Auth credential lives on the volume (/etc/amnezia/.htpasswd), where nginx
# reads it and the panel's settings drawer changes it (core/settings.py Access). One
# rule, as for every setting: NGINX_PASSWORD, when set, writes through at every boot
# (and doubles as recovery: set it, restart, remove it); otherwise the stored one
# stays; with neither it is admin/changeme, and the marker file shows the panel's
# "default password" banner until it is changed. NGINX_USER renames the stored user.
HTPASSWD=/etc/amnezia/.htpasswd
DEFAULT_MARK=/etc/amnezia/.htpasswd.default
mkdir -p /etc/amnezia

# SHA-512 crypt ($6$) with the password on stdin, never argv (ps would show it).
hash_password() {
    printf '%s' "$1" | openssl passwd -6 -stdin
}
write_htpasswd() {
    printf '%s:%s\n' "$1" "$2" > "$HTPASSWD.tmp"
    # nginx's workers run as `nginx`: a root-only file is a 500 on every request.
    chown root:nginx "$HTPASSWD.tmp"
    chmod 640 "$HTPASSWD.tmp"
    mv "$HTPASSWD.tmp" "$HTPASSWD"
}

stored_user=$(cut -d: -f1 "$HTPASSWD" 2>/dev/null)
if [ -n "${NGINX_PASSWORD:-}" ]; then
    user="${NGINX_USER:-${stored_user:-admin}}"
    if ! hash=$(hash_password "$NGINX_PASSWORD"); then
        echo "FATAL: failed to generate the nginx Basic Auth password hash" >&2
        exit 1
    fi
    write_htpasswd "$user" "$hash"
    rm -f "$DEFAULT_MARK"
    echo "Basic Auth: user '$user', password from NGINX_PASSWORD"
elif [ -s "$HTPASSWD" ]; then
    if [ -n "${NGINX_USER:-}" ] && [ "$NGINX_USER" != "$stored_user" ]; then
        write_htpasswd "$NGINX_USER" "$(cut -d: -f2- "$HTPASSWD")"
    fi
    chown root:nginx "$HTPASSWD"
    chmod 640 "$HTPASSWD"
    echo "Basic Auth: user '$(cut -d: -f1 "$HTPASSWD")', the stored password"
else
    user="${NGINX_USER:-admin}"
    write_htpasswd "$user" "$(hash_password changeme)"
    touch "$DEFAULT_MARK"
    echo "WARNING: Basic Auth is $user/changeme (no NGINX_PASSWORD, nothing stored): change it in Settings"
fi

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