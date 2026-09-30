"""Serve the real panel with invented example data: for trying the UI, the smoke
tests and screenshots, with no container and no AmneziaWG.

Everything is the production code (routes, guards, Socket.IO, the traffic loop and
its `awg show all dump` parser) except the system edges of the manager: keys, awg-quick, ip,
iptables, GeoIP and the egress probe are answered here. Addresses come from the
documentation ranges and keys are random, so nothing real can leak into a screenshot.

    uv run --no-project --python 3.14 --with-requirements web-ui/requirements.txt \
        tests/demo_server.py [--port 8099]

Then open http://127.0.0.1:8099/ (no login). State lives in a temp dir and is gone
on exit. The smoke tests run against it the same way they run against a container.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "web-ui"))

from core.guards import install_guards, rotate_secret_key
from core.helpers import to_bool
from core.runtime import create_flask_app, create_socketio, register_socket_handlers
from core.settings import Access, Settings
from flask import send_from_directory
from routes.servers import register_server_routes
from routes.settings import register_settings_routes
from routes.system import register_system_routes, render_page
from services.amnezia_manager import AmneziaManager

PUBLIC_IPS = ("203.0.113.24", "203.0.113.57")  # refresh-ip flips between these
GEO = {
    "203.0.113.24": ("Amsterdam", "NL"),
    "203.0.113.57": ("Rotterdam", "NL"),
    "198.51.100.40": ("Amsterdam", "NL"),
    "203.0.113.88": ("Berlin", "DE"),
    "203.0.113.201": ("Warsaw", "PL"),
    "198.51.100.17": ("Amsterdam", "NL"),
    "198.51.100.61": ("Frankfurt am Main", "DE"),
}
KiB, MiB, GiB = 1024, 1024**2, 1024**3


def random_key():
    return base64.b64encode(os.urandom(32)).decode()


class DemoManager(AmneziaManager):
    """The real manager with its system edges answered in memory."""

    def __init__(self, **kwargs):
        self.running = set()  # interfaces that are "up"
        self.peers = {}  # client public key -> {endpoint, handshake_at, rx, tx}
        self.egress = {}  # server_ip -> external IP
        self._public_ip_calls = 0
        super().__init__(**kwargs)

    # --- keys -----------------------------------------------------------------
    def generate_wireguard_keys(self):
        private_key = random_key()
        return {"private_key": private_key, "public_key": self.derive_public_key(private_key)}

    def derive_public_key(self, private_key):
        return base64.b64encode(hashlib.sha256(private_key.encode()).digest()).decode()

    def generate_preshared_key(self):
        return random_key()

    def generate_header_protection_key(self):
        return random_key()

    # --- system ---------------------------------------------------------------
    def ensure_directories(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.wireguard_config_dir, exist_ok=True)

    def run_command(self, args, env=None):
        if args[:2] == ["/usr/bin/awg-quick", "up"]:
            self.running.add(args[2])
        elif args[:2] == ["/usr/bin/awg-quick", "down"]:
            self.running.discard(args[2])
        elif args == ["/usr/bin/awg", "show", "all", "dump"]:
            return self.awg_dump()
        return ""

    def interface_state(self, interface):
        return "unknown" if interface in self.running else None

    def setup_iptables(self, *args, **kwargs):
        return True

    def cleanup_iptables(self, *args, **kwargs):
        return True

    def reapply_iptables_for_server(self, server):
        return True

    def apply_live_config(self, *args, **kwargs):
        return True

    def awg_dump(self):
        """`awg show all dump` for the running interfaces; traffic grows each call."""
        lines, now = [], time.time()
        for server in self.config["servers"]:
            interface = server["interface"]
            if interface not in self.running:
                continue
            lines.append(
                "\t".join([interface, "(hidden)", server["server_public_key"], str(server["port"]), *["0"] * 25, "off"])
            )
            for client in server["clients"]:
                if client.get("suspended"):
                    continue
                peer = self.peers.get(client["client_public_key"])
                if peer and now - peer["handshake_at"] < 300:  # online: keep it chatty, each at its own pace
                    spread = sum(map(ord, client["client_public_key"])) % 97
                    peer["handshake_at"] = now - ((int(now) + spread) % 110 + 1)
                    peer["rx"] += (int(now) % 7 + 1) * 180 * KiB
                    peer["tx"] += (int(now) % 5 + 1) * 40 * KiB
                endpoint, handshake, rx, tx = (
                    (peer["endpoint"], int(peer["handshake_at"]), int(peer["rx"]), int(peer["tx"]))
                    if peer
                    else ("(none)", 0, 0, 0)
                )
                lines.append("\t".join([interface, client["client_public_key"], "(hidden)", endpoint,
                                        f"{client['client_ip']}/32", str(handshake), str(rx), str(tx), "off"]))  # fmt: skip
        return "\n".join(lines)

    # --- network lookups ------------------------------------------------------
    def detect_public_ip(self):
        ip = PUBLIC_IPS[self._public_ip_calls % 2]
        self._public_ip_calls += 1
        return ip

    def lookup_geoip(self, ip):
        return GEO.get(ip, (None, None))

    def get_route_for_source_ip(self, source_ip):
        return "demo"

    def detect_public_ip_from_source(self, source_ip, service):
        if source_ip not in self.egress:
            raise RuntimeError(f"no route to the internet from {source_ip}")
        return self.egress[source_ip], service


def seed(manager):
    """The mockup's example: two running servers, one stopped, one outdated client."""

    def server(name, protocol, port, subnet, *, mtu=1420, dns="1.1.1.1, 9.9.9.9", transport, lan=True):
        return manager.create_wireguard_server(
            {"name": name, "protocol": protocol, "port": port, "subnet": subnet, "mtu": mtu, "dns": dns,
             "transport_params": transport, "auto_start": False, "enable_nat": True, "block_lan_cidrs": lan}
        )  # fmt: skip

    home = server("Home NL", "AWG 2.0", 51820, "10.10.0.0/24",
                  transport={"S1": 50, "S2": 60, "S3": 40, "S4": 20,
                             "H1": "1000-1400", "H2": "2000-2400", "H3": "3000-3400", "H4": "4000-4400"})  # fmt: skip
    travel = server("Travel 443", "AWG 3.1", 443, "10.20.0.0/24", mtu=1380, dns="1.1.1.1", lan=False,
                    transport={"S1": 64, "S2": 88, "S3": 24, "S4": 16, "H1": "120000-130000",
                               "H2": "230000-240000", "H3": "340000-350000", "H4": "450000-460000",
                               "HeaderProtectionKey": random_key(), "RandomTrailers": True})  # fmt: skip
    lab = server("Lab", "AWG 1.5", 51830, "10.30.0.0/24", dns="9.9.9.9",
                 transport={"S1": 30, "S2": 45, "H1": "1182367", "H2": "2295734", "H3": "3348912", "H4": "4417281"})  # fmt: skip

    params = {"Jc": 8, "Jmin": 40, "Jmax": 70}
    now = time.time()
    traffic = {  # name -> (endpoint, seconds since handshake, rx, tx)
        "iPhone": ("198.51.100.40:53412", 12, 1.39 * GiB, 214.6 * MiB),
        "MacBook": ("203.0.113.88:61022", 184, 6.59 * GiB, 802.1 * MiB),
        "iPad": ("198.51.100.40:50112", 3 * 86400 + 7260, 412.3 * MiB, 38.9 * MiB),
        "Pixel": ("203.0.113.201:40211", 47, 922.4 * MiB, 101.7 * MiB),
    }
    clients = {}
    for srv, names in ((home, ("iPhone", "MacBook", "iPad", "Router")), (travel, ("Pixel", "Work laptop")),
                       (lab, ("test-peer",))):  # fmt: skip
        for name in names:
            extra = {"ContentPaddingAddition": "8-24"} if name == "Pixel" else {}
            client, _ = manager.add_wireguard_client(srv["id"], name, client_params={**params, **extra})
            clients[name] = client
            if name in traffic:
                endpoint, ago, rx, tx = traffic[name]
                manager.peers[client["client_public_key"]] = {
                    "endpoint": endpoint,
                    "handshake_at": now - ago,
                    "rx": rx,
                    "tx": tx,
                }

    # Every device got its config; the MacBook's Jc was edited afterwards.
    for srv in (home, travel, lab):
        for client in srv["clients"]:
            manager.mark_config_issued(srv, client)
    manager.update_client_params(home["id"], clients["MacBook"]["id"], {**clients["MacBook"]["client_params"], "Jc": 10})
    manager.toggle_client_suspend(home["id"], clients["Router"]["id"])

    manager.egress = {home["server_ip"]: "198.51.100.17", travel["server_ip"]: "198.51.100.61"}
    for srv in (home, travel):
        manager.start_server(srv["id"])
        manager.probe_server_egress_ip(srv["id"])


def write_log(path, manager):
    """A few daemon lines per interface, in the format scripts/amneziawg-go-logged.sh writes."""
    lines = []
    for server in manager.config["servers"]:
        iface = server["interface"]
        lines += [
            f"2026-09-26T20:41:06+00:00 [amneziawg-go-logged] starting: {iface}",
            f"INFO: ({iface}) 2026/09/26 20:41:07 Starting amneziawg-go version 0.2.16",
            f"DEBUG: ({iface}) 2026/09/26 20:41:07 Interface state was Down, requested Up, now Up",
            f"DEBUG: ({iface}) 2026/09/26 20:41:07 UDP bind has been updated",
        ]
        for client in server["clients"][:2]:
            peer = f"peer({client['client_public_key'][:4]}…{client['client_public_key'][-5:-1]})"
            lines += [
                f"DEBUG: ({iface}) 2026/09/26 20:41:12 {peer} - Received handshake initiation",
                f"DEBUG: ({iface}) 2026/09/26 20:41:12 {peer} - Sending handshake response",
            ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--empty", action="store_true", help="start with no servers")
    parser.add_argument("--label", default="demo (example data)", help="build label under the heading")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="awg-demo-")
    web_ui = REPO / "web-ui"
    app = create_flask_app(str(web_ui / "templates"), str(web_ui / "static"))
    secret_key = os.path.join(tmp, ".flask_secret_key")
    install_guards(app, secret_key_path=secret_key)
    socketio = create_socketio(app, None)
    # Settings as a deployment would have them: DEFAULT_MTU pinned by its variable (the
    # drawer shows it read-only), the rest stored; and the default admin/changeme
    # credential with its banner, as start.sh leaves a fresh volume.
    Path(tmp, "web_config.json").write_text(
        json.dumps({"servers": [], "settings": {"default_subnet": "10.10.0.0/24", "default_dns": "1.1.1.1, 9.9.9.9"}}),
        encoding="utf-8",
    )
    settings = Settings({"DEFAULT_MTU": "1420"})
    access = Access(os.path.join(tmp, ".htpasswd"), environ={})
    Path(access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")
    manager = DemoManager(
        socketio_instance=socketio, auto_start_servers=False, dns_servers=["1.1.1.1", "9.9.9.9"],
        default_enable_nat=True, default_block_lan_cidrs=True, config_dir=tmp, enable_geoip=True,
        default_mtu=1420, default_subnet="10.10.0.0/24", default_port=51820, settings=settings,
    )  # fmt: skip
    if not args.empty:
        seed(manager)
    log_file = os.path.join(tmp, "awg.log")
    write_log(log_file, manager)

    register_system_routes(app, manager, awg_log_file=log_file, nginx_port=str(args.port))
    register_server_routes(app, manager, to_bool=to_bool)
    register_settings_routes(app, manager, access, build_label=args.label,
                             rotate_secret_key=lambda: rotate_secret_key(app, secret_key))  # fmt: skip
    register_socket_handlers(socketio, manager, str(args.port))

    @app.route("/")
    def index():
        return render_page(manager, access, cache_bust=int(time.time()), build_label=args.label)

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(str(web_ui / "static"), filename)

    print(f"Demo panel on http://127.0.0.1:{args.port}/ (state in {tmp})", flush=True)
    socketio.run(app, host="127.0.0.1", port=args.port, allow_unsafe_werkzeug=True, log_output=False)


if __name__ == "__main__":
    main()
