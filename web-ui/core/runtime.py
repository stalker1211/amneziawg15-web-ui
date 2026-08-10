"""Runtime wiring helpers for Flask and Socket.IO startup."""

import os
from flask import Flask, request
from flask_socketio import SocketIO
from core.logging_setup import get_logger

logger = get_logger(__name__)


def create_flask_app(template_dir, static_dir):
    """Create and configure the Flask application instance."""
    app = Flask(
        __name__,
        template_folder=template_dir,
        static_folder=static_dir,
    )
    app.secret_key = os.urandom(24)
    return app


def create_socketio(app, allowed_origins):
    """Create the Socket.IO server with optional CORS allow-list."""
    if allowed_origins:
        return SocketIO(
            app,
            async_mode='eventlet',
            manage_session=False,
            cors_allowed_origins=allowed_origins,
            path='/socket.io'
        )

    return SocketIO(
        app,
        async_mode='eventlet',
        manage_session=False,
        path='/socket.io'
    )


def register_socket_handlers(socketio, amnezia_manager, nginx_port):
    """Register WebSocket connect/disconnect event handlers."""
    @socketio.on('connect')
    def handle_connect():
        logger.info("WebSocket connected from %s", request.remote_addr)
        socketio.emit('status', {
            'message': 'Connected to AmneziaWG Web UI',
            'public_ip': amnezia_manager.public_ip,
            'nginx_port': nginx_port,
            'server_port': request.environ.get('SERVER_PORT', 'unknown'),
            'client_port': request.environ.get('HTTP_X_FORWARDED_PORT', 'unknown')
        })

    @socketio.on('disconnect')
    def handle_disconnect():
        logger.info("WebSocket disconnected from %s", request.remote_addr)


# pylint: disable=too-many-arguments

def run_web_ui(socketio, app, *, web_ui_port, nginx_port, auto_start_servers,
                default_mtu, default_subnet, default_port, public_ip):
    """Log the startup summary and run the web UI server.

    The full configuration is already logged by app.py at import time; this only
    records what is specific to actually serving.
    """
    logger.info("AmneziaWG Web UI listening on 0.0.0.0:%s (behind nginx on port %s), public IP %s",
                web_ui_port, nginx_port, public_ip)
    del auto_start_servers, default_mtu, default_subnet, default_port  # logged in app.py
    socketio.run(app, host='0.0.0.0', port=web_ui_port, debug=False, allow_unsafe_werkzeug=True)
