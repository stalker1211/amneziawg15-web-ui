"""System-level API routes and health/status endpoints."""

import os
import re
import time
import subprocess
from collections import deque
from flask import Blueprint, request, jsonify

# pylint: disable=broad-exception-caught
# pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements


def register_system_routes(
    app,
    require_token,
    amnezia_manager,
    *,
    awg_log_file,
    nginx_port,
    auto_start_servers,
    default_mtu,
    default_subnet,
    default_port,
    default_dns,
):
    """Register system and health-related Flask routes on the app."""
    system_bp = Blueprint('system_routes', __name__)

    @system_bp.route('/api/system/status')
    @require_token
    def system_status():
        _, public_ip_geo_country_code = amnezia_manager.lookup_geoip(amnezia_manager.public_ip)
        status = {
            "awg_available": (
                os.path.exists("/usr/bin/awg")
                and os.path.exists("/usr/bin/awg-quick")
            ),
            "public_ip": amnezia_manager.public_ip,
            "public_ip_geo_country_code": public_ip_geo_country_code,
            "total_servers": len(amnezia_manager.config["servers"]),
            "total_clients": len(amnezia_manager.config["clients"]),
            "active_servers": len([s for s in amnezia_manager.config["servers"]
                                 if amnezia_manager.get_server_status(s["id"]) == "running"]),
            "timestamp": time.time(),
            "environment": {
                "nginx_port": nginx_port,
                "auto_start_servers": auto_start_servers,
                "default_mtu": default_mtu,
                "default_subnet": default_subnet,
                "default_port": default_port,
                "default_dns": default_dns
            }
        }
        return jsonify(status)

    @system_bp.route('/api/system/awg-log')
    @require_token
    def get_awg_log():
        """Tail amneziawg-go log with optional interface filtering.

        Filtering rules:
                - If interface is set, include lines that match that interface marker
                    ("(wg0)" or "*** (wg0) ***").
        - Always include general lines that do not mention any interface.
        """
        interface = (request.args.get('interface') or '').strip()
        raw_lines = request.args.get('lines', '400')
        try:
            lines_n = int(raw_lines)
        except Exception:
            lines_n = 400
        lines_n = max(50, min(5000, lines_n))

        log_path = awg_log_file or '/var/log/amnezia/amneziawg-go.log'
        if not os.path.exists(log_path):
            return jsonify({"path": log_path, "lines": [], "note": "log file not found"})

        iface_any_re = re.compile(r"\([A-Za-z0-9_.=+\-]{1,15}\)")
        iface_star_any_re = re.compile(r"\*\*\*\s*\([A-Za-z0-9_.=+\-]{1,15}\)\s*\*\*\*")

        iface_token = f"({interface})" if interface else ""
        iface_star = f"*** ({interface}) ***" if interface else ""
        start_iface_re = re.compile(r"\bstarting:\s*([A-Za-z0-9_.=+\-]{1,15})\b")

        def line_mentions_any_interface(line: str) -> bool:
            return bool(iface_any_re.search(line) or iface_star_any_re.search(line))

        def line_matches_interface(line: str) -> bool:
            if not interface:
                return True
            return (iface_token in line) or (iface_star in line)

        def is_global_noise(line: str) -> bool:
            stripped = line.strip()
            if "[amneziawg-go-logged]" in line:
                return True
            if stripped.startswith("┌") or stripped.startswith("└") or stripped.startswith("│"):
                return True
            if stripped.startswith(
                "| https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"
            ):
                return True
            if "amneziawg-go is not required" in line:
                return True
            if "kernel has first class support for AmneziaWG" in line:
                return True
            return False

        try:
            buf = deque(maxlen=lines_n)
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                for ln in f:
                    buf.append(ln.rstrip('\n'))

            if not interface:
                filtered = list(buf)
            else:
                filtered = []
                banner_iface = None
                banner_active = False
                for ln in buf:
                    sm = start_iface_re.search(ln)
                    if sm:
                        banner_iface = sm.group(1)
                        banner_active = True

                    if line_matches_interface(ln):
                        filtered.append(ln)
                    elif not line_mentions_any_interface(ln):
                        if is_global_noise(ln):
                            if banner_active and banner_iface == interface:
                                filtered.append(ln)
                        else:
                            filtered.append(ln)

                    if banner_active and ln.strip().startswith("└"):
                        banner_active = False

            return jsonify({
                "path": log_path,
                "lines": filtered,
                "interface": interface,
                "total": len(filtered),
            })
        except Exception as e:
            return jsonify({"error": str(e), "path": log_path}), 500

    @system_bp.route('/api/system/refresh-ip')
    @require_token
    def refresh_ip():
        """Refresh public IP address"""
        new_ip = amnezia_manager.detect_public_ip()
        amnezia_manager.public_ip = new_ip
        _, public_ip_geo_country_code = amnezia_manager.lookup_geoip(new_ip)

        for server in amnezia_manager.config["servers"]:
            server["public_ip"] = new_ip

        amnezia_manager.save_config()
        return jsonify({
            "public_ip": new_ip,
            "public_ip_geo_country_code": public_ip_geo_country_code,
        })

    @system_bp.route('/api/system/iptables-test')
    @require_token
    def iptables_test():
        """Test iptables setup for a specific server"""
        server_id = request.args.get('server_id')
        if not server_id:
            return jsonify({"error": "server_id parameter required"}), 400

        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        try:
            check_commands = [
                f"iptables -L INPUT -n | grep {server['interface']}",
                f"iptables -L FORWARD -n | grep {server['interface']}",
                f"iptables -t nat -L POSTROUTING -n | grep {server['subnet']}"
            ]

            results = {}
            for cmd in check_commands:
                try:
                    result = amnezia_manager.execute_command(cmd)
                    results[cmd] = "Found" if result else "Not found"
                except Exception:
                    results[cmd] = "Error"

            return jsonify({
                "server_id": server_id,
                "server_name": server['name'],
                "interface": server['interface'],
                "subnet": server['subnet'],
                "iptables_check": results
            })

        except Exception as e:
            return jsonify({"error": f"iptables test failed: {str(e)}"}), 500

    @system_bp.route('/status')
    def get_container_uptime():
        """Return simple text uptime for container health checks."""
        result = subprocess.check_output(["stat", "-c %Y", "/proc/1/cmdline"], text=True)
        uptime_seconds_epoch = int(result.strip())

        now_epoch = int(time.time())

        uptime_seconds = now_epoch - uptime_seconds_epoch
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        seconds = uptime_seconds % 60

        return f"Container Uptime: {days}d {hours}h {minutes}m {seconds}s"

    app.register_blueprint(system_bp)
