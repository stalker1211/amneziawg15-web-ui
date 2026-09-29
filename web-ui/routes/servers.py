"""Server and client management API routes."""

import io
import os
import re

from core.logging_setup import get_logger
from flask import Blueprint, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

# pylint: disable=too-many-arguments,too-many-locals,too-many-statements

logger = get_logger(__name__)


def register_server_routes(
    app,
    amnezia_manager,
    *,
    to_bool,
    default_enable_nat,
    default_block_lan_cidrs,
):
    """Register server/client management routes on the Flask app."""
    server_bp = Blueprint("server_routes", __name__)

    # --- errors: every failure leaves as {"error": ...} JSON --------------------

    @server_bp.errorhandler(HTTPException)
    def http_error(error):
        return jsonify({"error": error.description}), error.code

    @server_bp.errorhandler(ValueError)
    def invalid_input(error):
        # Validators raise ValueError; that is the caller's mistake, not ours.
        return jsonify({"error": str(error)}), 400

    @server_bp.errorhandler(Exception)
    def unexpected(error):
        logger.exception("Unhandled error in %s %s", request.method, request.path)
        return jsonify({"error": str(error) or type(error).__name__}), 500

    def server_or_404(server_id):
        server = amnezia_manager.get_server(server_id)
        if not server:
            abort(404, description="Server not found")
        return server

    def client_or_404(server_id, client_id):
        """(server, client) when the client exists and belongs to that server."""
        server = server_or_404(server_id)
        client = amnezia_manager.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            abort(404, description="Client not found")
        return server, client

    def json_body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    # Never sent to the browser: the UI reads none of them, and every config that needs
    # them is rendered server-side (config-both, the .conf downloads, /config). The
    # fingerprint hashes a config that holds the client's private key.
    secret_keys = frozenset({"server_private_key", "client_private_key", "preshared_key", "config_issued_fingerprint"})

    def serialize_client(client, server=None):
        # server_name is not stored per client; it always comes from the server.
        server = server or amnezia_manager.get_server(client.get("server_id")) or {}
        payload = {key: value for key, value in client.items() if key not in secret_keys}
        return {
            **payload,
            "server_name": server.get("name"),
            "config_issued_at": client.get("config_issued_at"),
            "config_outdated": bool(server) and amnezia_manager.is_config_outdated(server, client),
        }

    def serialize_server(server):
        payload = {key: value for key, value in server.items() if key not in secret_keys}
        return {**payload, "clients": [serialize_client(client, server) for client in server.get("clients", [])]}

    # --- dry-run validation -----------------------------------------------------

    def collect(errors, check, *args):
        """Run one validator; its ValueError becomes a message instead of a 400."""
        try:
            return check(*args)
        except ValueError as exc:
            errors.append(str(exc))
            return None

    @server_bp.route("/api/validate", methods=["POST"])
    def validate():
        """Check a form as it is being filled in, without changing anything, so the
        rules live only here. One body per form:
          {"server": {...}}                                        new server
          {"server_id", "protocol", "transport_params": {...}}     server settings
          {"server_id", "client_params": {...}}                    add/edit client
        Answers {"errors": [...], "warnings": [...]}; the settings form also gets
        the counts its warning shows. Each validator stops at its first problem, so
        there is one message per failing validator."""
        data = json_body()
        errors, warnings = [], []

        if isinstance(data.get("server"), dict):
            server_data = data["server"]
            if not str(server_data.get("name") or "").strip():
                errors.append("A server name is required")
            basics, basic_errors = amnezia_manager.check_server_basics(server_data)
            errors += basic_errors
            if "port" in basics and "subnet" in basics:
                collect(errors, amnezia_manager.assert_no_conflicts, basics["port"], basics["subnet"])
            mtu = basics.get("mtu", amnezia_manager.default_mtu)
            protocol = amnezia_manager.normalize_protocol(server_data.get("protocol"))
            if isinstance(server_data.get("transport_params"), dict):
                transport = collect(
                    errors, amnezia_manager.validate_transport_params, protocol, server_data["transport_params"]
                )
                if transport:
                    warnings += amnezia_manager.transport_param_warnings(protocol, transport, mtu)
            if isinstance(server_data.get("client_defaults"), dict):
                client_params = collect(errors, amnezia_manager.validate_client_params, server_data["client_defaults"])
                if client_params:
                    warnings += amnezia_manager.client_param_warnings(client_params, mtu)
            return jsonify({"errors": errors, "warnings": warnings})

        if "server_id" not in data:
            abort(400, description="Expected 'server', or 'server_id' with transport_params or client_params")
        server = server_or_404(data["server_id"])
        mtu = server.get("mtu", 1420)

        if isinstance(data.get("transport_params"), dict):
            protocol = amnezia_manager.normalize_protocol(data.get("protocol", server.get("protocol")))
            transport = collect(errors, amnezia_manager.validate_transport_params, protocol, data["transport_params"])
            counts = {"configs_changed": 0, "outdated_now": 0, "outdated_after": 0}
            if transport:
                warnings += amnezia_manager.transport_param_warnings(protocol, transport, mtu)
                counts = amnezia_manager.preview_transport_change(server, protocol, transport)
            return jsonify({"errors": errors, "warnings": warnings, **counts})

        if isinstance(data.get("client_params"), dict):
            client_params = collect(errors, amnezia_manager.validate_client_params, data["client_params"])
            if client_params:
                warnings += amnezia_manager.client_param_warnings(client_params, mtu)
            return jsonify({"errors": errors, "warnings": warnings})

        abort(400, description="Expected transport_params or client_params with server_id")

    @server_bp.route("/api/generate", methods=["POST"])
    def generate():
        """Random parameters for a form, the one generator the UI uses: {protocol, mtu}
        -> {protocol, transport_params, client_defaults}. A dry run like /api/validate;
        nothing is saved. Everything drawn passes the validators without a warning."""
        data = json_body()
        protocol = amnezia_manager.normalize_protocol(data.get("protocol"))
        mtu = amnezia_manager.validate_mtu(data.get("mtu", amnezia_manager.default_mtu))
        return jsonify(
            {
                "protocol": protocol,
                "transport_params": amnezia_manager.generate_transport_params(protocol, mtu),
                "client_defaults": amnezia_manager.generate_client_defaults(protocol),
            }
        )

    # --- servers ------------------------------------------------------------------

    @server_bp.route("/api/servers", methods=["GET"])
    def get_servers():
        """List servers with live status and geo labels. Read-only: the display values
        are computed into the response, never written back to the stored config."""
        payload = []
        for server in amnezia_manager.config["servers"]:
            item = serialize_server(server)
            item["status"] = amnezia_manager.get_server_status(server["id"])
            item["public_ip_geo"], item["public_ip_geo_country_code"] = amnezia_manager.lookup_geoip(server.get("public_ip"))

            if isinstance(server.get("egress_probe"), dict):
                probe = dict(server["egress_probe"])
                probe["external_ip_geo"], probe["external_ip_geo_country_code"] = amnezia_manager.lookup_geoip(
                    probe.get("external_ip")
                )
                probe["service_name"] = amnezia_manager.format_probe_service_name(probe.get("service"))
                item["egress_probe"] = probe

            payload.append(item)
        return jsonify(payload)

    @server_bp.route("/api/servers", methods=["POST"])
    def create_server():
        data = json_body()
        # Require an explicit name so an empty POST cannot silently create a
        # fully-defaulted (and auto-started) server.
        if not str(data.get("name") or "").strip():
            return jsonify({"error": "A server name is required"}), 400
        return jsonify(serialize_server(amnezia_manager.create_wireguard_server(data)))

    @server_bp.route("/api/servers/<server_id>", methods=["DELETE"])
    def delete_server(server_id):
        server_or_404(server_id)
        amnezia_manager.delete_server(server_id)
        return jsonify({"status": "deleted", "server_id": server_id})

    @server_bp.route("/api/servers/<server_id>/start", methods=["POST"])
    def start_server(server_id):
        if amnezia_manager.start_server(server_id):
            return jsonify({"status": "started"})
        return jsonify({"error": "Server not found or failed to start"}), 404

    @server_bp.route("/api/servers/<server_id>/stop", methods=["POST"])
    def stop_server(server_id):
        if amnezia_manager.stop_server(server_id):
            return jsonify({"status": "stopped"})
        return jsonify({"error": "Server not found or failed to stop"}), 404

    @server_bp.route("/api/servers/<server_id>/rename", methods=["POST"])
    def rename_server(server_id):
        server_or_404(server_id)
        new_name = str(json_body().get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "Name cannot be empty"}), 400
        amnezia_manager.rename_server(server_id, new_name)
        return jsonify({"status": "renamed", "server_id": server_id, "name": new_name})

    @server_bp.route("/api/servers/<server_id>/info")
    def get_server_info(server_id):
        server = server_or_404(server_id)
        return jsonify(
            {
                "id": server["id"],
                "name": server["name"],
                "protocol": server["protocol"],
                "port": server["port"],
                "status": amnezia_manager.get_server_status(server_id),
                "interface": server["interface"],
                "config_path": server["config_path"],
                "public_ip": server["public_ip"],
                "server_ip": server["server_ip"],
                "subnet": server["subnet"],
                "mtu": server.get("mtu", 1420),
                "transport_params": server.get("transport_params", {}),
                "client_defaults": server.get("client_defaults", {}),
                "enable_nat": server.get("enable_nat", default_enable_nat),
                "block_lan_cidrs": server.get("block_lan_cidrs", default_block_lan_cidrs),
                "clients_count": len(server["clients"]),
                "created_at": server["created_at"],
                "public_key": server["server_public_key"],
                "dns": server["dns"],
            }
        )

    @server_bp.route("/api/servers/<server_id>/config")
    def get_server_config(server_id):
        server = server_or_404(server_id)
        if not os.path.exists(server["config_path"]):
            abort(404, description="Config file not found")
        with open(server["config_path"], "r", encoding="utf-8") as f:
            config_content = f.read()
        return jsonify(
            {
                "server_id": server_id,
                "server_name": server["name"],
                "config_path": server["config_path"],
                "config_content": config_content,
                "interface": server["interface"],
                "public_key": server["server_public_key"],
            }
        )

    @server_bp.route("/api/servers/<server_id>/config/download")
    def download_server_config(server_id):
        server = server_or_404(server_id)
        if not os.path.exists(server["config_path"]):
            abort(404, description="Config file not found")
        return send_file(server["config_path"], as_attachment=True, download_name=f"{server['interface']}.conf")

    @server_bp.route("/api/servers/<server_id>/transport-params", methods=["POST"])
    def update_server_transport_params(server_id):
        server_or_404(server_id)
        return jsonify(amnezia_manager.update_server_transport_params(server_id, json_body()))

    @server_bp.route("/api/servers/<server_id>/networking", methods=["POST"])
    def update_server_networking(server_id):
        server = server_or_404(server_id)
        data = json_body()
        server["enable_nat"] = to_bool(data.get("enable_nat"), server.get("enable_nat", default_enable_nat))
        server["block_lan_cidrs"] = to_bool(data.get("block_lan_cidrs"), server.get("block_lan_cidrs", default_block_lan_cidrs))
        amnezia_manager.save_config()

        iptables_status = "skipped"
        if amnezia_manager.get_server_status(server_id) == "running":
            iptables_status = "reapplied" if amnezia_manager.reapply_iptables_for_server(server) else "failed"

        return jsonify(
            {
                "status": "updated",
                "server_id": server_id,
                "enable_nat": server["enable_nat"],
                "block_lan_cidrs": server["block_lan_cidrs"],
                "iptables": iptables_status,
            }
        )

    @server_bp.route("/api/servers/<server_id>/traffic")
    def get_server_traffic(server_id):
        traffic = amnezia_manager.get_traffic_for_server(server_id)
        if traffic is None:
            abort(404, description="Server not found or no traffic data")
        return jsonify(traffic)

    @server_bp.route("/api/servers/<server_id>/egress-ip", methods=["POST"])
    def probe_server_egress_ip(server_id):
        server = server_or_404(server_id)
        probe = amnezia_manager.probe_server_egress_ip(server_id)
        return jsonify({"server_id": server_id, "server_name": server.get("name"), **probe})

    # --- clients ------------------------------------------------------------------

    @server_bp.route("/api/clients", methods=["GET"])
    def get_all_clients():
        return jsonify([serialize_client(client) for client in amnezia_manager.get_client_configs()])

    @server_bp.route("/api/servers/<server_id>/clients", methods=["GET"])
    def get_server_clients(server_id):
        # Lists by filter: an unknown server simply has no clients.
        return jsonify([serialize_client(client) for client in amnezia_manager.get_client_configs(server_id)])

    @server_bp.route("/api/servers/<server_id>/clients", methods=["POST"])
    def add_client(server_id):
        server_or_404(server_id)
        data = json_body()
        client_params = data.get("client_params") if isinstance(data.get("client_params"), dict) else None
        client_config, config_content = amnezia_manager.add_wireguard_client(
            server_id,
            data.get("name", "New Client"),
            client_params=client_params,
            copy_from_client_id=data.get("copy_from_client_id"),
        )
        return jsonify({"client": serialize_client(client_config), "config": config_content})

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>", methods=["DELETE"])
    def delete_client(server_id, client_id):
        client_or_404(server_id, client_id)
        amnezia_manager.delete_client(server_id, client_id)
        return jsonify({"status": "deleted", "client_id": client_id})

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>/rename", methods=["POST"])
    def rename_client(server_id, client_id):
        client_or_404(server_id, client_id)
        new_name = str(json_body().get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "Name cannot be empty"}), 400
        amnezia_manager.rename_client(server_id, client_id, new_name)
        return jsonify({"status": "renamed", "server_id": server_id, "client_id": client_id, "name": new_name})

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>/suspend", methods=["POST"])
    def toggle_client_suspend(server_id, client_id):
        client_or_404(server_id, client_id)
        client = amnezia_manager.toggle_client_suspend(server_id, client_id)
        return jsonify(
            {
                "status": "updated",
                "server_id": server_id,
                "client_id": client_id,
                "suspended": client.get("suspended", False),
                "client": serialize_client(client),
            }
        )

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>/client-params", methods=["POST"])
    def update_client_params(server_id, client_id):
        client_or_404(server_id, client_id)
        client_params = json_body().get("client_params")
        if not isinstance(client_params, dict):
            return jsonify({"error": "client_params must be an object"}), 400
        client = amnezia_manager.update_client_params(server_id, client_id, client_params)
        return jsonify(
            {
                "status": "updated",
                "server_id": server_id,
                "client_id": client_id,
                "client_params": client.get("client_params", {}),
                "client": serialize_client(client),
            }
        )

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>/issued", methods=["POST"])
    def mark_client_config_issued(server_id, client_id):
        """The UI calls this after showing the QR or downloading the .conf. A POST, not a
        side effect of the GET config routes, so it stays behind the JSON-only guard."""
        server, client = client_or_404(server_id, client_id)
        amnezia_manager.mark_config_issued(server, client)
        return jsonify({"status": "issued", "client": serialize_client(client, server)})

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>/config")
    def download_client_config(server_id, client_id):
        server, client = client_or_404(server_id, client_id)
        config_content = amnezia_manager.generate_wireguard_client_config(server, client, include_comments=True)

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{client.get('name', 'client')}_{server.get('name', 'server')}")
        filename = (safe_name.strip("._")[:195] or "client") + ".conf"
        return send_file(
            io.BytesIO(config_content.encode("utf-8")),
            as_attachment=True,
            download_name=filename,
            mimetype="text/plain; charset=utf-8",
        )

    @server_bp.route("/api/servers/<server_id>/clients/<client_id>/config-both")
    def get_client_config_both(server_id, client_id):
        server, client = client_or_404(server_id, client_id)
        clean_config = amnezia_manager.generate_wireguard_client_config(server, client, include_comments=False)
        full_config = amnezia_manager.generate_wireguard_client_config(server, client, include_comments=True)
        return jsonify(
            {
                "server_id": server_id,
                "client_id": client_id,
                "client_name": client["name"],
                "clean_config": clean_config,
                "full_config": full_config,
                "clean_length": len(clean_config),
                "full_length": len(full_config),
            }
        )

    app.register_blueprint(server_bp)
