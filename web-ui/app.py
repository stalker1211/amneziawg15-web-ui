"""Main Flask entrypoint for the AmneziaWG web UI."""

import logging
import os
import time
from urllib.parse import quote

from core.guards import install_guards, rotate_secret_key
from core.helpers import to_bool
from core.logging_setup import configure_logging, get_logger
from core.runtime import (
    create_flask_app,
    create_socketio,
    register_socket_handlers,
    run_web_ui,
)
from core.settings import Access, Settings
from flask import render_template, send_from_directory
from routes.servers import register_server_routes
from routes.settings import register_settings_routes
from routes.system import page_config, register_system_routes
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

# Deployment-only environment variables. Everything else is a panel setting
# (core/settings.py): stored in web_config.json, pinned while its variable is set.
NGINX_PORT = os.getenv("NGINX_PORT", "80")
AWG_LOG_FILE = os.getenv("AWG_LOG_FILE", "/var/log/amnezia/amneziawg-go.log")

# Fixed values for other settings
WEB_UI_PORT = 5000
CONFIG_DIR = "/etc/amnezia"
WIREGUARD_CONFIG_DIR = os.path.join(CONFIG_DIR, "amneziawg")
CONFIG_FILE = os.path.join(CONFIG_DIR, "web_config.json")
SECRET_KEY_FILE = os.path.join(CONFIG_DIR, ".flask_secret_key")
# Written by scripts/start.sh; nginx reads the credential from the volume.
HTPASSWD_FILE = os.path.join(CONFIG_DIR, ".htpasswd")
DEFAULT_PASSWORD_MARKER = os.path.join(CONFIG_DIR, ".htpasswd.default")

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

# Retired in 2.4: boot restores each server's last start/stop state. For a while a
# false value still means "start nothing at boot".
AUTO_START_RAW = os.getenv("AUTO_START_SERVERS", "").strip()
RESTORE_AT_BOOT = to_bool(AUTO_START_RAW, True)

logger.info("=== AmneziaWG Web UI configuration ===")
logger.info(
    "dirs: base=%s templates=%s (exists=%s) static=%s (exists=%s)",
    BASE_DIR,
    TEMPLATE_DIR,
    os.path.exists(TEMPLATE_DIR),
    STATIC_DIR,
    os.path.exists(STATIC_DIR),
)
logger.info("nginx_port=%s allowed_origins=%s", NGINX_PORT, ALLOWED_ORIGINS if ALLOWED_ORIGINS else "<same-origin only>")
if AUTO_START_RAW:
    logger.warning(
        "AUTO_START_SERVERS is retired: servers come back as they were last left%s",
        "" if RESTORE_AT_BOOT else "; set to false, so none is started at boot",
    )
if os.getenv("API_TOKEN", "").strip():
    # Removed in 2.4: Flask listens on 127.0.0.1 only, so nginx's Basic Auth always
    # decides first and the token never admitted or refused anything.
    logger.warning("API_TOKEN is set but no longer used (removed in 2.4); nginx Basic Auth is the only credential")
logger.info("config_dir=%s web_ui_port=%s (internal)", CONFIG_DIR, WEB_UI_PORT)
logger.debug("template files: %s", os.listdir(TEMPLATE_DIR) if os.path.exists(TEMPLATE_DIR) else [])
logger.debug("static files: %s", os.listdir(STATIC_DIR) if os.path.exists(STATIC_DIR) else [])

app = create_flask_app(TEMPLATE_DIR, STATIC_DIR)
# Persisted session secret + the /socket.io/ auth cookie and the anti-CSRF JSON check
# (see core/guards.py).
install_guards(app, secret_key_path=SECRET_KEY_FILE)
socketio = create_socketio(app, ALLOWED_ORIGINS)

settings = Settings()
access = Access(HTPASSWD_FILE, DEFAULT_PASSWORD_MARKER)

# The defaults below are replaced by the resolved settings as the manager loads them.
amnezia_manager = AmneziaManager(
    socketio_instance=socketio,
    auto_start_servers=RESTORE_AT_BOOT,
    default_mtu=1280,
    default_subnet="10.0.0.0/24",
    default_port=51820,
    dns_servers=["8.8.8.8", "1.1.1.1"],
    default_enable_nat=True,
    default_block_lan_cidrs=True,
    config_dir=CONFIG_DIR,
    wireguard_config_dir=WIREGUARD_CONFIG_DIR,
    config_file=CONFIG_FILE,
    settings=settings,
)
logging.getLogger().setLevel(settings.values["log_level"])
logger.info("settings: %s", ", ".join(f"{k}={v} ({settings.sources[k]})" for k, v in settings.values.items()))
if access.is_default():
    logger.warning("The panel is on the default password (%s/changeme): change it in Settings", access.user())

register_system_routes(app, amnezia_manager, awg_log_file=AWG_LOG_FILE, nginx_port=NGINX_PORT)
register_server_routes(app, amnezia_manager, to_bool=to_bool)
register_settings_routes(
    app,
    amnezia_manager,
    access,
    build_label=BUILD_LABEL,
    rotate_secret_key=lambda: rotate_secret_key(app, SECRET_KEY_FILE),
)

register_socket_handlers(socketio, amnezia_manager, NGINX_PORT)


# API Routes
@app.route("/")
def index():
    """Render the main single-page web UI."""
    logger.debug("Serving index.html")
    return render_template(
        "index.html", cache_bust=cache_bust(), build_label=BUILD_LABEL, app_config=page_config(amnezia_manager, access)
    )


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
