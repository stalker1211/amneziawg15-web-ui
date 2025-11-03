#!/bin/sh

# Create necessary directories and set permissions
mkdir -p /var/log/amnezia /var/log/nginx /var/log/supervisor
chmod 755 /var/log/amnezia /var/log/nginx /var/log/supervisor
chmod -R 755 /app/web-ui/

if [ -n "$NGINX_PORT" ] && [ "$NGINX_PORT" != "80" ]; then
    echo "Configuring nginx to listen on port $NGINX_PORT"
    sed -i "s/listen 80;/listen $NGINX_PORT;/g" /etc/nginx/http.d/default.conf
fi
nginx -t

# Start supervisord
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf