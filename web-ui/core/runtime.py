"""Runtime wiring helpers for Flask and Socket.IO startup."""

import os
from flask import Flask, request
from flask_socketio import SocketIO


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
        print(f"WebSocket connected from {request.remote_addr}")

        socketio.emit('status', {
            'message': 'Connected to AmneziaWG Web UI',
            'public_ip': amnezia_manager.public_ip,
            'nginx_port': nginx_port,
            'server_port': request.environ.get('SERVER_PORT', 'unknown'),
            'client_port': request.environ.get('HTTP_X_FORWARDED_PORT', 'unknown')
        })

    @socketio.on('disconnect')
    def handle_disconnect():
        print(f"WebSocket disconnected from {request.remote_addr}")


# pylint: disable=too-many-arguments

def run_web_ui(socketio, app, *, web_ui_port, nginx_port, auto_start_servers,
                default_mtu, default_subnet, default_port, public_ip):
    """Log startup parameters and run the web UI server."""
    print("AmneziaWG Web UI starting...")
    print("Configuration:")
    print(f"  NGINX Port: {nginx_port}")
    print(f"  Auto-start: {auto_start_servers}")
    print(f"  Default MTU: {default_mtu}")
    print(f"  Default Subnet: {default_subnet}")
    print(f"  Default Port: {default_port}")
    print(f"Detected public IP: {public_ip}")

    if auto_start_servers:
        print("Auto-starting existing servers...")

    socketio.run(app, host='0.0.0.0', port=web_ui_port, debug=False, allow_unsafe_werkzeug=True)
