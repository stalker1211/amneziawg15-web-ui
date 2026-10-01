"""System-level API routes and health/status endpoints."""

import os
import re
import subprocess
import time

from core.logging_setup import get_logger
from flask import Blueprint, Response, jsonify, make_response, render_template, request
from services import signatures

# pylint: disable=broad-exception-caught
# pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements

logger = get_logger(__name__)

# /status forgives a server that should run but is down this long after the container
# starts: the boot restore is bringing it back.
STATUS_GRACE_SECONDS = 60


def tagged_rules(amnezia_manager):
    """Every rule of the filter and nat tables, and an error per table that could not
    be listed. Plain argv calls matched in Python: no shell, no grep."""
    lines, errors = [], []
    for table in ("filter", "nat"):
        output = amnezia_manager.run_command(["iptables", "-t", table, "-S"])
        if output is None:
            errors.append(f"could not list the {table} table")
            continue
        lines += output.splitlines()
    return lines, errors


def rules_of(server, lines):
    """The rules scripts/setup_iptables.sh tagged `awg:<interface>` for this server."""
    tag = f'"awg:{server["interface"]}"'
    return [line for line in lines if tag in line]


def expected_rule_count(server):
    """INPUT, OUTPUT, FORWARD from the VPN and ESTABLISHED,RELATED; with Block LAN the
    panel's INPUT drop and 3 LAN drops; NAT."""
    return 4 + 4 * bool(server.get("block_lan_cidrs")) + bool(server.get("enable_nat"))


def health_problems(amnezia_manager):
    """What /status reports, one line per kind of problem. Each is state this
    container owns and a restart of the server puts right:

      * a server that should run (its stored status) but is down;
      * a running server whose daemon has peers the panel does not (a suspended or
        deleted device still let in) or lacks some (a live reload that failed, which
        apply_live_config only logs);
      * a running server whose tagged firewall rules are not the number its switches
        call for (a lost Block LAN drop, or a NAT rule left after NAT was turned off).
    """
    m = amnezia_manager
    servers = m.config["servers"]
    live = {s["id"]: m.get_server_status(s["id"]) == "running" for s in servers}
    running = [s for s in servers if live[s["id"]]]

    def label(server):
        return f"{server['name']} ({server['interface']})"

    problems = []
    down = [label(s) for s in servers if s.get("status") == "running" and not live[s["id"]]]
    if down:
        problems.append(f"Down, though it should run: {', '.join(down)}")
    if not running:
        return problems

    # A fresh dump, not read_telemetry()'s snapshot: that one reads a failed command
    # as "no peers", which would look like drift (or hide it).
    dump = m.run_command(["/usr/bin/awg", "show", "all", "dump"])
    if dump is None:
        problems.append("Peers: could not read awg show all dump")
    else:
        peers = m.parse_dump(dump)
        drift = []
        for server in running:
            want = {c.get("client_public_key") for c in server.get("clients") or [] if not c.get("suspended")}
            have = set(peers.get(server["interface"]) or {})
            if want != have:
                drift.append(f"{label(server)}: {len(have - want)} not in the panel, {len(want - have)} missing")
        if drift:
            problems.append(f"Peers differ from the panel: {'; '.join(drift)}")

    lines, errors = tagged_rules(m)
    if errors:
        problems.append(f"Firewall rules: {', '.join(errors)}")
    else:
        counts = [(server, len(rules_of(server, lines)), expected_rule_count(server)) for server in running]
        off = [f"{label(server)} has {have} of {want}" for server, have, want in counts if have != want]
        if off:
            problems.append(f"Firewall rules differ: {', '.join(off)}")
    return problems


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
    the built-in values the New server form offers when there is no server to copy
    from yet. The UI keeps no copy of any of it (static/js/protocols.js only reads
    this), so there is nothing to drift.
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
        # The client drawer's Generate for I1-I5 (services/signatures.py).
        "signatureProfiles": [dict(profile) for profile in signatures.PROFILES],
        "defaults": {
            "mtu": m.default_mtu,
            "subnet": m.default_subnet,
            "port": m.default_port,
            "dns": ", ".join(m.dns_servers),
            "enable_nat": m.default_enable_nat,
            "block_lan_cidrs": m.default_block_lan_cidrs,
        },
        # The "default password" banner from the first paint, and which fix it offers.
        "passwordIsDefault": bool(access and access.is_default()),
        "passwordPinned": bool(access and access.pinned("NGINX_PASSWORD")),
    }


def render_page(amnezia_manager, access, *, cache_bust, build_label):
    """index.html with the page config, never cached.

    The page carries live state (#appConfig: the default-password banner). Without
    Cache-Control Safari brought back an old copy from its back/forward cache after
    the password changed, banner and all; no-store also keeps the page out of that
    cache.
    """
    response = make_response(
        render_template(
            "index.html", cache_bust=cache_bust, build_label=build_label, app_config=page_config(amnezia_manager, access)
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def register_system_routes(app, amnezia_manager, *, awg_log_file, nginx_port):
    """Register system and health-related Flask routes on the app."""
    system_bp = Blueprint("system_routes", __name__)

    @system_bp.route("/api/events")
    def events():
        """Live updates as Server-Sent Events (core/events.py): server_status and
        traffic_update. Under /api/, so nginx's Basic Auth gates it like every call."""
        return Response(
            amnezia_manager.events.stream(),
            mimetype="text/event-stream",
            # nginx buffers a proxied response; this one must pass through as written.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @system_bp.route("/api/system/status")
    def system_status():
        _, public_ip_geo_country_code = amnezia_manager.netinfo.lookup_geoip(amnezia_manager.public_ip)
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
        _, public_ip_geo_country_code = amnezia_manager.netinfo.lookup_geoip(new_ip)

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
        """A server's firewall rules as they are now: every rule tagged `awg:<interface>`
        (scripts/setup_iptables.sh), from the filter and nat tables, and how many its
        NAT and LAN switches call for."""
        server_id = request.args.get("server_id")
        if not server_id:
            return jsonify({"error": "server_id parameter required"}), 400

        server = amnezia_manager.get_server(server_id)
        if not server:
            return jsonify({"error": "Server not found"}), 404

        lines, errors = tagged_rules(amnezia_manager)
        return jsonify(
            {
                "server_id": server_id,
                "server_name": server["name"],
                "interface": server["interface"],
                "running": amnezia_manager.get_server_status(server_id) == "running",
                "rules": rules_of(server, lines),
                "expected": expected_rule_count(server),
                "errors": errors,
            }
        )

    @system_bp.route("/status")
    def get_container_uptime():
        """The Docker HEALTHCHECK: container uptime, and a 503 with a line per problem
        (health_problems), once the container is STATUS_GRACE_SECONDS old.

        "Should run" is the stored status, what was last asked for (the boot restore
        brings those back), so "unhealthy" in docker ps and TrueNAS means a VPN is
        down or has drifted from the panel, not only that the panel is down. The
        internet is deliberately not checked: nothing here can fix it.
        """
        result = subprocess.check_output(["stat", "-c %Y", "/proc/1/cmdline"], text=True)
        uptime_seconds_epoch = int(result.strip())

        now_epoch = int(time.time())

        uptime_seconds = now_epoch - uptime_seconds_epoch
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        seconds = uptime_seconds % 60

        uptime = f"Container Uptime: {days}d {hours}h {minutes}m {seconds}s"
        if uptime_seconds < STATUS_GRACE_SECONDS:
            return uptime
        problems = health_problems(amnezia_manager)
        if problems:
            return "\n".join([uptime, *problems]), 503
        return uptime

    app.register_blueprint(system_bp)
