"""Serve the real panel with invented example data: for trying the UI, the smoke
tests and screenshots, with no container and no AmneziaWG.

Everything is the production code (routes, guards, the event stream, the traffic loop
and its `awg show all dump` parser) except the system edges of the manager: keys, awg-quick, ip,
iptables, GeoIP and the egress probe are answered here. Addresses come from the
documentation ranges and keys are random, so nothing real can leak into a screenshot.
The traffic history starts with an invented day (seed_history), recorded through the
history's own path, so the Traffic dialog has 24 hours to show at once.

    uv run --no-project --python 3.14 --with-requirements web-ui/requirements.txt \
        tests/demo_server.py [--port 8099]

Then open http://127.0.0.1:8099/ (no login). State lives in a temp dir and is gone
on exit. The smoke tests run against it the same way they run against a container.
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import random
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "web-ui"))

from core.events import EventBroadcaster
from core.guards import install_guards
from core.helpers import to_bool
from core.runtime import create_flask_app
from core.settings import Access, Settings
from flask import send_from_directory
from routes.servers import register_server_routes
from routes.settings import register_settings_routes
from routes.system import register_system_routes, render_page
from services.amnezia_manager import AmneziaManager
from services.history import TrafficHistory
from services.netinfo import NetInfo

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
MiB, GiB = 1024**2, 1024**3


def random_key():
    return base64.b64encode(os.urandom(32)).decode()


class DemoNetInfo(NetInfo):
    """GeoIP and the egress probe answered from the tables above, not the network."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.egress = {}  # server_ip -> external IP

    def lookup_geoip(self, ip):
        return GEO.get(ip, (None, None))

    # The traffic loop's cache-only lookup would skip documentation-range addresses.
    def lookup_geoip_cached(self, ip):
        return self.lookup_geoip(ip)

    def get_route_for_source_ip(self, source_ip, destination="1.1.1.1"):
        return "demo"

    def detect_public_ip_from_source(self, source_ip, service):
        if source_ip not in self.egress:
            raise RuntimeError(f"no route to the internet from {source_ip}")
        return self.egress[source_ip], service


class DemoManager(AmneziaManager):
    """The real manager with its system edges answered in memory."""

    def __init__(self, **kwargs):
        self.running = set()  # interfaces that are "up"
        self.started = {}  # interface -> when it came up (the totals' `since`)
        self.peers = {}  # client public key -> {endpoint, handshake_at, rx, tx, read_at}
        self._live_rngs = {}  # client name -> its live traffic's random.Random
        self._public_ip_calls = 0
        super().__init__(**kwargs)
        # Replaced before the seed: until then the traffic loop has no server to look up.
        self.netinfo = DemoNetInfo(
            run_command=self.run_command,
            start_background_task=self.start_background_task,
            enable_geoip=self.netinfo.enable_geoip,
        )

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
            self.started[args[2]] = time.time()
        elif args[:2] == ["/usr/bin/awg-quick", "down"]:
            self.running.discard(args[2])
        elif args == ["/usr/bin/awg", "show", "all", "dump"]:
            return self.awg_dump()
        return ""

    def interface_state(self, interface):
        return "unknown" if interface in self.running else None

    def interface_totals(self, interface):
        """The interface's counters as the sum of its peers' (the demo has no framing to
        leave out), since its start."""
        if interface not in self.running:
            return None
        server = next(s for s in self.config["servers"] if s["interface"] == interface)
        peers = [self.peers.get(c["client_public_key"]) or {} for c in server["clients"]]
        return {
            "received_bytes": int(sum(p.get("rx", 0) for p in peers)),
            "sent_bytes": int(sum(p.get("tx", 0) for p in peers)),
            "since": int(self.started[interface]),
        }

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
                    # The invented day (DAYS) goes on live, each device at its own rate over
                    # the time since the last reading, so the charts carry on from the seeded
                    # history; a keepalive trickle in its quiet hours. tx is the device's download.
                    elapsed = now - peer.get("read_at", now)
                    peer["read_at"] = now
                    day = DAYS.get(client["name"])
                    rng = self._live_rngs.setdefault(client["name"], random.Random(client["name"] + "/live"))
                    state, down, up = day(_hour(now), rng, elapsed / 60, 0) if day else OFFLINE
                    if state != "o":
                        down, up = 0.004, 0.002
                    peer["tx"] += int(down * 1e6 / 8 * elapsed)
                    peer["rx"] += int(up * 1e6 / 8 * elapsed)
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
    traffic = {  # name -> (endpoint, seconds since handshake, rx: the device's upload, tx: its download)
        "iPhone": ("198.51.100.40:53412", 12, 214.6 * MiB, 1.39 * GiB),
        "MacBook": ("203.0.113.88:61022", 184, 802.1 * MiB, 6.59 * GiB),
        "iPad": ("198.51.100.40:50112", 3 * 86400 + 7260, 38.9 * MiB, 412.3 * MiB),
        "Pixel": ("203.0.113.201:40211", 47, 101.7 * MiB, 922.4 * MiB),
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

    manager.netinfo.egress = {home["server_ip"]: "198.51.100.17", travel["server_ip"]: "198.51.100.61"}
    for srv in (home, travel):
        manager.start_server(srv["id"])
        manager.probe_server_egress_ip(srv["id"])
    # Home has been up for days (the iPad's handshake is 3 d old), Travel since this morning.
    manager.started[home["interface"]] = now - (3 * 86400 + 4 * 3600)
    manager.started[travel["interface"]] = now - 5 * 3600
    seed_history(manager)


# --- an invented day of traffic, for the history ---------------------------------------
# What each device did at a time of day: (state, download, upload), rates in Mbit/s from
# the device's side; "o" online, "-" offline, "s" suspended. Ported from the step 13 mock.
OFFLINE = ("-", 0.0, 0.0)


def _hour(at):
    local = time.localtime(at)
    return local.tm_hour + local.tm_min / 60 + local.tm_sec / 3600


def _within(h, a, b):
    return a <= h < b if a <= b else h >= a or h < b


def _jitter(r, v, f):
    return v * (1 - f + 2 * f * r.random())


def _iphone(h, r, dt, _ago):
    if not _within(h, 6.8, 0.7):
        return OFFLINE
    down = _jitter(r, 0.12, 0.8)
    up = down * 0.15
    if _within(h, 7.65, 8.5):  # music on the way to work
        down, up = _jitter(r, 0.32, 0.1), 0.03
    if _within(h, 12.45, 12.6):  # photos syncing
        up = _jitter(r, 6.2, 0.15)
    if _within(h, 20.83, 22.5):  # an evening video
        down, up = _jitter(r, 4.4, 0.3), _jitter(r, 0.16, 0.3)
    if r.random() < 0.04 * dt:
        down, up = down + 1.5 + r.random() * 7, up + 0.1 + r.random() * 0.5
    return ("o", down, up)


def _macbook(h, r, dt, _ago):
    if not _within(h, 8.75, 24) or _within(h, 13.08, 13.75):
        return OFFLINE
    down, up = _jitter(r, 0.9 if h < 18.5 else 0.35, 0.6), _jitter(r, 0.18, 0.5)
    if _within(h, 10, 10.75) or _within(h, 15, 15.5):  # calls
        down, up = _jitter(r, 2.6, 0.2), _jitter(r, 2.2, 0.2)
    if _within(h, 14.17, 14.25):  # a system update
        down, up = _jitter(r, 39, 0.06), _jitter(r, 0.45, 0.2)
    if r.random() < 0.03 * dt:
        down, up = down + 2 + r.random() * 10, up + r.random() * 1.5
    return ("o", down, up)


def _router(_h, r, _dt, ago):
    # Suspended in the demo; until five hours ago it carried a household's background.
    return ("s", 0.0, 0.0) if ago < 5 * 3600 else ("o", _jitter(r, 1.6, 0.45), _jitter(r, 0.3, 0.4))


def _pixel(h, r, dt, _ago):
    if not (_within(h, 7.15, 7.85) or _within(h, 12, 13.2) or h >= 18.5):
        return OFFLINE
    down = _jitter(r, 1.1 if h >= 21 else 0.5, 0.5)  # maps on the way back, in the evening
    up = down * 0.2
    if r.random() < 0.05 * dt:
        down, up = down + 1 + r.random() * 5, up + r.random() * 0.6
    return ("o", down, up)


def _work_laptop(h, r, dt, _ago):
    if not _within(h, 9.08, 12.58):
        return OFFLINE
    down, up = _jitter(r, 1.2, 0.6), _jitter(r, 0.35, 0.5)
    if _within(h, 11.33, 11.4):
        down = _jitter(r, 12, 0.15)
    if r.random() < 0.03 * dt:
        down += 2 + r.random() * 6
    return ("o", down, up)


DAYS = {"iPhone": _iphone, "MacBook": _macbook, "Router": _router, "Pixel": _pixel, "Work laptop": _work_laptop}


def seed_history(manager, hours=24):
    """The invented day, through TrafficHistory.record as the loop would have recorded it
    every 7 s: counters that grow by each device's rates, a suspended client with no peer,
    a stopped server with no tick. Into a fresh history, swapped in at the end, because the
    live loop has already recorded a tick at the present. The live counters then carry on
    from where the day left them, so the next tick is no reset."""
    history = TrafficHistory()
    end = time.time()
    step = TrafficHistory.TICK_SECONDS
    rngs = {name: random.Random(name) for name in DAYS}
    counters = {}  # client id -> [rx, tx]: the device's upload, its download
    for server in manager.config["servers"]:
        for client in server["clients"]:
            peer = manager.peers.get(client["client_public_key"]) or {}
            counters[client["id"]] = [int(peer.get("rx", 0)), int(peer.get("tx", 0))]  # as the dump prints them
    at = end - hours * 3600
    while at < end:
        h = _hour(at)
        samples = {}
        for server in manager.config["servers"]:
            if server["interface"] not in manager.running:
                samples[server["id"]] = None
                continue
            samples[server["id"]] = clients = {}
            for client in server["clients"]:
                day = DAYS.get(client["name"])
                state, down, up = day(h, rngs[client["name"]], step / 60, end - at) if day else OFFLINE
                counter = counters[client["id"]]
                if state == "s":
                    clients[client["id"]] = ("s", None, None)
                    continue
                counter[0] += int(up * 1e6 / 8 * step)
                counter[1] += int(down * 1e6 / 8 * step)
                clients[client["id"]] = (state, *counter)
        history.record(at, samples)
        at += step
    for server in manager.config["servers"]:
        for client in server["clients"]:
            peer = manager.peers.get(client["client_public_key"])
            if peer:
                peer["rx"], peer["tx"] = counters[client["id"]]
    manager.history = history


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
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on; 0.0.0.0 for the LAN (no sign-in!)")
    parser.add_argument("--empty", action="store_true", help="start with no servers")
    parser.add_argument("--label", default="demo (example data)", help="build label under the heading")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="awg-demo-")
    web_ui = REPO / "web-ui"
    app = create_flask_app(str(web_ui / "templates"), str(web_ui / "static"))
    install_guards(app)
    # Settings as a deployment would have them: AWG_LOG_LEVEL pinned by its variable (the
    # drawer shows it read-only), the rest stored; and the default admin/changeme
    # credential with its banner, as start.sh leaves a fresh volume.
    Path(tmp, "web_config.json").write_text(
        json.dumps({"servers": [], "settings": {"log_level": "INFO"}}),
        encoding="utf-8",
    )
    settings = Settings({"AWG_LOG_LEVEL": "error"})
    access = Access(os.path.join(tmp, ".htpasswd"), environ={})
    Path(access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")
    manager = DemoManager(
        events=EventBroadcaster(), auto_start_servers=False, dns_servers=["1.1.1.1", "9.9.9.9"],
        default_enable_nat=True, default_block_lan_cidrs=True, config_dir=tmp, enable_geoip=True,
        default_mtu=1420, default_subnet="10.10.0.0/24", default_port=51820, settings=settings,
    )  # fmt: skip
    if not args.empty:
        seed(manager)
    log_file = os.path.join(tmp, "awg.log")
    write_log(log_file, manager)

    register_system_routes(app, manager, awg_log_file=log_file, nginx_port=str(args.port))
    register_server_routes(app, manager, to_bool=to_bool)
    register_settings_routes(app, manager, access, build_label=args.label)

    @app.route("/")
    def index():
        return render_page(manager, access, cache_bust=int(time.time()), build_label=args.label)

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(str(web_ui / "static"), filename)

    print(f"Demo panel on http://{args.host}:{args.port}/ (state in {tmp})", flush=True)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no line per request
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
