#!/usr/bin/env python3
"""Main Flask entrypoint for the AmneziaWG web UI."""

import os
import time
from functools import wraps

from core.helpers import to_bool
from core.runtime import (
    create_flask_app,
    create_socketio,
    register_socket_handlers,
    run_web_ui,
)
from flask import jsonify, render_template, request, send_from_directory
from routes.servers import register_server_routes
from routes.system import register_system_routes
from services.amnezia_manager import AmneziaManager

# Get the absolute path to the current directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Essential environment variables
NGINX_PORT = os.getenv("NGINX_PORT", "80")
AUTO_START_SERVERS = os.getenv("AUTO_START_SERVERS", "true").lower() == "true"
DEFAULT_MTU = int(os.getenv("DEFAULT_MTU", "1280"))
DEFAULT_SUBNET = os.getenv("DEFAULT_SUBNET", "10.0.0.0/24")
DEFAULT_PORT = int(os.getenv("DEFAULT_PORT", "51820"))
DEFAULT_DNS = os.getenv("DEFAULT_DNS", "8.8.8.8,1.1.1.1")
DEFAULT_ENABLE_NAT = os.getenv("ENABLE_NAT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
DEFAULT_BLOCK_LAN_CIDRS = os.getenv("BLOCK_LAN_CIDRS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
AWG_LOG_FILE = os.getenv("AWG_LOG_FILE", "/var/log/amnezia/amneziawg-go.log")

# Parse DNS servers from comma-separated string
DNS_SERVERS = [dns.strip() for dns in DEFAULT_DNS.split(",") if dns.strip()]

# Fixed values for other settings
WEB_UI_PORT = 5000
CONFIG_DIR = "/etc/amnezia"
WIREGUARD_CONFIG_DIR = os.path.join(CONFIG_DIR, "amneziawg")
CONFIG_FILE = os.path.join(CONFIG_DIR, "web_config.json")
ENABLE_OBFUSCATION = True
ENABLE_GEOIP = os.getenv("ENABLE_GEOIP", "1").strip().lower() not in ("0", "false", "no", "off")

# API Token Auth (optional, for defense-in-depth)
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# Socket.IO CORS origins (comma-separated list or '*' for all)
# Empty/not set = same-origin only (recommended for production)
ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "").strip()
if ALLOWED_ORIGINS_RAW == "*":
    ALLOWED_ORIGINS = "*"
elif ALLOWED_ORIGINS_RAW:
    # Parse comma-separated list and strip whitespace
    ALLOWED_ORIGINS = [
        origin.strip()
        for origin in ALLOWED_ORIGINS_RAW.split(",")
        if origin.strip()
    ]
else:
    # Default: same-origin only (let Flask-SocketIO use its default behavior)
    ALLOWED_ORIGINS = []

print(f"Base directory: {BASE_DIR}")
print(f"Template directory: {TEMPLATE_DIR}")
print(f"Static directory: {STATIC_DIR}")
# Print environment configuration for debugging
print("=== Environment Configuration ===")
print(f"NGINX_PORT: {NGINX_PORT}")
print(f"AUTO_START_SERVERS: {AUTO_START_SERVERS}")
print(f"DEFAULT_MTU: {DEFAULT_MTU}")
print(f"DEFAULT_SUBNET: {DEFAULT_SUBNET}")
print(f"DEFAULT_PORT: {DEFAULT_PORT}")
print(f"DEFAULT_DNS: {DEFAULT_DNS}")
print(f"DEFAULT_ENABLE_NAT: {DEFAULT_ENABLE_NAT}")
print(f"DEFAULT_BLOCK_LAN_CIDRS: {DEFAULT_BLOCK_LAN_CIDRS}")
print(f"DNS_SERVERS: {DNS_SERVERS}")
print("==================================")
print("Fixed Configuration:")
print(f"WEB_UI_PORT: {WEB_UI_PORT} (internal)")
print(f"CONFIG_DIR: {CONFIG_DIR}")
print(f"ENABLE_OBFUSCATION: {ENABLE_OBFUSCATION}")
print(f"API_TOKEN: {'<set>' if API_TOKEN else '<not set>'}")
print(f"ALLOWED_ORIGINS: {ALLOWED_ORIGINS if ALLOWED_ORIGINS else '<same-origin only>'}")
print("==================================")

# Check if directories exist
print(f"Templates exist: {os.path.exists(TEMPLATE_DIR)}")
print(f"Static exist: {os.path.exists(STATIC_DIR)}")
if os.path.exists(TEMPLATE_DIR):
    print(f"Template files: {os.listdir(TEMPLATE_DIR)}")
if os.path.exists(STATIC_DIR):
    print(f"Static files: {os.listdir(STATIC_DIR)}")

app = create_flask_app(TEMPLATE_DIR, STATIC_DIR)
socketio = create_socketio(app, ALLOWED_ORIGINS)


# API Token Auth decorator
def require_token(f):
    """Enforce API token auth if API_TOKEN is set (defense-in-depth with Nginx Basic Auth)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # If no API_TOKEN is configured, allow access (rely on Nginx Basic Auth)
        if not API_TOKEN:
            return f(*args, **kwargs)

        # Support either:
        # - Authorization: Bearer <token> (useful when there is no proxy auth)
        # - X-API-Token: <token>         (works alongside Nginx Basic Auth)
        token = (request.headers.get("X-API-Token") or "").strip()
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()  # Remove 'Bearer ' prefix

        if not token:
            return jsonify({
                "error": (
                    "Missing API token "
                    "(use X-API-Token header or Authorization: Bearer ...)"
                )
            }), 401

        if token != API_TOKEN:
            return jsonify({"error": "Invalid API token"}), 401

        return f(*args, **kwargs)
    return decorated


amnezia_manager = AmneziaManager(
    socketio_instance=socketio,
    auto_start_servers=AUTO_START_SERVERS,
    default_mtu=DEFAULT_MTU,
    default_subnet=DEFAULT_SUBNET,
    default_port=DEFAULT_PORT,
    dns_servers=DNS_SERVERS,
    default_enable_nat=DEFAULT_ENABLE_NAT,
    default_block_lan_cidrs=DEFAULT_BLOCK_LAN_CIDRS,
    config_dir=CONFIG_DIR,
    wireguard_config_dir=WIREGUARD_CONFIG_DIR,
    config_file=CONFIG_FILE,
    enable_obfuscation=ENABLE_OBFUSCATION,
    enable_geoip=ENABLE_GEOIP,
)

register_system_routes(
    app,
    require_token,
    amnezia_manager,
    awg_log_file=AWG_LOG_FILE,
    nginx_port=NGINX_PORT,
    auto_start_servers=AUTO_START_SERVERS,
    default_mtu=DEFAULT_MTU,
    default_subnet=DEFAULT_SUBNET,
    default_port=DEFAULT_PORT,
    default_dns=DEFAULT_DNS,
)

register_server_routes(
    app,
    require_token,
    amnezia_manager,
    to_bool=to_bool,
    default_enable_nat=DEFAULT_ENABLE_NAT,
    default_block_lan_cidrs=DEFAULT_BLOCK_LAN_CIDRS,
)

register_socket_handlers(socketio, amnezia_manager, NGINX_PORT)


# API Routes
@app.route("/")
def index():
    """Render the main single-page web UI."""
    print("Serving index.html")
    # Cache-bust static assets so browsers pick up new JS/CSS immediately.
    try:
        js_path = os.path.join(STATIC_DIR, "js", "app.js")
        css_path = os.path.join(STATIC_DIR, "css", "style.css")
        cache_bust = int(max(os.path.getmtime(js_path), os.path.getmtime(css_path)))
    except OSError:
        cache_bust = int(time.time())

    return render_template("index.html", cache_bust=cache_bust)


# Explicit static file route to ensure they're served
@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve static assets from the configured static directory."""
    return send_from_directory(STATIC_DIR, filename)


if __name__ == "__main__":
    run_web_ui(
        socketio,
        app,
        web_ui_port=WEB_UI_PORT,
        nginx_port=NGINX_PORT,
        auto_start_servers=AUTO_START_SERVERS,
        default_mtu=DEFAULT_MTU,
        default_subnet=DEFAULT_SUBNET,
        default_port=DEFAULT_PORT,
        public_ip=amnezia_manager.public_ip,
    )
