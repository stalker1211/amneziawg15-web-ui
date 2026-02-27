"""Server and client management API routes."""

import io
import os
import re
from flask import Blueprint, request, jsonify, send_file

# pylint: disable=broad-exception-caught
# pylint: disable=too-many-arguments,too-many-locals,too-many-statements


def register_server_routes(
    app,
    require_token,
    amnezia_manager,
    *,
    to_bool,
    default_enable_nat,
    default_block_lan_cidrs,
):
    """Register server/client management routes on the Flask app."""
    server_bp = Blueprint('server_routes', __name__)

    @server_bp.route('/api/servers', methods=['POST'])
    @require_token
    def create_server():
        data = request.get_json(silent=True) or {}
        server = amnezia_manager.create_wireguard_server(data)
        return jsonify(server)

    @server_bp.route('/api/servers/<server_id>', methods=['DELETE'])
    @require_token
    def delete_server(server_id):
        if amnezia_manager.delete_server(server_id):
            return jsonify({"status": "deleted", "server_id": server_id})
        return jsonify({"error": "Server not found"}), 404

    @server_bp.route('/api/servers/<server_id>/start', methods=['POST'])
    @require_token
    def start_server(server_id):
        if amnezia_manager.start_server(server_id):
            return jsonify({"status": "started"})
        return jsonify({"error": "Server not found or failed to start"}), 404

    @server_bp.route('/api/servers/<server_id>/stop', methods=['POST'])
    @require_token
    def stop_server(server_id):
        if amnezia_manager.stop_server(server_id):
            return jsonify({"status": "stopped"})
        return jsonify({"error": "Server not found or failed to stop"}), 404

    @server_bp.route('/api/servers/<server_id>/clients', methods=['GET'])
    @require_token
    def get_server_clients(server_id):
        clients = amnezia_manager.get_client_configs(server_id)
        return jsonify(clients)

    @server_bp.route('/api/servers/<server_id>/clients', methods=['POST'])
    @require_token
    def add_client(server_id):
        data = request.get_json(silent=True) or {}
        client_name = data.get('name', 'New Client')

        i_params = None
        if isinstance(data, dict):
            raw = data.get('i_params')
            if raw is None:
                raw = data.get('obfuscation_params')
            if isinstance(raw, dict):
                i_params = {k: raw.get(k, "") for k in ("I1", "I2", "I3", "I4", "I5") if k in raw}

        result = amnezia_manager.add_wireguard_client(server_id, client_name, i_params=i_params)
        if result:
            client_config, config_content = result
            return jsonify({
                "client": client_config,
                "config": config_content
            })
        return jsonify({"error": "Server not found"}), 404

    @server_bp.route('/api/servers/<server_id>/clients/<client_id>', methods=['DELETE'])
    @require_token
    def delete_client(server_id, client_id):
        if amnezia_manager.delete_client(server_id, client_id):
            return jsonify({"status": "deleted", "client_id": client_id})
        return jsonify({"error": "Client not found"}), 404

    @server_bp.route('/api/servers/<server_id>/clients/<client_id>/config')
    @require_token
    def download_client_config(server_id, client_id):
        client = amnezia_manager.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            return jsonify({"error": "Client not found"}), 404

        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        config_content = amnezia_manager.generate_wireguard_client_config(
            server, client, include_comments=True
        )

        raw_filename = f"{client.get('name', 'client')}_{server.get('name', 'server')}.conf"

        def sanitize_filename(value):
            safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value)).strip('._')
            return (safe[:200] or "client")

        if raw_filename.endswith('.conf'):
            filename = sanitize_filename(raw_filename)
        else:
            filename = sanitize_filename(raw_filename) + ".conf"
        if not filename.lower().endswith('.conf'):
            filename += '.conf'

        data = io.BytesIO(config_content.encode('utf-8'))
        data.seek(0)
        return send_file(
            data,
            as_attachment=True,
            download_name=filename,
            mimetype='text/plain; charset=utf-8'
        )

    @server_bp.route('/api/clients', methods=['GET'])
    @require_token
    def get_all_clients():
        clients = amnezia_manager.get_client_configs()
        return jsonify(clients)

    @server_bp.route('/api/servers/<server_id>/egress-ip', methods=['POST'])
    @require_token
    def probe_server_egress_ip(server_id):
        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        probe = amnezia_manager.probe_server_egress_ip(server_id)
        if probe is None:
            return jsonify({"error": "Server not found"}), 404

        return jsonify({
            "server_id": server_id,
            "server_name": server.get('name'),
            **probe
        })

    @server_bp.route('/api/servers/<server_id>/config')
    @require_token
    def get_server_config(server_id):
        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        try:
            if os.path.exists(server['config_path']):
                with open(server['config_path'], 'r', encoding='utf-8') as f:
                    config_content = f.read()

                return jsonify({
                    "server_id": server_id,
                    "server_name": server['name'],
                    "config_path": server['config_path'],
                    "config_content": config_content,
                    "interface": server['interface'],
                    "public_key": server['server_public_key']
                })
            return jsonify({"error": "Config file not found"}), 404
        except Exception as e:
            return jsonify({"error": f"Failed to read config: {str(e)}"}), 500

    @server_bp.route('/api/servers/<server_id>/config/download')
    @require_token
    def download_server_config(server_id):
        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        try:
            if os.path.exists(server['config_path']):
                return send_file(
                    server['config_path'],
                    as_attachment=True,
                    download_name=f"{server['interface']}.conf"
                )
            return jsonify({"error": "Config file not found"}), 404
        except Exception as e:
            return jsonify({"error": f"Failed to download config: {str(e)}"}), 500

    @server_bp.route('/api/servers/<server_id>/info')
    @require_token
    def get_server_info(server_id):
        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        current_status = amnezia_manager.get_server_status(server_id)
        server['current_status'] = current_status

        config_preview = ""
        if os.path.exists(server['config_path']):
            try:
                with open(server['config_path'], 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    config_preview = ''.join(lines[:min(10, len(lines))])
            except Exception:
                config_preview = "Unable to read config file"

        mtu_value = server.get('mtu', 1420)

        server_info = {
            "id": server['id'],
            "name": server['name'],
            "protocol": server['protocol'],
            "port": server['port'],
            "status": current_status,
            "interface": server['interface'],
            "config_path": server['config_path'],
            "public_ip": server['public_ip'],
            "server_ip": server['server_ip'],
            "subnet": server['subnet'],
            "mtu": mtu_value,
            "obfuscation_enabled": server['obfuscation_enabled'],
            "obfuscation_params": server.get('obfuscation_params', {}),
            "enable_nat": server.get('enable_nat', default_enable_nat),
            "block_lan_cidrs": server.get('block_lan_cidrs', default_block_lan_cidrs),
            "clients_count": len(server['clients']),
            "created_at": server['created_at'],
            "config_preview": config_preview,
            "public_key": server['server_public_key'],
            "dns": server['dns']
        }

        return jsonify(server_info)

    @server_bp.route('/api/servers/<server_id>/obfuscation-params', methods=['POST'])
    @require_token
    def update_server_obfuscation_params(server_id):
        data = request.get_json(silent=True) or {}
        try:
            result = amnezia_manager.update_server_obfuscation_params(server_id, data)
            if not result:
                return jsonify({"error": "Server not found"}), 404
            return jsonify(result)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to update obfuscation params: {str(e)}"}), 500

    @server_bp.route('/api/servers/<server_id>/networking', methods=['POST'])
    @require_token
    def update_server_networking(server_id):
        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        data = request.get_json(silent=True) or {}
        enable_nat = to_bool(
            data.get('enable_nat'),
            server.get('enable_nat', default_enable_nat),
        )
        block_lan_cidrs = to_bool(
            data.get('block_lan_cidrs'),
            server.get('block_lan_cidrs', default_block_lan_cidrs),
        )

        server['enable_nat'] = enable_nat
        server['block_lan_cidrs'] = block_lan_cidrs
        amnezia_manager.save_config()

        iptables_status = 'skipped'
        if amnezia_manager.get_server_status(server_id) == 'running':
            ok = amnezia_manager.reapply_iptables_for_server(server)
            iptables_status = 'reapplied' if ok else 'failed'

        return jsonify({
            "status": "updated",
            "server_id": server_id,
            "enable_nat": enable_nat,
            "block_lan_cidrs": block_lan_cidrs,
            "iptables": iptables_status
        })

    @server_bp.route('/api/servers/<server_id>/i-params', methods=['POST'])
    @require_token
    def update_server_i_params(server_id):
        data = request.json or {}
        i_params = {}
        for key in ("I1", "I2", "I3", "I4", "I5"):
            if key in data:
                i_params[key] = data.get(key, "")

        updated_server = amnezia_manager.update_server_i_params(server_id, i_params)
        if not updated_server:
            return jsonify({"error": "Server not found"}), 404

        return jsonify({
            "status": "updated",
            "server_id": server_id,
            "obfuscation_params": updated_server.get('obfuscation_params', {})
        })

    @server_bp.route('/api/servers/<server_id>/clients/<client_id>/i-params', methods=['POST'])
    @require_token
    def update_client_i_params(server_id, client_id):
        data = request.json or {}
        i_params = {}
        for key in ("I1", "I2", "I3", "I4", "I5"):
            if key in data:
                i_params[key] = data.get(key, "")

        updated_client = amnezia_manager.update_client_i_params(server_id, client_id, i_params)
        if not updated_client:
            return jsonify({"error": "Client not found"}), 404

        return jsonify({
            "status": "updated",
            "server_id": server_id,
            "client_id": client_id,
            "obfuscation_params": updated_client.get('obfuscation_params', {})
        })

    @server_bp.route('/api/servers', methods=['GET'])
    @require_token
    def get_servers():
        for server in amnezia_manager.config["servers"]:
            server["status"] = amnezia_manager.get_server_status(server["id"])
            if 'mtu' not in server:
                server['mtu'] = 1420
            if 'enable_nat' not in server:
                server['enable_nat'] = default_enable_nat
            if 'block_lan_cidrs' not in server:
                server['block_lan_cidrs'] = default_block_lan_cidrs
            if 'egress_probe' not in server:
                server['egress_probe'] = None

            public_geo, public_geo_cc = amnezia_manager.lookup_geoip(server.get("public_ip"))
            server["public_ip_geo"] = public_geo
            server["public_ip_geo_country_code"] = public_geo_cc

            probe = server.get("egress_probe") if isinstance(server.get("egress_probe"), dict) else None
            if probe is not None:
                egress_geo, egress_geo_cc = amnezia_manager.lookup_geoip(probe.get("external_ip"))
                probe["external_ip_geo"] = egress_geo
                probe["external_ip_geo_country_code"] = egress_geo_cc

        amnezia_manager.save_config()
        return jsonify(amnezia_manager.config["servers"])

    @server_bp.route('/api/servers/<server_id>/clients/<client_id>/config-both')
    @require_token
    def get_client_config_both(server_id, client_id):
        client = amnezia_manager.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            return jsonify({"error": "Client not found"}), 404

        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        clean_config = amnezia_manager.generate_wireguard_client_config(
            server, client, include_comments=False
        )

        full_config = amnezia_manager.generate_wireguard_client_config(
            server, client, include_comments=True
        )

        return jsonify({
            "server_id": server_id,
            "client_id": client_id,
            "client_name": client['name'],
            "clean_config": clean_config,
            "full_config": full_config,
            "clean_length": len(clean_config),
            "full_length": len(full_config)
        })

    @server_bp.route('/api/servers/<server_id>/traffic')
    @require_token
    def get_server_traffic(server_id):
        traffic = amnezia_manager.get_traffic_for_server(server_id)
        if traffic is None:
            return jsonify({"error": "Server not found or no traffic data"}), 404
        return jsonify(traffic)

    app.register_blueprint(server_bp)
