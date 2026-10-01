"""Main Flask entrypoint for the AmneziaWG web UI."""

import logging
import os
import time
from urllib.parse import quote

from core.events import EventBroadcaster
from core.guards import install_guards
from core.helpers import to_bool
from core.logging_setup import configure_logging, get_logger
from core.runtime import create_flask_app, run_web_ui
from core.settings import Access, Settings, retired_variables
from flask import send_from_directory
from routes.servers import register_server_routes
from routes.settings import register_settings_routes
from routes.system import register_system_routes, render_page
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
# Written by scripts/start.sh; nginx reads the credential from the volume.
HTPASSWD_FILE = os.path.join(CONFIG_DIR, ".htpasswd")
# Signed the /socket.io/ session cookie until 2.5; a volume from before still has it.
RETIRED_SECRET_KEY_FILE = os.path.join(CONFIG_DIR, ".flask_secret_key")

logger.info("=== AmneziaWG Web UI configuration ===")
logger.info(
    "dirs: base=%s templates=%s (exists=%s) static=%s (exists=%s)",
    BASE_DIR,
    TEMPLATE_DIR,
    os.path.exists(TEMPLATE_DIR),
    STATIC_DIR,
    os.path.exists(STATIC_DIR),
)
logger.info("nginx_port=%s", NGINX_PORT)
for name, note in retired_variables():
    logger.warning("%s is set but no longer used (%s)", name, note)
if os.path.exists(RETIRED_SECRET_KEY_FILE):
    try:
        os.remove(RETIRED_SECRET_KEY_FILE)
        logger.info("Removed %s: the session cookie it signed went in 2.5", RETIRED_SECRET_KEY_FILE)
    except OSError as e:
        logger.warning("Could not remove %s (no longer used): %s", RETIRED_SECRET_KEY_FILE, e)
logger.info("config_dir=%s web_ui_port=%s (internal)", CONFIG_DIR, WEB_UI_PORT)
logger.debug("template files: %s", os.listdir(TEMPLATE_DIR) if os.path.exists(TEMPLATE_DIR) else [])
logger.debug("static files: %s", os.listdir(STATIC_DIR) if os.path.exists(STATIC_DIR) else [])

app = create_flask_app(TEMPLATE_DIR, STATIC_DIR)
# The anti-CSRF JSON check (see core/guards.py).
install_guards(app)
# Live updates for the page, served on GET /api/events (see core/events.py).
events = EventBroadcaster()

settings = Settings()
access = Access(HTPASSWD_FILE)

# The built-in new-server values: the form's first server (page_config), and what an
# API call that leaves a field out gets. Later servers start from the newest one.
amnezia_manager = AmneziaManager(
    events=events,
    auto_start_servers=True,
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
register_settings_routes(app, amnezia_manager, access, build_label=BUILD_LABEL)


# API Routes
@app.route("/")
def index():
    """Render the main single-page web UI."""
    logger.debug("Serving index.html")
    return render_page(amnezia_manager, access, cache_bust=cache_bust(), build_label=BUILD_LABEL)


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
        app,
        web_ui_port=WEB_UI_PORT,
        nginx_port=NGINX_PORT,
        public_ip=amnezia_manager.public_ip,
    )
