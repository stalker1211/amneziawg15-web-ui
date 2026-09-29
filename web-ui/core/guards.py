"""Request guards: session secret, the socket.io auth cookie and anti-CSRF.

Kept out of app.py, which builds the whole app at import time (a real
AmneziaManager, /etc/amnezia), so the tests can install these exact functions on
a bare Flask app instead of re-implementing them.
"""

import os
from datetime import timedelta

from flask import jsonify, request, session


def load_or_create_secret_key(path):
    """Read the Flask secret key from `path`, creating it on first run.

    Persisted (rather than os.urandom() per boot) so the session cookie set by
    mark_authenticated() survives a container restart -- it is what lets a
    WebSocket reconnect stay authorized without redoing nginx's Basic Auth
    dialog, which iPadOS Safari does not reliably reattach to a WS handshake.
    """
    try:
        with open(path, "rb") as key_file:
            data = key_file.read()
            if data:
                return data
    except OSError:
        pass

    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = os.urandom(24)
    with open(path, "wb") as key_file:
        key_file.write(key)
    os.chmod(path, 0o600)
    return key


def rotate_secret_key(app, path):
    """Replace the persisted secret key, after the Basic Auth credential changed.

    Every session cookie signed with the old key stops being accepted, so a tab
    that held one cannot reconnect its WebSocket without passing the new Basic Auth.
    """
    key = os.urandom(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as key_file:
        key_file.write(key)
    os.chmod(path, 0o600)
    app.secret_key = key
    return key


def install_guards(app, *, secret_key_path):
    """Set up the session secret and the before_request guards.

    There is no app-layer credential: Flask listens on 127.0.0.1 only, so every
    request has already passed nginx's Basic Auth (DEVELOPMENT.md §6).

    The session cookie is permanent (a year): it is the only thing authorizing a
    WebSocket reconnect, so it must outlive browser restarts as Basic Auth does.
    """
    app.secret_key = load_or_create_secret_key(secret_key_path)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)

    @app.before_request
    def mark_authenticated():
        """Set a long-lived session cookie once nginx's Basic Auth has passed.

        Every request that reaches Flask already cleared nginx's Basic Auth gate, so
        this grants no new trust -- it just records that fact in a cookie, which
        browsers attach to a WebSocket upgrade handshake far more reliably than a
        cached Basic Auth credential (see register_socket_handlers in core/runtime.py).
        """
        session.permanent = True
        session.setdefault("nginx_authenticated", True)

    @app.before_request
    def require_json_for_mutations():
        """Require a JSON content-type on state-changing API requests (anti-CSRF).

        Browsers cache Basic Auth credentials per origin, so once the panel is open a
        page on another site could otherwise POST to the API and have the credentials
        attached automatically. An HTML form can only send urlencoded, text/plain or
        multipart bodies; asking for application/json means a cross-site request needs
        a CORS preflight, which is not granted. Same-origin calls from the UI are
        unaffected because ApiClient always sets this header.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if not request.path.startswith("/api/"):
            return None

        if not request.is_json:
            return jsonify(
                {
                    "error": (
                        "Content-Type: application/json is required for this request "
                        f"(got {request.headers.get('Content-Type') or 'none'})"
                    )
                }
            ), 415

        return None
