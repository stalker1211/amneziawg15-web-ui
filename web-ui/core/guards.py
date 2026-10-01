"""Request guards: anti-CSRF for the API.

Kept out of app.py, which builds the whole app at import time (a real
AmneziaManager, /etc/amnezia), so the tests can install these exact functions on
a bare Flask app instead of re-implementing them.
"""

from flask import jsonify, request


def install_guards(app):
    """Set up the before_request guards.

    There is no app-layer credential and no session: Flask listens on 127.0.0.1 only,
    so every request, the event stream included, has already passed nginx's Basic
    Auth (DEVELOPMENT.md §6).
    """

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
