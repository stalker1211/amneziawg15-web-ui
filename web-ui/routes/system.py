"""System-level API routes and health/status endpoints."""

import os
import re
import subprocess
import time

from core.logging_setup import get_logger
from flask import Blueprint, jsonify, request

# pylint: disable=broad-exception-caught
# pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements

logger = get_logger(__name__)


def tail_lines(path, count, block_size=64 * 1024):
    """The last `count` lines of a text file, read backwards from its end.

    The Logs view asks every 10 s, and at debug level the daemon log only grows;
    reading it whole each time cost more with every day the container ran.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        position = f.tell()
        data = b""
        # count + 1 newlines guarantee `count` whole lines (the first may be partial).
        while position > 0 and data.count(b"\n") <= count:
            step = min(block_size, position)
            position -= step
            f.seek(position)
            data = f.read(step) + data
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file's final newline
    return lines[-count:] if count else []


def page_config(amnezia_manager, access=None):
    """What the page needs from the backend before its first request, rendered into
    index.html as JSON (`#appConfig`): the protocol table, the parameter key lists and
    the new-server defaults. The UI keeps no copy of any of it (static/js/protocols.js
    only reads this), so there is nothing to drift.
    """
    m = amnezia_manager
    return {
        "protocols": {
            "default": m.DEFAULT_PROTOCOL,
            "supported": [
                {
                    "id": protocol,
                    "supportsS34": m.protocol_supports_s34(protocol),
                    "supportsHeaderRanges": m.protocol_supports_header_ranges(protocol),
                    "supportsAwg3": m.protocol_supports_awg3(protocol),
                    "supportsAwg31": m.protocol_supports_awg31(protocol),
                }
                for protocol in m.SUPPORTED_PROTOCOLS
            ],
        },
        "params": {
            "transport": list(m.TRANSPORT_PARAM_KEYS),
            "signature": [key for key in m.CLIENT_ONLY_PARAM_KEYS if key.startswith("I")],
            "awg3Client": list(m.CLIENT_AWG3_PARAM_KEYS),
        },
        "defaults": {
            "mtu": m.default_mtu,
            "subnet": m.default_subnet,
            "port": m.default_port,
            "dns": ", ".join(m.dns_servers),
            "enable_nat": m.default_enable_nat,
            "block_lan_cidrs": m.default_block_lan_cidrs,
        },
        # Shows the "default password" banner from the first paint.
        "passwordIsDefault": bool(access and access.is_default()),
    }


def register_system_routes(app, amnezia_manager, *, awg_log_file, nginx_port):
    """Register system and health-related Flask routes on the app."""
    system_bp = Blueprint("system_routes", __name__)

    @system_bp.route("/api/system/status")
    def system_status():
        _, public_ip_geo_country_code = amnezia_manager.lookup_geoip(amnezia_manager.public_ip)
        status = {
            "awg_available": (os.path.exists("/usr/bin/awg") and os.path.exists("/usr/bin/awg-quick")),
            "public_ip": amnezia_manager.public_ip,
            "public_ip_geo_country_code": public_ip_geo_country_code,
            "total_servers": len(amnezia_manager.config["servers"]),
            "total_clients": len(amnezia_manager.get_client_configs()),
            "active_servers": len(
                [s for s in amnezia_manager.config["servers"] if amnezia_manager.get_server_status(s["id"]) == "running"]
            ),
            "timestamp": time.time(),
            "environment": {
                "nginx_port": nginx_port,
                "default_mtu": amnezia_manager.default_mtu,
                "default_subnet": amnezia_manager.default_subnet,
                "default_port": amnezia_manager.default_port,
                "default_dns": ", ".join(amnezia_manager.dns_servers),
            },
        }
        return jsonify(status)

    @system_bp.route("/api/system/awg-log")
    def get_awg_log():
        """Tail amneziawg-go log with optional interface filtering.

        Filtering rules:
                - If interface is set, include lines that match that interface marker
                    ("(wg0)" or "*** (wg0) ***").
        - Always include general lines that do not mention any interface.
        """
        interface = (request.args.get("interface") or "").strip()
        raw_lines = request.args.get("lines", "400")
        try:
            lines_n = int(raw_lines)
        except Exception:
            lines_n = 400
        lines_n = max(50, min(5000, lines_n))

        log_path = awg_log_file or "/var/log/amnezia/amneziawg-go.log"
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
            if stripped.startswith(("┌", "└", "│")):
                return True
            if stripped.startswith("| https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"):
                return True
            if "amneziawg-go is not required" in line:
                return True
            return "kernel has first class support for AmneziaWG" in line

        try:
            buf = tail_lines(log_path, lines_n)

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

            return jsonify(
                {
                    "path": log_path,
                    "lines": filtered,
                    "interface": interface,
                    "total": len(filtered),
                }
            )
        except Exception as e:
            return jsonify({"error": str(e), "path": log_path}), 500

    @system_bp.route("/api/system/refresh-ip", methods=["POST"])
    def refresh_ip():
        """Detect the public IP again and give it to every server (their clients' Endpoint).

        A POST, since it writes. When detection fails nothing is written: the old
        fallback address would have gone into every client config.
        """
        new_ip = amnezia_manager.detect_public_ip()
        if not new_ip:
            return jsonify({"error": "Could not detect the public IP; nothing was changed"}), 502
        amnezia_manager.public_ip = new_ip
        _, public_ip_geo_country_code = amnezia_manager.lookup_geoip(new_ip)

        for server in amnezia_manager.config["servers"]:
            server["public_ip"] = new_ip

        amnezia_manager.save_config()
        return jsonify(
            {
                "public_ip": new_ip,
                "public_ip_geo_country_code": public_ip_geo_country_code,
            }
        )

    @system_bp.route("/api/system/iptables-test")
    def iptables_test():
        """Test iptables setup for a specific server"""
        server_id = request.args.get("server_id")
        if not server_id:
            return jsonify({"error": "server_id parameter required"}), 400

        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        try:
            # Listed as (label, argv, needle): the chain is dumped with a plain argv
            # call and matched in Python, so interface/subnet never reach a shell.
            # Uses -S rather than -L because -L omits the interface column, which
            # made the INPUT/FORWARD checks always report "Not found".
            checks = [
                ("iptables -S INPUT", ["iptables", "-S", "INPUT"], server["interface"]),
                ("iptables -S FORWARD", ["iptables", "-S", "FORWARD"], server["interface"]),
                ("iptables -t nat -S POSTROUTING", ["iptables", "-t", "nat", "-S", "POSTROUTING"], server["subnet"]),
            ]

            results = {}
            for label, argv, needle in checks:
                output = amnezia_manager.run_command(argv)
                if output is None:
                    results[f"{label} | grep {needle}"] = "Error"
                else:
                    results[f"{label} | grep {needle}"] = "Found" if needle in output else "Not found"

            return jsonify(
                {
                    "server_id": server_id,
                    "server_name": server["name"],
                    "interface": server["interface"],
                    "subnet": server["subnet"],
                    "iptables_check": results,
                }
            )

        except Exception as e:
            return jsonify({"error": f"iptables test failed: {e!s}"}), 500

    @system_bp.route("/status")
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
