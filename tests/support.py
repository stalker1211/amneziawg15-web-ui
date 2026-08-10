"""Shared test helpers.

`AmneziaManager` touches the network and the system on construction (public IP
detection, /var/log, `wg genkey`, a traffic-monitor background task). `build_manager`
returns an instance with exactly those edges stubbed and nothing else, so tests
exercise the real logic.
"""

import os
import sys
import tempfile

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
    """Records emits; runs background tasks inline-free (never starts them)."""

    def __init__(self):
        self.emitted = []

    def emit(self, event, data=None, **_kwargs):
        self.emitted.append((event, data))

    def sleep(self, _seconds):
        return None

    def start_background_task(self, _target, *_args, **_kwargs):
        return None


def build_manager(**overrides):
    """Return an AmneziaManager wired to temp dirs with system edges stubbed."""
    from services.amnezia_manager import AmneziaManager  # noqa: PLC0415

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

        def run_command(self, args):
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
