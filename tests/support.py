"""Shared test helpers.

`AmneziaManager` touches the network and the system on construction (public IP
detection, /var/log, `wg genkey`, a traffic-monitor background task). `build_manager`
returns an instance with exactly those edges stubbed and nothing else, so tests
exercise the real logic.
"""

import os
import subprocess
import sys
import tempfile
from unittest import mock

# Test doubles intentionally ignore arguments and skip docstrings.
# pylint: disable=missing-function-docstring,unused-argument,import-outside-toplevel

WEB_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web-ui")
GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

if WEB_UI_DIR not in sys.path:
    sys.path.insert(0, WEB_UI_DIR)

# Deterministic stand-ins. Real `wg genkey` output shape: 44 chars of base64 ending
# in '='. Values differ per role so a swapped key is visible in a golden file.
SERVER_PRIVATE_KEY = "c2VydmVyUFJJVkFURWtleTAwMDAwMDAwMDAwMDAwMDA="
SERVER_PUBLIC_KEY = "c2VydmVyUFVCTElDa2V5MDAwMDAwMDAwMDAwMDAwMDA="
CLIENT_PRIVATE_KEY = "Y2xpZW50UFJJVkFURWtleTAwMDAwMDAwMDAwMDAwMDA="
CLIENT_PUBLIC_KEY = "Y2xpZW50UFVCTElDa2V5MDAwMDAwMDAwMDAwMDAwMDA="
PRESHARED_KEY = "cHJlc2hhcmVka2V5MDAwMDAwMDAwMDAwMDAwMDAwMDA="
HEADER_PROTECTION_KEY = "aGVhZGVyUFJPVEVDVElPTmtleTAwMDAwMDAwMDAwMDA="
PUBLIC_IP = "203.0.113.9"


class FakeSocketIO:
    """Records emits and sleeps. Background tasks are dropped unless `run_tasks`,
    in which case they run inline (the traffic loop needs a way out: see StopLoop)."""

    def __init__(self, run_tasks=False):
        self.emitted = []
        self.slept = []
        self.run_tasks = run_tasks

    def emit(self, event, data=None, **_kwargs):
        self.emitted.append((event, data))

    def sleep(self, seconds):
        self.slept.append(seconds)

    def start_background_task(self, target, *args, **kwargs):
        if self.run_tasks:
            target(*args, **kwargs)


class FakeSubprocess:
    """Stands in for `subprocess.run`: records every call and answers from a script.

    `respond(prefix, result)` matches calls whose argv starts with `prefix`; the latest
    matching rule wins, and unmatched calls succeed with empty stdout. `result` is
    stdout (str), a non-zero exit code (int), an exception to raise, or a callable
    taking (argv, kwargs) that returns one of those.
    """

    def __init__(self):
        self.calls = []
        self.rules = []

    def respond(self, prefix, result):
        self.rules.append((tuple(prefix), result))
        return self

    def with_wg_keys(self):
        return (
            self.respond(["wg", "genkey"], SERVER_PRIVATE_KEY)
            .respond(["wg", "pubkey"], SERVER_PUBLIC_KEY)
            .respond(["wg", "genpsk"], PRESHARED_KEY)
        )

    def argvs(self):
        return [call["args"] for call in self.calls]

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        result = ""
        for prefix, candidate in reversed(self.rules):
            if tuple(args[: len(prefix)]) == prefix:
                result = candidate
                break
        if callable(result):
            result = result(args, kwargs)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, int):
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(result, args, output="", stderr="failed")
            return subprocess.CompletedProcess(args, result, stdout="", stderr="failed")
        return subprocess.CompletedProcess(args, 0, stdout=result, stderr="")


class SystemPaths:
    """Stand in for the two system probes the manager makes.

    `interfaces` lists the interfaces that are up: their sysfs operstate reads
    "unknown", as amneziawg-go's tun does (`states` overrides one, e.g. "down");
    any other interface does not exist. `scripts` says whether
    /app/scripts/*_iptables.sh exist. Every other path is real.
    """

    def __init__(self, interfaces=(), scripts=True):
        self.interfaces = set(interfaces)
        self.states = {}
        self.scripts = scripts
        self._real_exists = os.path.exists
        self._patchers = [
            mock.patch("os.path.exists", self._exists),
            mock.patch("services.amnezia_manager.AmneziaManager.interface_state", staticmethod(self._state)),
        ]

    def _exists(self, path):
        path = str(path)
        if path.startswith("/app/scripts/"):
            return self.scripts
        return self._real_exists(path)

    def _state(self, interface):
        return self.states.get(interface, "unknown" if interface in self.interfaces else None)

    def start(self, test_case):
        for patcher in self._patchers:
            patcher.start()
            test_case.addCleanup(patcher.stop)
        return self


def build_real_manager(test_case, fake_subprocess=None, **overrides):
    """An AmneziaManager running its real command, key and interface code.

    Only the constructor's own edges are stubbed (directories, public-IP detection,
    the traffic thread); every subprocess call goes to a FakeSubprocess, patched for
    the duration of `test_case`. Returns (manager, fake_subprocess).
    """
    from services.amnezia_manager import AmneziaManager

    fake = fake_subprocess or FakeSubprocess().with_wg_keys()
    patcher = mock.patch("services.amnezia_manager.subprocess.run", fake)
    patcher.start()
    test_case.addCleanup(patcher.stop)

    class _RealSystemManager(AmneziaManager):
        def ensure_directories(self):
            os.makedirs(self.config_dir, exist_ok=True)
            os.makedirs(self.wireguard_config_dir, exist_ok=True)

        def detect_public_ip(self):
            return PUBLIC_IP

        def start_traffic_monitoring(self):
            return None

    tmp = tempfile.mkdtemp(prefix="awg-sys-")
    kwargs = {
        "socketio_instance": FakeSocketIO(run_tasks=True),
        "auto_start_servers": False,
        "default_mtu": 1420,
        "default_subnet": "10.0.0.0/24",
        "default_port": 51820,
        "dns_servers": ["1.1.1.1"],
        "default_enable_nat": True,
        "default_block_lan_cidrs": True,
        "config_dir": tmp,
        "wireguard_config_dir": tmp,
        "config_file": os.path.join(tmp, "web_config.json"),
    }
    kwargs.update(overrides)
    return _RealSystemManager(**kwargs), fake


def build_manager(**overrides):
    """Return an AmneziaManager wired to temp dirs with system edges stubbed."""
    from services.amnezia_manager import AmneziaManager

    class _TestManager(AmneziaManager):
        """Overrides only the boundaries: no network, no /var/log, no real keys."""

        def __init__(self, *args, **kwargs):
            self.commands = []
            self._server_keys_issued = False
            super().__init__(*args, **kwargs)

        def ensure_directories(self):
            os.makedirs(self.config_dir, exist_ok=True)
            os.makedirs(self.wireguard_config_dir, exist_ok=True)

        def detect_public_ip(self):
            return PUBLIC_IP

        def start_traffic_monitoring(self):
            return None

        def run_command(self, args, env=None):
            self.commands.append(list(args))
            joined = " ".join(args)
            if "genkey" in joined:
                return SERVER_PRIVATE_KEY
            if "genpsk" in joined:
                return PRESHARED_KEY
            if "pubkey" in joined:
                return SERVER_PUBLIC_KEY
            # awg-quick up/down, ip link show, awg show: success with no output.
            return ""

        def derive_public_key(self, private_key):
            return SERVER_PUBLIC_KEY

        # Keys are deterministic so golden files are stable. Client and server keys
        # differ so a renderer mixing them up fails visibly.
        def generate_wireguard_keys(self):
            if self._server_keys_issued:
                return {"private_key": CLIENT_PRIVATE_KEY, "public_key": CLIENT_PUBLIC_KEY}
            self._server_keys_issued = True
            return {"private_key": SERVER_PRIVATE_KEY, "public_key": SERVER_PUBLIC_KEY}

        def generate_header_protection_key(self):
            return HEADER_PROTECTION_KEY

        def apply_live_config(self, interface):
            return True

        def start_server(self, server_id):
            return True

    tmp = tempfile.mkdtemp(prefix="awg-test-")
    kwargs = {
        "socketio_instance": FakeSocketIO(),
        "auto_start_servers": False,
        "default_mtu": 1420,
        "default_subnet": "10.0.0.0/24",
        "default_port": 51820,
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
        "default_enable_nat": True,
        "default_block_lan_cidrs": True,
        "config_dir": tmp,
        "wireguard_config_dir": tmp,
        "config_file": os.path.join(tmp, "web_config.json"),
    }
    kwargs.update(overrides)
    return _TestManager(**kwargs)


def build_app(secret_key_path=None, awg_log_file="/nonexistent/awg.log", manager=None, access=None):
    """The Flask app wired as app.py does it -- real guards and routes -- around a stubbed manager.

    app.py itself is not imported: it builds everything at import time against
    /etc/amnezia and a real AmneziaManager. The settings routes are registered when
    the manager has settings (build_manager(settings=...)) and `access` is given.
    """
    from core.guards import install_guards, rotate_secret_key
    from core.helpers import to_bool
    from flask import Flask
    from routes.servers import register_server_routes
    from routes.settings import register_settings_routes
    from routes.system import register_system_routes

    manager = manager or build_manager()

    app = Flask(__name__)
    app.config.update(TESTING=True)
    if secret_key_path is None:
        secret_key_path = os.path.join(manager.config_dir, ".flask_secret_key")
    install_guards(app, secret_key_path=secret_key_path)

    register_system_routes(app, manager, awg_log_file=awg_log_file, nginx_port="80")
    register_server_routes(app, manager, to_bool=to_bool)
    if manager.settings is not None and access is not None:
        register_settings_routes(
            app,
            manager,
            access,
            build_label="test",
            rotate_secret_key=lambda: rotate_secret_key(app, secret_key_path),
        )
    return app, manager


def normalize_conf(text):
    """Strip values that legitimately change between runs (timestamps)."""
    lines = []
    for line in text.splitlines():
        if line.startswith("# Generated:"):
            line = "# Generated: <timestamp>"
        lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"


def read_golden(name):
    with open(os.path.join(GOLDEN_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def write_golden(name, text):
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    with open(os.path.join(GOLDEN_DIR, name), "w", encoding="utf-8") as f:
        f.write(text)
