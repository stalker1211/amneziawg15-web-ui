"""Panel settings API: the settings drawer (⚙ in the header).

GET  /api/settings                  values, where each comes from, the credential's state, About
POST /api/settings                  {"settings": {...partial}, "access": {current_password, user?, password?}}
POST /api/settings/restart-servers  restart the running servers (a new daemon log level needs it)

The rules live in core/settings.py; /api/validate checks the drawer as you type.
"""

import logging

from core.logging_setup import get_logger
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

logger = get_logger(__name__)


def register_settings_routes(app, amnezia_manager, access, *, build_label, rotate_secret_key):
    """`rotate_secret_key()` replaces the Flask secret key (core/guards.py) after a
    credential change, so a tab holding the old /socket.io/ cookie has to sign in again."""
    settings_bp = Blueprint("settings_routes", __name__)
    settings = amnezia_manager.settings

    @settings_bp.errorhandler(HTTPException)
    def http_error(error):
        return jsonify({"error": error.description}), error.code

    @settings_bp.errorhandler(ValueError)
    def invalid_input(error):
        return jsonify({"error": str(error)}), 400

    def first_line(argv):
        output = amnezia_manager.run_command(argv) or ""
        return output.splitlines()[0].strip() if output.strip() else None

    # Read once: they change only with the image.
    about = {
        "build_label": build_label,
        "daemon": first_line(["amneziawg-go", "--version"]),
        "tools": first_line(["awg", "--version"]),
    }

    def running_servers():
        return [s for s in amnezia_manager.config["servers"] if amnezia_manager.get_server_status(s["id"]) == "running"]

    def payload():
        return {
            **settings.payload(),
            "access": access.payload(),
            "about": about,
            "running_servers": len(running_servers()),
        }

    @settings_bp.route("/api/settings", methods=["GET"])
    def get_settings():
        return jsonify(payload())

    @settings_bp.route("/api/settings", methods=["POST"])
    def save_settings():
        """Nothing is applied unless all of it is valid, the current password included."""
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        changes = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        credential = data.get("access") if isinstance(data.get("access"), dict) else {}

        _values, errors = settings.check(changes)
        if errors:
            raise ValueError(errors[0])

        access_changed = bool(credential.get("user") is not None or credential.get("password") is not None)
        if access_changed:
            access.change(credential.get("current_password"), user=credential.get("user"), password=credential.get("password"))
            rotate_secret_key()
            logger.info("The panel's credential was changed in Settings")

        changed = settings.update(amnezia_manager.config.setdefault("settings", {}), changes)
        if changed:
            amnezia_manager.apply_settings()
            if "log_level" in changed:
                logging.getLogger().setLevel(settings.values["log_level"])
            amnezia_manager.save_config()
            logger.info("Settings changed: %s", ", ".join(changed))

        # amneziawg-go reads its level once at start: running servers keep theirs until
        # restarted, which the drawer offers.
        restart_needed = len(running_servers()) if "awg_log_level" in changed else 0
        return jsonify({**payload(), "changed": changed, "access_changed": access_changed, "restart_needed": restart_needed})

    @settings_bp.route("/api/settings/restart-servers", methods=["POST"])
    def restart_servers():
        """Restart every running server, so it picks up the daemon's log level."""
        restarted, failed = [], []
        for server in running_servers():
            ok = amnezia_manager.stop_server(server["id"]) and amnezia_manager.start_server(server["id"])
            (restarted if ok else failed).append(server["name"])
        return jsonify({"restarted": restarted, "failed": failed})

    app.register_blueprint(settings_bp)
