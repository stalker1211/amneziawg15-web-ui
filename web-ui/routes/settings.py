"""Panel settings API: the settings drawer (⚙ in the header).

GET  /api/settings                  values, where each comes from, the credential's state, About,
                                    how the GeoIP source has done (geoip_status)
POST /api/settings                  {"settings": {...partial}, "access": {current_password, user?, password?}}
POST /api/settings/restart-servers  restart the running servers (a new daemon log level needs it)
POST /api/settings/geoip-test       {"provider"}: ask it, as typed, where the public IP is; saves nothing

The rules live in core/settings.py; /api/validate checks the drawer as you type.
"""

import logging

from core.logging_setup import get_logger
from core.settings import FIELDS, NOT_LOGGED
from flask import Blueprint, jsonify, request
from services.activity import field_changes
from werkzeug.exceptions import HTTPException

logger = get_logger(__name__)


def register_settings_routes(app, amnezia_manager, access, *, build_label):
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
            "geoip_status": amnezia_manager.netinfo.geoip_status(),
        }

    @settings_bp.route("/api/settings", methods=["GET"])
    def get_settings():
        return jsonify(payload())

    @settings_bp.route("/api/settings", methods=["POST"])
    def save_settings():
        """Nothing is applied unless all of it is valid, the current password included."""
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        changes, credential = data.get("settings"), data.get("access")
        changes = changes if isinstance(changes, dict) else {}
        credential = credential if isinstance(credential, dict) else {}

        _values, errors = settings.check(changes)
        if errors:
            raise ValueError(errors[0])

        access_changed = bool(credential.get("user") is not None or credential.get("password") is not None)
        if access_changed:
            old_user = access.user()
            access.change(credential.get("current_password"), user=credential.get("user"), password=credential.get("password"))
            logger.info("The panel's credential was changed in Settings")
            # Whether the name changed, never what to: the user name is half the credential.
            amnezia_manager.activity.record("change", "access.change", detail={"user_changed": access.user() != old_user})

        old_values = dict(settings.values)
        changed = settings.update(amnezia_manager.config.setdefault("settings", {}), changes)
        if changed:
            # Each setting's old and new value, but the provider URL only by name.
            changes = field_changes(old_values, settings.values, fields=changed, values=frozenset(FIELDS) - NOT_LOGGED)
            amnezia_manager.activity.change("settings.save", changes=changes)
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

    @settings_bp.route("/api/settings/geoip-test", methods=["POST"])
    def geoip_test():
        """Ask a GeoIP provider as the drawer has it, saved or not ("" for ipapi.co),
        where this server's public IP is: once, without falling back. Nothing is saved,
        cached or counted; a URL the setting would refuse is a 400, with its reason."""
        data = request.get_json(silent=True)
        provider = data.get("provider") if isinstance(data, dict) else None
        _env, parse, _default = FIELDS["geoip_provider"]
        url = parse(provider or "")
        return jsonify(amnezia_manager.netinfo.check_geoip_source(url, amnezia_manager.public_ip))

    app.register_blueprint(settings_bp)
