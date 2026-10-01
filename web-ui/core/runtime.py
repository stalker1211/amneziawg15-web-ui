"""Runtime wiring helpers for Flask startup."""

from flask import Flask

from core.logging_setup import get_logger

logger = get_logger(__name__)


def create_flask_app(template_dir, static_dir):
    """Create and configure the Flask application instance."""
    app = Flask(
        __name__,
        template_folder=template_dir,
        static_folder=static_dir,
    )
    return app


def run_web_ui(app, *, web_ui_port, nginx_port, public_ip):
    """Log the startup summary and run the web UI server.

    Loopback only: nginx's Basic Auth is the only gate, so nothing may reach Flask
    around it (on a macvlan network the container's own address is on the LAN).
    Werkzeug's threaded server, the one Flask-SocketIO ran until 2.5: every request is
    a thread, each open event stream (GET /api/events) included. The full
    configuration is already logged by app.py at import time.
    """
    logger.info(
        "AmneziaWG Web UI listening on 127.0.0.1:%s (behind nginx on port %s), public IP %s", web_ui_port, nginx_port, public_ip
    )
    app.run(host="127.0.0.1", port=web_ui_port, debug=False, threaded=True)
