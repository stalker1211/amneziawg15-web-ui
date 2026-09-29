"""Runtime wiring helpers for Flask and Socket.IO startup."""

from flask import Flask, request, session
from flask_socketio import SocketIO

from core.logging_setup import get_logger

logger = get_logger(__name__)


def create_flask_app(template_dir, static_dir):
    """Create and configure the Flask application instance.

    app.secret_key is set by core/guards.py from a persisted file, not here, so the
    session cookie used to authorize WebSocket handshakes (see
    register_socket_handlers) survives a container restart.
    """
    app = Flask(
        __name__,
        template_folder=template_dir,
        static_folder=static_dir,
    )
    return app


def create_socketio(app, allowed_origins):
    """Create the Socket.IO server with optional CORS allow-list.

    async_mode="threading": request handlers and background tasks are plain threads
    and WebSockets go through simple-websocket -- no eventlet, no monkey-patching.
    """
    cors = {"cors_allowed_origins": allowed_origins} if allowed_origins else {}
    return SocketIO(app, async_mode="threading", manage_session=False, path="/socket.io", **cors)


def register_socket_handlers(socketio, amnezia_manager, nginx_port):
    """Register WebSocket connect/disconnect event handlers."""

    @socketio.on("connect")
    def handle_connect():
        # nginx does not gate /socket.io/ with Basic Auth (WebKit does not reliably
        # reattach cached Basic Auth credentials to a WS upgrade handshake, causing
        # endless re-prompts on iPadOS Safari). Authorization instead rides the
        # session cookie core/guards.py sets on any request that already cleared nginx's
        # Basic Auth, which browsers do attach to the WS handshake.
        if not session.get("nginx_authenticated"):
            logger.warning("WebSocket connect rejected (no auth cookie) from %s", request.remote_addr)
            return False

        logger.info("WebSocket connected from %s", request.remote_addr)
        # Only to the tab that connected: every other tab already has its status.
        socketio.emit(
            "status",
            {
                "message": "Connected to AmneziaWG Web UI",
                "public_ip": amnezia_manager.public_ip,
                "nginx_port": nginx_port,
                "server_port": request.environ.get("SERVER_PORT", "unknown"),
                "client_port": request.environ.get("HTTP_X_FORWARDED_PORT", "unknown"),
            },
            to=request.sid,  # pyright: ignore[reportAttributeAccessIssue]  -- set by Flask-SocketIO
        )

    @socketio.on("disconnect")
    def handle_disconnect():
        logger.info("WebSocket disconnected from %s", request.remote_addr)


def run_web_ui(socketio, app, *, web_ui_port, nginx_port, public_ip):
    """Log the startup summary and run the web UI server.

    Loopback only: nginx's Basic Auth is the only gate, so nothing may reach Flask
    around it (on a macvlan network the container's own address is on the LAN).
    The full configuration is already logged by app.py at import time.
    """
    logger.info(
        "AmneziaWG Web UI listening on 127.0.0.1:%s (behind nginx on port %s), public IP %s", web_ui_port, nginx_port, public_ip
    )
    socketio.run(app, host="127.0.0.1", port=web_ui_port, debug=False, allow_unsafe_werkzeug=True)
