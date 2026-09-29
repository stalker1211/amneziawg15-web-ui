"""Main Flask entrypoint for the AmneziaWG web UI."""

import os
import time
from urllib.parse import quote

from core.guards import install_guards
from core.helpers import to_bool
from core.logging_setup import configure_logging, get_logger
from core.runtime import (
    create_flask_app,
    create_socketio,
    register_socket_handlers,
    run_web_ui,
)
from flask import render_template, send_from_directory
from routes.servers import register_server_routes
from routes.system import register_system_routes
from services.amnezia_manager import AmneziaManager

configure_logging()
logger = get_logger(__name__)

# Get the absolute path to the current directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Build label shown under the page heading. The Dockerfile writes this file from
# --build-arg BUILD_LABEL (run.sh, publish_dockerhub.sh), so a plain `docker build`
# or a source bind-mount has none and reads "dev".
try:
    with open(os.path.join(BASE_DIR, "BUILD"), encoding="utf-8") as _fh:
        BUILD_LABEL = _fh.read().strip() or "dev"
except OSError:
    BUILD_LABEL = "dev"

# Essential environment variables
NGINX_PORT = os.getenv("NGINX_PORT", "80")
AUTO_START_SERVERS = to_bool(os.getenv("AUTO_START_SERVERS"), True)
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
SECRET_KEY_FILE = os.path.join(CONFIG_DIR, ".flask_secret_key")
ENABLE_GEOIP = os.getenv("ENABLE_GEOIP", "1").strip().lower() not in ("0", "false", "no", "off")

# Socket.IO CORS origins (comma-separated list or '*' for all)
# Empty/not set = same-origin only (recommended for production)
ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "").strip()
if ALLOWED_ORIGINS_RAW == "*":
    ALLOWED_ORIGINS = "*"
elif ALLOWED_ORIGINS_RAW:
    # Parse comma-separated list and strip whitespace
    ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS_RAW.split(",") if origin.strip()]
else:
    # Default: same-origin only (let Flask-SocketIO use its default behavior)
    ALLOWED_ORIGINS = []

logger.info("=== AmneziaWG Web UI configuration ===")
logger.info(
    "dirs: base=%s templates=%s (exists=%s) static=%s (exists=%s)",
    BASE_DIR,
    TEMPLATE_DIR,
    os.path.exists(TEMPLATE_DIR),
    STATIC_DIR,
    os.path.exists(STATIC_DIR),
)
logger.info(
    "nginx_port=%s auto_start=%s allowed_origins=%s",
    NGINX_PORT,
    AUTO_START_SERVERS,
    ALLOWED_ORIGINS if ALLOWED_ORIGINS else "<same-origin only>",
)
if os.getenv("API_TOKEN", "").strip():
    # Removed in 2.4: Flask listens on 127.0.0.1 only, so nginx's Basic Auth always
    # decides first and the token never admitted or refused anything.
    logger.warning("API_TOKEN is set but no longer used (removed in 2.4); nginx Basic Auth is the only credential")
logger.info(
    "defaults: mtu=%s subnet=%s port=%s dns=%s nat=%s block_lan=%s geoip=%s",
    DEFAULT_MTU,
    DEFAULT_SUBNET,
    DEFAULT_PORT,
    DNS_SERVERS,
    DEFAULT_ENABLE_NAT,
    DEFAULT_BLOCK_LAN_CIDRS,
    ENABLE_GEOIP,
)
logger.info("config_dir=%s web_ui_port=%s (internal)", CONFIG_DIR, WEB_UI_PORT)
logger.debug("template files: %s", os.listdir(TEMPLATE_DIR) if os.path.exists(TEMPLATE_DIR) else [])
logger.debug("static files: %s", os.listdir(STATIC_DIR) if os.path.exists(STATIC_DIR) else [])

app = create_flask_app(TEMPLATE_DIR, STATIC_DIR)
# Persisted session secret + the /socket.io/ auth cookie and the anti-CSRF JSON check
# (see core/guards.py).
install_guards(app, secret_key_path=SECRET_KEY_FILE)
socketio = create_socketio(app, ALLOWED_ORIGINS)


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
    enable_geoip=ENABLE_GEOIP,
)

register_system_routes(
    app,
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
    logger.debug("Serving index.html")
    return render_template("index.html", cache_bust=cache_bust(), build_label=BUILD_LABEL)


def cache_bust():
    """The ?v= on every script and stylesheet, so a new image is never served stale JS.

    An image built with a label gets it (the files cannot change under it); a plain
    build or a bind-mounted source ("dev") uses the newest asset's mtime instead, so
    an edit shows up on the next reload.
    """
    if BUILD_LABEL != "dev":
        return quote(BUILD_LABEL, safe="")
    newest = 0.0
    for sub in ("js", "css"):
        folder = os.path.join(STATIC_DIR, sub)
        try:
            newest = max([newest, *(os.path.getmtime(os.path.join(folder, name)) for name in os.listdir(folder))])
        except OSError:
            continue
    return str(int(newest or time.time()))


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
        public_ip=amnezia_manager.public_ip,
    )
