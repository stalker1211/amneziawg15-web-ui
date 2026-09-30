"""Core service logic for managing AmneziaWG servers and clients."""

import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import ClassVar
from urllib.parse import urlparse

import requests
from core.helpers import is_valid_ip, sanitize_config_value, to_bool
from core.logging_setup import get_logger
from requests.adapters import HTTPAdapter

from services import generator

logger = get_logger(__name__)


class AmneziaManager:
    """Manage VPN server lifecycle, clients, configs, and runtime telemetry."""

    # Protocol table — the authoritative definition. Adding a generation means
    # extending these tuples and nothing else on the backend; the frontend mirror
    # lives in static/js/protocols.js.
    DEFAULT_PROTOCOL = "AWG 1.5"
    SUPPORTED_PROTOCOLS = ("AWG 1.5", "AWG 2.0", "AWG 3.0", "AWG 3.1")

    # Capabilities, keyed by the protocols that have them.
    PROTOCOLS_WITH_S34 = ("AWG 2.0", "AWG 3.0", "AWG 3.1")
    PROTOCOLS_WITH_HEADER_RANGES = ("AWG 2.0", "AWG 3.0", "AWG 3.1")
    PROTOCOLS_WITH_AWG3 = ("AWG 3.0", "AWG 3.1")
    PROTOCOLS_WITH_AWG31 = ("AWG 3.1",)

    # Client-side params: may differ between server and client, so they are only
    # written into client configs.
    CLIENT_ONLY_PARAM_KEYS = ("Jc", "Jmin", "Jmax", "I1", "I2", "I3", "I4", "I5")

    # AWG 3.x client-side params. Range-valued ("a" or "a-b"); empty means unset,
    # in which case amneziawg-go keeps its built-in WireGuard defaults.
    CLIENT_TIMING_PARAM_KEYS = (
        "RekeyAfterTime",
        "RekeyTimeout",
        "RejectAfterTime",
        "KeepaliveTimeout",
        "MaxHandshakeAttempts",
    )
    CLIENT_AWG3_PARAM_KEYS = ("ContentPaddingAddition",) + CLIENT_TIMING_PARAM_KEYS

    # Server-side params: must be identical on both ends, so they are written into
    # the server config and mirrored into every client config.
    TRANSPORT_PARAM_KEYS = ("S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4")
    TRANSPORT_AWG3_PARAM_KEYS = ("HeaderProtectionKey",)
    TRANSPORT_AWG31_PARAM_KEYS = ("RandomTrailers", "DisableCookies")

    # CPS tags amneziawg-go builds I1-I5 from (device/obf.go obfBuilders). `<c>` is
    # only in the kernel module; the daemon refuses it as an unknown tag.
    CPS_TAGS = ("b", "t", "r", "rc", "rd", "d", "ds", "dz")

    # WireGuard's timer constants (device/constants.go), which the AWG 3.x timing
    # params replace; used to check a partly set group against its defaults.
    WG_DEFAULT_TIMINGS: ClassVar[dict[str, int]] = {
        "RekeyAfterTime": 120,
        "RekeyTimeout": 5,
        "RejectAfterTime": 180,
        "KeepaliveTimeout": 10,
        "MaxHandshakeAttempts": 18,
    }

    # S1-S4, Jc, Jmin, Jmax are uint16 on the wire; H1-H4 are uint32.
    UINT16_MAX = 0xFFFF
    UINT32_MAX = 0xFFFFFFFF

    # amneziawg-go uses the first 12 bytes of each packet's S-padding as the header
    # protection cipher nonce, so every S value must be at least this large once a
    # HeaderProtectionKey is set (device/noise-types.go: HeaderCipherNonceSize).
    HEADER_CIPHER_NONCE_SIZE = 12

    # What a client routes through the tunnel unless its Edit drawer says otherwise
    # (a narrower list is split tunnelling). ::/0 is not the default: tested on iOS,
    # IPv6 does not leak past a 0.0.0.0/0 tunnel, and a Linux device with IPv6 off
    # cannot bring ::/0 up. Any IPv6 a client does send is dropped by the server's
    # source check (its peer entry is the client's /32).
    DEFAULT_ALLOWED_IPS = "0.0.0.0/0"

    # A client is online (and "active") after a handshake this recent; with keepalive
    # 25 a connected device handshakes about every 2 minutes.
    ACTIVE_WITHIN_SECONDS = 5 * 60

    # GeoIP lookups are cached to avoid rate limits; bounded so the dict cannot grow
    # without limit as new client endpoints appear.
    GEOIP_CACHE_TTL_SECONDS = 24 * 3600
    # A failed lookup (rate limit, timeout) is retried after this, not after a day.
    GEOIP_FAILURE_TTL_SECONDS = 10 * 60
    GEOIP_CACHE_MAX_ENTRIES = 512

    EGRESS_PROBE_SERVICES = (
        "https://api.ipify.org",
        "https://ident.me",
        "https://icanhazip.com",
    )
    PUBLIC_IP_SERVICES = EGRESS_PROBE_SERVICES

    # awg-quick runs amneziawg-go through this wrapper when the daemon logs, so its
    # output lands in AWG_LOG_FILE (scripts/amneziawg-go-logged.sh).
    LOGGED_DAEMON = "/usr/local/bin/amneziawg-go-logged"
    DAEMON_LOG_LEVELS = ("off", "error", "debug")

    def __init__(
        self,
        *,
        socketio_instance,
        auto_start_servers,
        default_mtu,
        default_subnet,
        default_port,
        dns_servers,
        default_enable_nat,
        default_block_lan_cidrs,
        config_dir="/etc/amnezia",
        wireguard_config_dir=None,
        config_file=None,
        enable_geoip=True,
        awg_log_level="off",
        settings=None,
    ):
        # Request handlers and the traffic monitor are separate threads; config
        # writes go through this one at a time (see save_config).
        self._save_lock = threading.Lock()
        self.socketio = socketio_instance

        self.auto_start_servers_enabled = auto_start_servers
        self.default_mtu = default_mtu
        self.default_subnet = default_subnet
        self.default_port = default_port
        self.dns_servers = dns_servers
        self.default_enable_nat = default_enable_nat
        self.default_block_lan_cidrs = default_block_lan_cidrs

        self.config_dir = config_dir
        self.wireguard_config_dir = wireguard_config_dir or os.path.join(config_dir, "amneziawg")
        self.config_file = config_file or os.path.join(config_dir, "web_config.json")

        self.enable_geoip = enable_geoip
        self.awg_log_level = awg_log_level if awg_log_level in self.DAEMON_LOG_LEVELS else "off"

        self.config = self.load_config()
        # Stored settings, pinned by the environment (core/settings.py): they replace
        # the defaults passed in above. The tests pass none.
        self.settings = settings
        if settings is not None:
            if settings.resolve(self.config.setdefault("settings", {})):
                self.save_config()
            self.apply_settings()
        self.ensure_directories()
        # Saved at once, so a public IP change before the next save is still caught.
        if self.backfill_config_fingerprints():
            self.save_config()
        self.public_ip = self.detect_public_ip() or self.last_known_public_ip()

        # The last `awg show all dump`, parsed (read_telemetry). Memory only: the
        # monitor never writes the config.
        self._telemetry = {"at": 0.0, "interfaces": {}}

        # Cache GeoIP lookups to avoid rate limits and latency
        # { ip: {"ts": epoch_seconds, "label": str, "raw": dict} }
        self._geoip_cache = {}
        # Addresses a background task is looking up now (lookup_geoip_cached).
        self._geoip_pending = set()

        # Bring back the servers that were running (see auto_start_servers).
        if self.auto_start_servers_enabled:
            self.auto_start_servers()

        # Start real-time traffic monitoring
        self.start_traffic_monitoring()

    def ensure_directories(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.wireguard_config_dir, exist_ok=True)
        os.makedirs("/var/log/amnezia", exist_ok=True)

    def detect_public_ip(self):
        """The host's public IPv4 address, or None when no service answered.

        None rather than a guess: the answer goes into every client config's Endpoint.
        The old fallbacks -- the `ip route get` source (the LAN address on macvlan) or
        "YOUR_SERVER_IP" -- sent every device to the wrong place after a refresh
        during an outage. HTTPS only, so nobody on the path can supply the answer.
        """
        for service in self.PUBLIC_IP_SERVICES:
            try:
                response = requests.get(service, timeout=5)
            except Exception:  # pylint: disable=broad-exception-caught  -- try the next one
                continue
            ip = response.text.strip() if response.status_code == 200 else ""
            if is_valid_ip(ip):
                logger.info("Detected public IP: %s", ip)
                return ip
        logger.warning("Could not detect the public IP: none of %s answered", ", ".join(self.PUBLIC_IP_SERVICES))
        return None

    def last_known_public_ip(self):
        """The address the servers were last given, for a boot without internet."""
        return next((s["public_ip"] for s in self.config["servers"] if is_valid_ip(s.get("public_ip"))), None)

    @staticmethod
    def _config_line(params, key):
        """Render 'Key = value' for a set param, or '' when unset/empty."""
        value = params.get(key)
        if value is None or value == "":
            return ""
        if isinstance(value, bool):
            value = "on" if value else "off"
        return f"{key} = {value}\n"

    @staticmethod
    def sanitize_name(value, fallback="unnamed"):
        """Return a display name that is safe to embed in a .conf comment.

        Server and client names are written into the generated configs as
        `# Client: <name>` lines. A newline in the name would end the comment and let
        the rest be parsed as configuration directives, so collapse whitespace and cap
        the length.
        """
        cleaned = sanitize_config_value(value if value is not None else "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:64] or fallback

    @staticmethod
    def validate_subnet(value):
        """Return a normalized IPv4 CIDR, rejecting anything else.

        The subnet is written into config files and passed to the iptables scripts,
        so it must never carry shell metacharacters or stray whitespace.
        """
        raw = sanitize_config_value(value if value is not None else "")
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid subnet '{raw}': expected CIDR such as 10.0.0.0/24") from exc

        if network.version != 4:
            raise ValueError(f"Invalid subnet '{raw}': only IPv4 subnets are supported")
        if network.prefixlen > 30:
            raise ValueError(f"Subnet '{raw}' is too small: use /30 or larger")

        return str(network)

    @staticmethod
    def is_valid_wireguard_key(value):
        """Check for a base64-encoded 32-byte key, as produced by 'awg genkey'."""
        if not isinstance(value, str):
            return False
        candidate = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=", candidate):
            return False
        try:
            # binascii.Error (raised on malformed base64) subclasses ValueError.
            return len(base64.b64decode(candidate, validate=True)) == 32
        except ValueError:
            return False

    class _SourceAddressAdapter(HTTPAdapter):
        """Requests adapter that binds outbound sockets to a specific source IP."""

        def __init__(self, source_ip, **kwargs):
            self._source_ip = source_ip
            super().__init__(**kwargs)

        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs["source_address"] = (self._source_ip, 0)
            return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

        def proxy_manager_for(self, proxy, **proxy_kwargs):
            proxy_kwargs["source_address"] = (self._source_ip, 0)
            return super().proxy_manager_for(proxy, **proxy_kwargs)

    def detect_public_ip_from_source(self, source_ip, service):
        """Detect external IP for traffic originating from a specific source IP."""
        if not is_valid_ip(source_ip):
            raise ValueError(f"Invalid source IP: {source_ip}")

        if service not in self.EGRESS_PROBE_SERVICES:
            raise ValueError(f"Unsupported egress probe service: {service}")

        with requests.Session() as session:
            adapter = self._SourceAddressAdapter(source_ip)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            try:
                response = session.get(service, timeout=8)
                if response.status_code != 200:
                    raise RuntimeError(f"{service}: HTTP {response.status_code}")

                body = response.text.strip()
                if body and is_valid_ip(body):
                    return body, service

                raise RuntimeError(f"{service}: invalid IP response '{body[:120]}'")
            except Exception as e:
                raise RuntimeError(f"{service}: {e}") from e

    def get_next_egress_probe_service(self, server):
        """Rotate egress probe services for a server across refreshes."""
        previous_service = None
        probe = server.get("egress_probe") if isinstance(server, dict) else None
        if isinstance(probe, dict):
            previous_service = probe.get("service")

        services = list(self.EGRESS_PROBE_SERVICES)
        if previous_service in services:
            previous_index = services.index(previous_service)
            return services[(previous_index + 1) % len(services)]

        return services[0]

    def format_probe_service_name(self, service):
        """Return a short host label for a probe service URL."""
        if not service or not isinstance(service, str):
            return None

        try:
            parsed = urlparse(service)
            host = (parsed.hostname or "").strip().lower()
            return host or service.strip()
        except Exception:
            return service.strip()

    def get_route_for_source_ip(self, source_ip, destination="1.1.1.1"):
        """Return Linux route decision for destination when source IP is forced."""
        result = {
            "destination": destination,
            "source_ip": source_ip,
            "raw": "",
            "dev": None,
            "via": None,
            "src": None,
        }

        if not is_valid_ip(source_ip):
            return result

        output = self.run_command(["ip", "route", "get", destination, "from", source_ip])
        if output is None:
            result["raw"] = "route lookup failed"
            return result

        line = (output.splitlines() or [""])[0]
        result["raw"] = line
        for key in ("dev", "via", "src"):
            match = re.search(rf"\b{key}\s+(\S+)", line)
            result[key] = match.group(1) if match else None
        return result

    def probe_server_egress_ip(self, server_id):
        """Probe external egress IP for a specific server from inside the container."""
        server = self.get_server(server_id)
        if not server:
            return None

        source_ip = server.get("server_ip")
        route = self.get_route_for_source_ip(source_ip)
        service = self.get_next_egress_probe_service(server)

        probe = {
            "source_ip": source_ip,
            "route": route,
            "checked_at": int(time.time()),
            "external_ip": None,
            "service": service,
            "error": None,
        }

        try:
            external_ip, service = self.detect_public_ip_from_source(source_ip, service)
            probe["external_ip"] = external_ip
            probe["service"] = service
            geo_label, geo_country_code = self.lookup_geoip(external_ip)
            probe["external_ip_geo"] = geo_label
            probe["external_ip_geo_country_code"] = geo_country_code
        except Exception as e:
            probe["error"] = str(e)

        server["egress_probe"] = probe
        self.save_config()
        return probe

    def _cache_geoip(self, ip, now, label, country_code, raw, failed=False):
        """Store a GeoIP result, evicting expired and then oldest entries.

        Unbounded growth was slow but real: one entry per distinct client endpoint IP,
        never removed.
        """
        self._geoip_cache[ip] = {
            "ts": now,
            "label": label,
            "country_code": country_code,
            "raw": raw,
            "failed": failed,
        }

        if len(self._geoip_cache) <= self.GEOIP_CACHE_MAX_ENTRIES:
            return

        for key, entry in list(self._geoip_cache.items()):
            if (now - entry.get("ts", 0)) >= self.GEOIP_CACHE_TTL_SECONDS:
                del self._geoip_cache[key]

        # Still over budget (many fresh entries): drop the oldest.
        if len(self._geoip_cache) > self.GEOIP_CACHE_MAX_ENTRIES:
            for key, _ in sorted(self._geoip_cache.items(), key=lambda kv: kv[1].get("ts", 0)):
                if len(self._geoip_cache) <= self.GEOIP_CACHE_MAX_ENTRIES:
                    break
                del self._geoip_cache[key]

    def _geoip_candidate(self, ip):
        """The stripped address when GeoIP is on and it is public, else None."""
        if not self.enable_geoip or not ip or not isinstance(ip, str):
            return None
        ip = ip.strip()
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return None
        return ip

    def _geoip_fresh(self, ip, now):
        """The cached (label, country code), or None when absent or expired."""
        cached = self._geoip_cache.get(ip)
        if not isinstance(cached, dict):
            return None
        ttl = self.GEOIP_FAILURE_TTL_SECONDS if cached.get("failed") else self.GEOIP_CACHE_TTL_SECONDS
        if (now - cached.get("ts", 0)) >= ttl:
            return None
        return (cached.get("label"), cached.get("country_code"))

    def lookup_geoip_cached(self, ip):
        """(label, country code) from the cache only; a miss is looked up in the background.

        For the traffic loop, which must not wait on ipapi.co (up to 2 s per new
        client endpoint): the label appears on the next tick.
        """
        ip = self._geoip_candidate(ip)
        if not ip:
            return (None, None)
        fresh = self._geoip_fresh(ip, time.time())
        if fresh is not None:
            return fresh
        if ip not in self._geoip_pending:
            self._geoip_pending.add(ip)

            def resolve():
                try:
                    self.lookup_geoip(ip)
                finally:
                    self._geoip_pending.discard(ip)

            self.socketio.start_background_task(resolve)
        return (None, None)

    def lookup_geoip(self, ip):
        """Return (geo label, country code) for a public IP with caching."""
        ip = self._geoip_candidate(ip)
        if not ip:
            return (None, None)

        now = time.time()
        fresh = self._geoip_fresh(ip, now)
        if fresh is not None:
            return fresh

        def format_geo_label(raw):
            if not isinstance(raw, dict):
                return None
            country = raw.get("country_name") or raw.get("country") or raw.get("countryCode")
            city = raw.get("city")
            region = raw.get("region") or raw.get("regionName")

            loc_parts = [p for p in [city, region] if p]
            loc = ", ".join(loc_parts).strip()

            if country and loc:
                return f"{country} / {loc}"
            if country:
                return str(country)
            if loc:
                return loc
            return None

        def extract_country_code(raw):
            if not isinstance(raw, dict):
                return None
            cc = raw.get("country_code") or raw.get("countryCode") or raw.get("country")
            if isinstance(cc, str):
                cc = cc.strip().upper()
                if re.fullmatch(r"[A-Z]{2}", cc):
                    return cc
            return None

        try:
            resp = requests.get(
                f"https://ipapi.co/{ip}/json/",
                timeout=2,
                headers={"User-Agent": "amneziawg-web-ui"},
            )
            if resp.status_code != 200:
                self._cache_geoip(ip, now, None, None, {"status": resp.status_code}, failed=True)
                return (None, None)

            content_type = resp.headers.get("content-type", "")
            data = resp.json() if content_type.startswith("application/json") else {}
            label = format_geo_label(data)
            country_code = extract_country_code(data)
            self._cache_geoip(ip, now, label, country_code, data)
            return (label, country_code)
        except Exception:
            self._cache_geoip(ip, now, None, None, {"error": "lookup_failed"}, failed=True)
            return (None, None)

    def apply_settings(self):
        """Take the new-server defaults, GeoIP and the daemon's log level from the settings."""
        values = self.settings.values
        self.default_mtu = values["default_mtu"]
        self.default_subnet = values["default_subnet"]
        self.default_port = values["default_port"]
        self.dns_servers = [dns.strip() for dns in values["default_dns"].split(",") if dns.strip()]
        self.default_enable_nat = values["enable_nat"]
        self.default_block_lan_cidrs = values["block_lan_cidrs"]
        self.enable_geoip = values["geoip"]
        self.awg_log_level = values["awg_log_level"]

    def auto_start_servers(self):
        """At boot, restore each server's last start/stop state.

        `status` is written by start_server/stop_server only, so it is what was last
        asked for: a server stopped in the panel stays stopped. (Until 2.4 a
        creation-time `auto_start` flag decided, so a stopped server came back up.)
        """
        logger.info("Restoring the servers that were running...")
        for server in self.config["servers"]:
            if server.get("status") != "running" or not os.path.exists(server["config_path"]):
                continue
            if self.get_server_status(server["id"]) == "running":
                continue
            logger.info("Starting server: %s", server["name"])
            try:
                self.start_server(server["id"])
            except Exception as e:
                # Never crash the Web UI on boot due to a VPN startup failure.
                logger.error("Auto-start failed for server '%s': %s", server.get("name", server.get("id")), e)

    def normalize_protocol(self, value):
        """Map any accepted spelling to a canonical name from SUPPORTED_PROTOCOLS.

        Accepts "AWG 3.0", "3.0", "awg3.0", "AWG_3.0"; anything unknown (including
        non-strings) falls back to DEFAULT_PROTOCOL rather than raising, because this
        runs over stored config as well as API input.
        """
        if not isinstance(value, str):
            return self.DEFAULT_PROTOCOL

        normalized = value.strip().upper().replace("_", " ")
        for protocol in self.SUPPORTED_PROTOCOLS:
            version = protocol.split(" ", 1)[1]  # "AWG 3.0" -> "3.0"
            if normalized in {protocol, version, f"AWG{version}"}:
                return protocol
        return self.DEFAULT_PROTOCOL

    def protocol_supports_s34(self, protocol):
        """S3/S4 padding: AWG 2.0 and later."""
        return self.normalize_protocol(protocol) in self.PROTOCOLS_WITH_S34

    def protocol_supports_header_ranges(self, protocol):
        """H1-H4 may be ranges ("1200-1400"): AWG 2.0 and later."""
        return self.normalize_protocol(protocol) in self.PROTOCOLS_WITH_HEADER_RANGES

    def protocol_supports_awg3(self, protocol):
        """AWG 3.x adds header protection, content padding and tunable timings."""
        return self.normalize_protocol(protocol) in self.PROTOCOLS_WITH_AWG3

    def protocol_supports_awg31(self, protocol):
        """AWG 3.1 adds random packet trailers and optional cookie suppression."""
        return self.normalize_protocol(protocol) in self.PROTOCOLS_WITH_AWG31

    def parse_uint_range(self, value, key="value"):
        """Parse an AWG 3.0 'a' or 'a-b' range, mirroring device/noise-types.go.

        Returns the normalized string form, or "" when unset.
        """
        raw = sanitize_config_value(value if value is not None else "")
        if not raw:
            return ""

        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", raw)
        if not match:
            raise ValueError(f"{key} must be an integer or range 'a-b', got '{raw}'")

        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) is not None else low
        if high < low:
            raise ValueError(f"{key} range '{raw}' is inverted: start must be <= end")
        for bound in (low, high):
            if bound > 0xFFFFFFFF:
                raise ValueError(f"{key} value '{raw}' exceeds the uint32 range")

        return str(low) if low == high else f"{low}-{high}"

    def extract_transport_params(self, params, protocol=None):
        if not isinstance(params, dict):
            return {}

        normalized_protocol = self.normalize_protocol(protocol)
        result = {}
        for key in self.TRANSPORT_PARAM_KEYS:
            if key in ("S3", "S4") and not self.protocol_supports_s34(normalized_protocol):
                continue
            value = params.get(key)
            if value is None or value == "":
                continue
            result[key] = value

        if self.protocol_supports_awg3(normalized_protocol):
            for key in self.TRANSPORT_AWG3_PARAM_KEYS:
                value = sanitize_config_value(params.get(key) or "")
                if value:
                    result[key] = value

        if self.protocol_supports_awg31(normalized_protocol):
            for key in self.TRANSPORT_AWG31_PARAM_KEYS:
                if key in params:
                    result[key] = to_bool(params.get(key))

        return result

    def extract_client_params(self, params):
        if not isinstance(params, dict):
            return {}

        result = {}
        for key in self.CLIENT_ONLY_PARAM_KEYS:
            value = params.get(key)
            if key.startswith("I"):
                result[key] = sanitize_config_value(value or "")
            elif value is not None and value != "":
                result[key] = value
        for key in ("I1", "I2", "I3", "I4", "I5"):
            result.setdefault(key, "")

        # AWG 3.0 params are kept whenever set, regardless of the server's current
        # protocol, so downgrading and re-upgrading a server does not lose them.
        # Rendering is gated on the protocol instead.
        for key in self.CLIENT_AWG3_PARAM_KEYS:
            value = sanitize_config_value(params.get(key) or "")
            if value:
                result[key] = value

        return result

    def default_client_defaults(self):
        return {
            "Jc": 8,
            "Jmin": 8,
            "Jmax": 80,
            "I1": "",
            "I2": "",
            "I3": "",
            "I4": "",
            "I5": "",
        }

    def parse_header_value(self, value, protocol):
        protocol = self.normalize_protocol(protocol)
        raw = str(value).strip()
        if not raw:
            raise ValueError("Header value cannot be empty")

        if self.protocol_supports_header_ranges(protocol) and re.fullmatch(r"\d+\s*-\s*\d+", raw):
            start_raw, end_raw = [part.strip() for part in raw.split("-", 1)]
            start, end = int(start_raw), int(end_raw)
            if start > end:
                raise ValueError(f"Invalid header range '{raw}': start must be <= end")
            if end > self.UINT32_MAX:
                raise ValueError(f"Header value '{raw}' exceeds {self.UINT32_MAX} (a uint32)")
            return {"raw": f"{start}-{end}", "start": start, "end": end}

        if re.fullmatch(r"\d+", raw):
            number = int(raw)
            if number > self.UINT32_MAX:
                raise ValueError(f"Header value '{raw}' exceeds {self.UINT32_MAX} (a uint32)")
            return {"raw": str(number), "start": number, "end": number}

        if self.protocol_supports_header_ranges(protocol):
            raise ValueError(f"Header value '{raw}' must be an integer or range x-y for {protocol}")
        raise ValueError(f"Header value '{raw}' must be a single integer for {protocol}")

    def validate_transport_params(self, protocol, params):
        protocol = self.normalize_protocol(protocol)
        if not isinstance(params, dict):
            raise ValueError("Transport params payload must be an object")

        def as_opt_int(key):
            value = params.get(key)
            if value is None:
                return None
            if isinstance(value, str) and not value.strip():
                return None
            try:
                return int(value)
            except Exception as exc:
                raise ValueError(f"'{key}' must be an integer or empty") from exc

        transport = {
            "S1": as_opt_int("S1"),
            "S2": as_opt_int("S2"),
            "S3": as_opt_int("S3"),
            "S4": as_opt_int("S4"),
            "H1": self.parse_header_value(params.get("H1", ""), protocol)["raw"],
            "H2": self.parse_header_value(params.get("H2", ""), protocol)["raw"],
            "H3": self.parse_header_value(params.get("H3", ""), protocol)["raw"],
            "H4": self.parse_header_value(params.get("H4", ""), protocol)["raw"],
        }

        for key in ("S1", "S2", "S3", "S4"):
            if transport[key] is not None and transport[key] < 0:
                raise ValueError(f"{key} must be non-negative, got {transport[key]}")
            if transport[key] is not None and transport[key] > self.UINT16_MAX:
                raise ValueError(f"{key} must be at most {self.UINT16_MAX}, got {transport[key]}")
        if transport["S1"] is not None and transport["S2"] is not None and transport["S1"] + 56 == transport["S2"]:
            raise ValueError("S1 + 56 must not equal S2")

        if not self.protocol_supports_s34(protocol):
            transport.pop("S3", None)
            transport.pop("S4", None)

        # Every protocol: on AWG 1.5 two equal single values are the same overlap.
        parsed_headers = [self.parse_header_value(transport[key], protocol) for key in ("H1", "H2", "H3", "H4")]
        for index, current in enumerate(parsed_headers):
            for other in parsed_headers[index + 1 :]:
                if current["start"] <= other["end"] and other["start"] <= current["end"]:
                    raise ValueError(f"H1-H4 ranges must not intersect for {protocol}")

        if self.protocol_supports_awg3(protocol):
            header_protection_key = sanitize_config_value(params.get("HeaderProtectionKey") or "")
            if header_protection_key:
                if not self.is_valid_wireguard_key(header_protection_key):
                    raise ValueError("HeaderProtectionKey must be a 32-byte base64 key (generate one with 'awg genkey')")
                # amneziawg-go refuses the config outright if any S value is below
                # the 12-byte header cipher nonce it slices out of the padding.
                for key in ("S1", "S2", "S3", "S4"):
                    value = transport.get(key)
                    if value is None or value < self.HEADER_CIPHER_NONCE_SIZE:
                        raise ValueError(
                            f"{key} must be at least {self.HEADER_CIPHER_NONCE_SIZE} when HeaderProtectionKey is set"
                        )
                transport["HeaderProtectionKey"] = header_protection_key

        if self.protocol_supports_awg31(protocol):
            for key in self.TRANSPORT_AWG31_PARAM_KEYS:
                transport[key] = to_bool(params.get(key), False)

        return transport

    def validate_client_params(self, params):
        if not isinstance(params, dict):
            raise ValueError("Client params payload must be an object")

        merged = self.default_client_defaults()
        merged.update(self.extract_client_params(params))

        try:
            jc = int(merged.get("Jc", 0))
            jmin = int(merged.get("Jmin", 0))
            jmax = int(merged.get("Jmax", 0))
        except Exception as exc:
            raise ValueError("Jc, Jmin and Jmax must be integers") from exc

        # Jc 0 sends no junk; the daemon accepts it.
        if jc < 0:
            raise ValueError(f"Jc must not be negative, got {jc}")
        if jmin <= 0:
            raise ValueError(f"Jmin must be positive, got {jmin}")
        if jmax <= 0:
            raise ValueError(f"Jmax must be positive, got {jmax}")
        for key, value in (("Jc", jc), ("Jmin", jmin), ("Jmax", jmax)):
            if value > self.UINT16_MAX:
                raise ValueError(f"{key} must be at most {self.UINT16_MAX}, got {value}")
        # The daemon draws min + rand(max - min) in uint32: max < min wraps to ~4 GB.
        if jmin > jmax:
            raise ValueError(f"Jmin must be less than or equal to Jmax, got Jmin={jmin}, Jmax={jmax}")

        for key in ("I1", "I2", "I3", "I4", "I5"):
            if merged.get(key):
                self.parse_signature_packet(key, merged[key])

        merged["Jc"] = jc
        merged["Jmin"] = jmin
        merged["Jmax"] = jmax

        # AWG 3.0 range params. Empty means "use the daemon's default", so unset
        # keys are dropped rather than written as 0.
        for key in self.CLIENT_AWG3_PARAM_KEYS:
            normalized = self.parse_uint_range(merged.get(key), key)
            if normalized:
                merged[key] = normalized
            else:
                merged.pop(key, None)

        return merged

    def parse_signature_packet(self, key, value):
        """Check an I1-I5 value the way amneziawg-go builds it (device/obf.go newObfChain).

        Returns (bytes the packet carries, warnings). Raises ValueError for what the
        daemon refuses -- a missing '>', an empty or unknown tag (`<c>` included), a
        bad argument -- and for a negative size, which it accepts and then panics on.
        """
        size, warnings, position, stray = 0, [], 0, False
        while True:
            start = value.find("<", position)
            if value[position : len(value) if start == -1 else start].strip():
                stray = True
            if start == -1:
                break
            end = value.find(">", start)
            if end == -1:
                raise ValueError(f"{key}: a tag is missing its closing '>'")
            position = end + 1
            parts = value[start + 1 : end].split()
            if not parts:
                raise ValueError(f"{key}: empty tag <>")
            tag, arg = parts[0], parts[1] if len(parts) > 1 else ""
            if tag not in self.CPS_TAGS:
                known = " ".join(f"<{t}>" for t in self.CPS_TAGS)
                raise ValueError(f"{key}: unknown tag <{tag}> (amneziawg-go knows {known}; <c> is kernel-module only)")
            if tag == "b":
                digits = arg.removeprefix("0x")
                if not digits or len(digits) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                    raise ValueError(f"{key}: <b> needs an even number of hex digits, e.g. <b 0xc0ffee>")
                size += len(digits) // 2
            elif tag == "t":
                size += 4  # a Unix timestamp
            elif tag in ("r", "rc", "rd", "dz"):
                if not re.fullmatch(r"[+-]?\d+", arg):
                    raise ValueError(f"{key}: <{tag}> needs a byte count, e.g. <{tag} 16>")
                if int(arg) < 0:
                    raise ValueError(f"{key}: <{tag}> must not be negative, got {arg}")
                size += int(arg)
            else:
                # send.go builds I-packets with no payload, so these write nothing.
                warnings.append(f"{key}: <{tag}> adds nothing here: signature packets carry no payload.")
        if stray:
            warnings.append(f"{key}: text outside <...> tags is ignored by the daemon.")
        return size, warnings

    def build_effective_client_params(self, server, client_params=None):
        transport_params = self.extract_transport_params(server.get("transport_params") or {}, server.get("protocol"))
        effective = dict(transport_params)
        effective.update(self.extract_client_params(client_params or {}))
        return effective

    def migrate_config_schema(self, config):
        if not isinstance(config, dict):
            return {"servers": []}

        legacy_client_map = config.pop("clients", None)
        config["servers"] = [s for s in config.get("servers") or [] if isinstance(s, dict)]

        # Until v2.2 every client was stored twice: in its server's list and in a
        # top-level `clients` map. The server lists are now the only store. A client
        # found only in the map joins its server's list; one whose server is gone is
        # dropped. Where both copies exist the embedded one wins -- it is what the
        # .conf renderer has always used.
        if isinstance(legacy_client_map, dict):
            servers_by_id = {s.get("id"): s for s in config["servers"]}
            embedded_ids = {c.get("id") for s in config["servers"] for c in s.get("clients") or [] if isinstance(c, dict)}
            for client_id, client in legacy_client_map.items():
                if not isinstance(client, dict) or client_id in embedded_ids:
                    continue
                server = servers_by_id.get(client.get("server_id"))
                if server is None:
                    logger.warning("Dropping client %s: its server %s no longer exists", client_id, client.get("server_id"))
                    continue
                client.setdefault("id", client_id)
                server.setdefault("clients", []).append(client)

        for server in config["servers"]:
            server["protocol"] = self.normalize_protocol(server.get("protocol"))

            legacy_params = server.get("obfuscation_params") if isinstance(server.get("obfuscation_params"), dict) else {}
            transport_params = server.get("transport_params")
            if not isinstance(transport_params, dict):
                transport_params = self.extract_transport_params(legacy_params, server.get("protocol"))
            else:
                transport_params = self.extract_transport_params(transport_params, server.get("protocol"))
            server["transport_params"] = transport_params

            server.setdefault("client_defaults", self.default_client_defaults())

            # Fields added over time. Until v2.2 GET /api/servers backfilled these on
            # every poll (and persisted them), so 1420 is what existing installs have.
            server.setdefault("mtu", 1420)
            server.setdefault("enable_nat", self.default_enable_nat)
            server.setdefault("block_lan_cidrs", self.default_block_lan_cidrs)
            server.setdefault("egress_probe", None)
            server.setdefault("endpoint_host", "")
            # Display values that GET /api/servers and /info used to write into state;
            # they are computed per response now.
            for derived in ("public_ip_geo", "public_ip_geo_country_code", "current_status"):
                server.pop(derived, None)
            # Until 2.4 a creation-time flag decided auto-start; the last start/stop does now.
            server.pop("auto_start", None)
            if isinstance(server.get("egress_probe"), dict):
                server["egress_probe"].pop("service_name", None)

            server["clients"] = [c for c in server.get("clients") or [] if isinstance(c, dict)]
            for client in server["clients"]:
                client_params = client.get("client_params")
                if not isinstance(client_params, dict):
                    client_params = client.get("obfuscation_params") or legacy_params
                client["client_params"] = self.extract_client_params(client_params)
                # IPv4 only, as issued before 2.4: the device's config still matches.
                client.setdefault("allowed_ips", self.DEFAULT_ALLOWED_IPS)
                client["server_id"] = server.get("id")
                # Derived when serialized: the name and protocol from the server, the
                # status from live telemetry. Until 2.4 the traffic monitor saved
                # status (up to once a minute) and protocol was a stale copy.
                for derived in ("server_name", "status", "protocol"):
                    client.pop(derived, None)

            # Pre-1.6 kept everything in one `obfuscation_params` dict; it has been
            # lifted into transport_params / client_params above and is not kept.
            for record in (server, *server["clients"]):
                record.pop("obfuscation_enabled", None)
                record.pop("obfuscation_params", None)

        return config

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                return self.migrate_config_schema(json.load(f))
        return self.migrate_config_schema({"servers": []})

    def save_config(self):
        """Persist config atomically: a crash mid-write must not lose server keys.

        One writer at a time (request threads and request-driven saves can overlap),
        and each write gets its own temp file, created 0600 so the keys in it are never
        readable by others, then atomically renamed over the config. Until 2.4 it also
        wrote the v2.1 layout (a top-level `clients` map, `server_name` per client) so
        the 2.1 image could be rolled back to; loading such a file still works.
        """
        directory = os.path.dirname(self.config_file) or "."
        os.makedirs(directory, exist_ok=True)
        with self._save_lock:
            payload = json.dumps(self.config, indent=2)
            fd, tmp_path = tempfile.mkstemp(prefix=".web_config-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.config_file)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

    def run_command(self, args, env=None):
        """Run a command from an argv list and return stdout, or None on failure.

        Always argv, never a shell string: values such as subnet, interface and
        port originate from API input, and argv form cannot be turned into extra
        shell commands. `env` replaces the environment for this command only.
        """
        try:
            result = subprocess.run(args, capture_output=True, text=True, check=True, env=env)
            return result.stdout.strip()
        except (subprocess.CalledProcessError, OSError) as e:
            logger.error(f"Command failed ({args[0] if args else '?'}): {e}")
            return None

    def generate_wireguard_keys(self):
        """Generate a real WireGuard keypair.

        Raises rather than inventing a fallback: an unrelated private/public pair
        yields a server that looks configured but no client can ever handshake.
        """
        private_key = self.run_command(["wg", "genkey"])
        if not private_key:
            raise RuntimeError("'wg genkey' failed: cannot generate a server keypair")

        public_key = self.derive_public_key(private_key)
        if not public_key:
            raise RuntimeError("'wg pubkey' failed: cannot derive the public key")

        return {"private_key": private_key, "public_key": public_key}

    def derive_public_key(self, private_key):
        """Derive a public key by piping a private key into 'wg pubkey' via stdin."""
        try:
            result = subprocess.run(
                ["wg", "pubkey"],
                input=private_key,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, OSError) as e:
            logger.error("Public key derivation failed: %s", e)
            return None

    def generate_preshared_key(self):
        """Generate preshared key"""
        key = self.run_command(["wg", "genpsk"])
        if not key:
            raise RuntimeError("'wg genpsk' failed: cannot generate a preshared key")
        return key

    def generate_header_protection_key(self):
        """Generate an AWG 3.0 header protection key (same format as a WG key)."""
        key = self.run_command(["wg", "genkey"])
        if key and self.is_valid_wireguard_key(key):
            return key
        return base64.b64encode(os.urandom(32)).decode("utf-8")

    def generate_transport_params(self, protocol, mtu=1420):
        """Random server-side parameters that pass validation with no warning (services/generator.py)."""
        awg3 = self.protocol_supports_awg3(protocol)
        return generator.transport_params(
            with_s34=self.protocol_supports_s34(protocol),
            header_ranges=self.protocol_supports_header_ranges(protocol),
            awg3=awg3,
            mtu=mtu,
            header_protection_key=self.generate_header_protection_key() if awg3 else None,
        )

    def generate_client_defaults(self, protocol=None):
        """Random client-side defaults: a small junk train and, on AWG 3.x, padding and timers."""
        return generator.client_defaults(awg3=self.protocol_supports_awg3(protocol))

    def create_wireguard_server(self, server_data):
        """Create a new WireGuard server configuration with environment defaults"""
        server_name = self.sanitize_name(server_data.get("name"), "New Server")
        basics, errors = self.check_server_basics(server_data)
        if errors:
            raise ValueError(errors[0])
        port, subnet, mtu, dns_servers = basics["port"], basics["subnet"], basics["mtu"], basics["dns"]
        self.assert_no_conflicts(port, subnet)
        endpoint_host = self.validate_endpoint_host(server_data.get("endpoint_host"))

        protocol = self.normalize_protocol(server_data.get("protocol"))
        # "Start after creating"; what happens at the next boot follows the last start/stop.
        auto_start = to_bool(server_data.get("auto_start"), True)
        enable_nat = to_bool(server_data.get("enable_nat"), self.default_enable_nat)
        block_lan_cidrs = to_bool(server_data.get("block_lan_cidrs"), self.default_block_lan_cidrs)

        # Every client config's Endpoint; a guess here would be baked into all of them.
        public_ip = self.public_ip or self.detect_public_ip()
        if not public_ip:
            raise ValueError("The public IP is unknown (detection failed); try again once the host is online")
        self.public_ip = public_ip

        server_id = str(uuid.uuid4())[:6]
        interface_name = f"wg-{server_id}"
        config_path = os.path.join(self.wireguard_config_dir, f"{interface_name}.conf")

        # Generate server keys
        server_keys = self.generate_wireguard_keys()

        raw_transport_params = server_data.get("transport_params")
        if not isinstance(raw_transport_params, dict):
            raw_transport_params = self.generate_transport_params(protocol, mtu)
        transport_params = self.validate_transport_params(protocol, raw_transport_params)

        raw_client_defaults = server_data.get("client_defaults")
        if not isinstance(raw_client_defaults, dict):
            raw_client_defaults = self.generate_client_defaults(protocol)
        client_defaults = self.validate_client_params(raw_client_defaults)

        server_ip = self.get_server_ip(subnet)

        server_config = {
            "id": server_id,
            "name": server_name,
            "protocol": protocol,
            "port": port,
            "status": "stopped",
            "interface": interface_name,
            "config_path": config_path,
            "server_public_key": server_keys["public_key"],
            "server_private_key": server_keys["private_key"],
            "subnet": subnet,
            "server_ip": server_ip,
            "mtu": mtu,
            "public_ip": self.public_ip,
            "endpoint_host": endpoint_host,
            "transport_params": transport_params,
            "client_defaults": client_defaults,
            "enable_nat": enable_nat,
            "block_lan_cidrs": block_lan_cidrs,
            "egress_probe": None,
            "dns": dns_servers,  # Store DNS servers
            "clients": [],
            "created_at": time.time(),
        }

        # Save WireGuard config file. Uses the same writer as every later rewrite
        # so the created file and subsequent updates cannot drift apart.
        self.write_server_conf(server_config)

        self.config["servers"].append(server_config)
        self.save_config()

        # Auto-start if enabled (from environment or request)
        if auto_start:
            logger.info("Auto-starting new server: %s", server_name)
            self.start_server(server_id)

        return server_config

    def write_server_conf(self, server):
        """Render and write a server's .conf from current state, mode 0600.

        Single writer for the interface config: it embeds PrivateKey (and, on AWG
        3.0, HeaderProtectionKey), so it must not be world-readable — awg-quick
        warns about that on every start.
        """
        content = self._build_server_config_content(server)
        path = server["config_path"]
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, 0o600)
        return content

    def _build_server_config_content(self, server):
        """Build full server config content (Interface + all Peer blocks)."""
        subnet = server.get("subnet", self.default_subnet)
        subnet_parts = str(subnet).split("/")
        prefix = subnet_parts[1] if len(subnet_parts) > 1 else "24"

        server_ip = server.get("server_ip") or self.get_server_ip(subnet)
        mtu = int(server.get("mtu", self.default_mtu))
        port = int(server.get("port", self.default_port))

        content = f"""[Interface]
PrivateKey = {server["server_private_key"]}
Address = {server_ip}/{prefix}
ListenPort = {port}
SaveConfig = false
MTU = {mtu}
"""

        p = self.extract_transport_params(server.get("transport_params") or {}, server.get("protocol"))
        if p:

            def _opt_line(key):
                return self._config_line(p, key)

            content += f"""{_opt_line("S1")}{_opt_line("S2")}{_opt_line("S3")}{_opt_line("S4")}H1 = {p.get("H1", 0)}
H2 = {p.get("H2", 0)}
H3 = {p.get("H3", 0)}
H4 = {p.get("H4", 0)}
{_opt_line("HeaderProtectionKey")}{_opt_line("RandomTrailers")}{_opt_line("DisableCookies")}"""

        for client in server.get("clients") or []:
            if client.get("suspended"):
                continue
            try:
                content += f"""

# Client: {client.get("name", client.get("id", "client"))}
[Peer]
PublicKey = {client["client_public_key"]}
PresharedKey = {client["preshared_key"]}
AllowedIPs = {client["client_ip"]}/32
"""
            except Exception as e:
                logger.error("Failed to render client peer block: %s", e)
        return content

    def update_server_transport_params(self, server_id, params):
        """Update server protocol and transport params, rewrite config, and restart if running."""
        server = self.get_server(server_id)
        if not server:
            return None

        if not isinstance(params, dict):
            raise ValueError("Invalid payload")

        next_protocol = self.normalize_protocol(params.get("protocol", server.get("protocol")))
        next_transport_params = self.validate_transport_params(next_protocol, params)

        server["protocol"] = next_protocol
        server["transport_params"] = dict(next_transport_params)

        # Re-extract so client params the new protocol does not support are dropped.
        for client in server.get("clients") or []:
            client["client_params"] = self.extract_client_params(client.get("client_params") or {})

        # Rewrite server config file
        self.write_server_conf(server)

        self.save_config()

        # Restart if running
        was_running = self.get_server_status(server_id) == "running"
        restarted = False
        if was_running and self.stop_server(server_id):
            restarted = bool(self.start_server(server_id))

        return {
            "status": "updated",
            "server_id": server_id,
            "was_running": was_running,
            "restarted": restarted,
        }

    def apply_live_config(self, interface):
        """Apply the latest config to a running interface using 'awg syncconf'.

        Upstream documents this as `awg syncconf <if> <(awg-quick strip <if>)`. The
        process substitution is replaced with a temp file so no shell is involved.
        """
        stripped = self.run_command(["awg-quick", "strip", interface])
        if stripped is None:
            logger.error("Failed to strip config for %s", interface)
            return False

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=f"{interface}-", suffix=".conf")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(stripped + "\n")

            if self.run_command(["awg", "syncconf", interface, tmp_path]) is None:
                logger.error("Failed to apply live config to %s", interface)
                return False

            logger.info("Live config applied to %s", interface)
            return True
        except OSError as e:
            logger.error("Error applying live config to %s: %s", interface, e)
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def get_server_ip(subnet):
        """The subnet's first host address: 10.8.0.65 for 10.8.0.64/26 (not a.b.c.1)."""
        return str(next(ipaddress.ip_network(str(subnet), strict=False).hosts()))

    def get_client_ip(self, server):
        """Return the first free client address in the server's subnet.

        Derived from the subnet rather than assuming a /24. Raises when the subnet is
        full — silently reusing an address would break the existing client that holds it.
        """
        subnet = server.get("subnet") or self.default_subnet
        server_ip = server.get("server_ip")

        try:
            network = ipaddress.ip_network(str(subnet), strict=False)
        except ValueError as exc:
            raise ValueError(f"Server {server.get('id')} has an invalid subnet '{subnet}'") from exc

        used = {server_ip}
        used.update(c.get("client_ip") for c in server.get("clients") or [])

        for candidate in network.hosts():
            text = str(candidate)
            if text not in used:
                return text

        raise ValueError(f"No free addresses left in {network} for server {server.get('id')}")

    def get_server(self, server_id):
        return next((s for s in self.config.get("servers", []) if s.get("id") == server_id), None)

    @staticmethod
    def validate_mtu(value):
        try:
            mtu = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"MTU must be an integer, got {value!r}") from exc
        if not 1280 <= mtu <= 1440:
            raise ValueError(f"MTU must be between 1280 and 1440, got {mtu}")
        return mtu

    @staticmethod
    def validate_port(value):
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Port must be an integer, got {value!r}") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {port}")
        return port

    def parse_dns_servers(self, value):
        """A comma-separated string or a list; empty means the environment default."""
        if isinstance(value, str):
            servers = [dns.strip() for dns in value.split(",") if dns.strip()]
        elif isinstance(value, list):
            servers = value
        else:
            servers = []
        servers = servers or self.dns_servers
        for dns in servers:
            if not is_valid_ip(dns):
                raise ValueError(f"Invalid DNS server IP: {dns}")
        return servers

    def check_server_basics(self, server_data):
        """MTU, port, subnet and DNS of a new server, each checked on its own.

        Returns (normalized values, one message per invalid field). The port and
        subnet reach config files and the iptables scripts straight from the API.
        """
        checks = (
            ("mtu", self.validate_mtu, self.default_mtu),
            ("port", self.validate_port, self.default_port),
            ("subnet", self.validate_subnet, self.default_subnet),
            ("dns", self.parse_dns_servers, None),
        )
        values, errors = {}, []
        for key, check, default in checks:
            try:
                values[key] = check(server_data.get(key, default))
            except ValueError as exc:
                errors.append(str(exc))
        return values, errors

    @staticmethod
    def validate_allowed_ips(value):
        """A client's AllowedIPs: IPv4/IPv6 CIDRs, comma-separated, as 'a, b'.

        A narrower list than 0.0.0.0/0, ::/0 is split tunnelling.
        """
        raw = value if isinstance(value, list) else sanitize_config_value(value if value is not None else "").split(",")
        networks = []
        for part in (str(p).strip() for p in raw):
            if not part:
                continue
            try:
                network = str(ipaddress.ip_network(part, strict=False))
            except ValueError as exc:
                raise ValueError(f"Invalid AllowedIPs entry '{part}': expected a CIDR such as 0.0.0.0/0 or ::/0") from exc
            if network not in networks:
                networks.append(network)
        if not networks:
            raise ValueError("AllowedIPs needs at least one network, e.g. 0.0.0.0/0, ::/0")
        return ", ".join(networks)

    @staticmethod
    def validate_endpoint_host(value):
        """The host clients dial: an IPv4 address or a DNS name, or '' for the detected IP.

        With a name (e.g. a dynamic DNS one that follows the WAN address), a new public
        IP changes no client config, so nothing needs re-importing.
        """
        host = sanitize_config_value(value if value is not None else "").strip().rstrip(".").lower()
        if not host or is_valid_ip(host):
            return host
        labels = host.split(".")
        label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
        if len(host) > 253 or len(labels) < 2 or not all(label.fullmatch(x) for x in labels) or labels[-1].isdigit():
            raise ValueError(f"Endpoint host '{host}' must be an IPv4 address or a DNS name such as vpn.example.com")
        return host

    @staticmethod
    def endpoint_of(server):
        """What client configs put in Endpoint's host: endpoint_host, else the detected IP."""
        return server.get("endpoint_host") or server.get("public_ip")

    def transport_param_warnings(self, protocol, transport, mtu):
        """Practical guidance for validated transport params; none of it is a protocol limit."""
        warnings = []

        def outside_common_range(key):
            value = transport.get(key)
            if value is not None and not 15 <= value <= 150:
                warnings.append(f"{key} ({value}) is outside the common 15-150 range.")

        s1, s2, s4 = transport.get("S1"), transport.get("S2"), transport.get("S4")
        outside_common_range("S1")
        if s1 is not None and s1 > mtu - 148:
            warnings.append(f"S1 ({s1}) is above the rule-of-thumb bound MTU - 148 ({mtu - 148}).")
        outside_common_range("S2")
        if s2 is not None and s2 > mtu - 92:
            warnings.append(f"S2 ({s2}) is above the rule-of-thumb bound MTU - 92 ({mtu - 92}).")
        if self.protocol_supports_s34(protocol):
            outside_common_range("S3")
            s3 = transport.get("S3")
            # Equal padded lengths never break the tunnel (the H ranges still tell the
            # types apart), but two message types of one length are a fingerprint.
            if s3 is not None and s1 is not None and s3 == s1 + 84:
                warnings.append(f"S3 ({s3}) = S1 + 84: cookie replies come out as long as handshake initiations.")
            if s3 is not None and s2 is not None and s3 == s2 + 28:
                warnings.append(f"S3 ({s3}) = S2 + 28: cookie replies come out as long as handshake responses.")
            if s4 is not None and s4 > 32:
                warnings.append(f"S4 ({s4}) is above a conservative 0-32 and may cause 'message too long' errors.")

        # Header protection is opt-in, so AWG 3.x without a key is valid and behaves like
        # 2.0; say so, and name the S values that would block turning it on.
        if self.protocol_supports_awg3(protocol) and not transport.get("HeaderProtectionKey"):
            low = [f"{k} ({transport[k]})" for k in ("S1", "S2", "S3", "S4")
                   if transport.get(k) is not None and transport[k] < self.HEADER_CIPHER_NONCE_SIZE]  # fmt: skip
            warnings.append(
                f"{protocol} without a header protection key: header protection is off and this works like AWG 2.0. "
                "Generate a key to turn it on"
                + (
                    f"; it needs S1-S4 each 12 or more, and {', '.join(low)} {'is' if len(low) == 1 else 'are'} below."
                    if low
                    else "."
                )
            )

        # 1-4 are WireGuard's own message types. With header protection the docs
        # recommend exactly that (the cipher hides the type); without, it is plain WireGuard.
        if not transport.get("HeaderProtectionKey"):
            standard = [key for key in ("H1", "H2", "H3", "H4") if int(str(transport.get(key, "0")).split("-")[0]) <= 4]
            if standard:
                warnings.append(f"{', '.join(standard)} in 1-4: WireGuard's own message types, which DPI recognises.")
        return warnings

    def client_param_warnings(self, client_params, mtu):
        """Practical guidance for validated client params."""
        warnings = []
        jc, jmax = client_params["Jc"], client_params["Jmax"]
        if jc == 0:
            warnings.append("Jc is 0: no junk packets are sent before the handshake.")
        elif not 4 <= jc <= 12:
            warnings.append(f"Jc ({jc}) is outside the recommended range 4-12.")
        if jmax >= mtu:
            warnings.append(f"Jmax ({jmax}) is at or above MTU ({mtu}) and may fragment junk packets.")

        for key in ("I1", "I2", "I3", "I4", "I5"):
            if not client_params.get(key):
                continue
            size, packet_warnings = self.parse_signature_packet(key, client_params[key])
            warnings += packet_warnings
            if size > mtu:
                warnings.append(f"{key} is {size} bytes, above MTU ({mtu}): it may be fragmented.")

        warnings += self.timing_warnings(client_params)
        return warnings

    def timing_warnings(self, client_params):
        """The two AWG 3.x timer rules (timers.go), checked when any of them is set.

        keyRefreshTimeoutReceiving = RejectAfterTime - KeepaliveTimeout.lo -
        RekeyTimeout.lo, clamped at 0, and at 0 every received packet counts as due
        for a rekey (receive.go). And a session rekeys before it is rejected only if
        RekeyAfterTime ends before RejectAfterTime starts.
        """
        involved = ("RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout")
        if not any(client_params.get(key) for key in involved):
            return []

        def bounds(key):
            raw = client_params.get(key) or str(self.WG_DEFAULT_TIMINGS[key])
            low, _, high = raw.partition("-")
            return int(low), int(high or low)

        reject_lo = bounds("RejectAfterTime")[0]
        keepalive_lo, rekey_timeout_lo = bounds("KeepaliveTimeout")[0], bounds("RekeyTimeout")[0]
        rekey_after_hi = bounds("RekeyAfterTime")[1]
        warnings = []
        if reject_lo <= keepalive_lo + rekey_timeout_lo:
            warnings.append(
                f"RejectAfterTime starts at {reject_lo}, not above KeepaliveTimeout + RekeyTimeout "
                f"({keepalive_lo} + {rekey_timeout_lo}): every received packet would trigger a rekey."
            )
        if rekey_after_hi >= reject_lo:
            warnings.append(
                f"RekeyAfterTime reaches {rekey_after_hi}, not below RejectAfterTime's start ({reject_lo}): "
                "the session can be rejected before it rekeys."
            )
        return warnings

    def preview_transport_change(self, server, protocol, transport, endpoint_host=None):
        """What saving new transport params (and endpoint host) would do to the server's client configs.

        configs_changed: clients whose config would differ from today's.
        outdated_now / outdated_after: issued clients whose device no longer matches,
        before and after the change (after < now means the change reverts something).
        """
        candidate = {**server, "protocol": protocol, "transport_params": transport}
        if endpoint_host is not None:
            candidate["endpoint_host"] = endpoint_host
        counts = {"configs_changed": 0, "outdated_now": 0, "outdated_after": 0}
        for client in server.get("clients") or []:
            now = self.config_fingerprint(server, client)
            after = self.config_fingerprint(candidate, client)
            counts["configs_changed"] += now != after
            issued = client.get("config_issued_fingerprint")
            if issued:
                counts["outdated_now"] += issued != now
                counts["outdated_after"] += issued != after
        return counts

    def assert_no_conflicts(self, port, subnet, ignore_server_id=None):
        """Reject a port already in use, or a subnet overlapping an existing server.

        The browser warns about this too, but only the backend can enforce it: two
        servers on one port means the second interface fails to bind, and overlapping
        subnets route unpredictably.
        """
        network = ipaddress.ip_network(str(subnet), strict=False)

        for server in self.config.get("servers", []):
            if not isinstance(server, dict) or server.get("id") == ignore_server_id:
                continue

            name = server.get("name", server.get("id"))
            if int(server.get("port", 0) or 0) == int(port):
                raise ValueError(f"Port {port} is already used by server '{name}'")

            try:
                existing = ipaddress.ip_network(str(server.get("subnet")), strict=False)
            except ValueError:
                continue  # a pre-existing bad subnet should not block new servers
            if network.overlaps(existing):
                raise ValueError(f"Subnet {network} overlaps '{name}' ({existing})")

    def reapply_iptables_for_server(self, server):
        """Reapply iptables rules for a running server after networking changes."""
        if not server:
            return False
        self.cleanup_iptables(
            server["interface"],
            server["subnet"],
            enable_nat=server.get("enable_nat"),
            block_lan_cidrs=server.get("block_lan_cidrs"),
        )
        return self.setup_iptables(
            server["interface"],
            server["subnet"],
            enable_nat=server.get("enable_nat"),
            block_lan_cidrs=server.get("block_lan_cidrs"),
        )

    def get_client(self, client_id):
        """The client with this id, looked up through the servers' client lists."""
        return next((c for c in self.get_client_configs() if c.get("id") == client_id), None)

    def delete_server(self, server_id):
        """Delete a server and all its clients"""
        server = self.get_server(server_id)
        if not server:
            return False

        # Stop based on the live interface state: a stale cached "stopped" would
        # orphan the interface and its iptables rules after the config is gone.
        if self.get_server_status(server_id) == "running":
            self.stop_server(server_id)

        # Remove config file
        if os.path.exists(server["config_path"]):
            os.remove(server["config_path"])

        # Remove the server (its clients live in its list and go with it)
        self.config["servers"] = [s for s in self.config["servers"] if s["id"] != server_id]
        self.save_config()
        return True

    def add_wireguard_client(self, server_id, client_name, client_params=None, copy_from_client_id=None, allowed_ips=None):
        """Add a client to a WireGuard server"""
        server = self.get_server(server_id)
        if not server:
            return None

        client_name = self.sanitize_name(client_name, "New Client")

        client_id = str(uuid.uuid4())[:6]

        # Generate client keys
        client_keys = self.generate_wireguard_keys()
        preshared_key = self.generate_preshared_key()

        # Assign client IP
        client_ip = self.get_client_ip(server)

        base_client_params = self.default_client_defaults()
        base_client_params.update(self.extract_client_params(server.get("client_defaults") or {}))

        if copy_from_client_id:
            source_client = next(
                (client for client in (server.get("clients") or []) if client.get("id") == copy_from_client_id),
                None,
            )
            if source_client:
                source_params = self.extract_client_params(source_client.get("client_params") or {})
                base_client_params.update(source_params)

        if isinstance(client_params, dict):
            base_client_params.update(self.extract_client_params(client_params))

        base_client_params = self.validate_client_params(base_client_params)

        client_config = {
            "id": client_id,
            "name": client_name,
            "server_id": server_id,
            "created_at": time.time(),
            "client_private_key": client_keys["private_key"],
            "client_public_key": client_keys["public_key"],
            "preshared_key": preshared_key,
            "client_ip": client_ip,
            "allowed_ips": self.validate_allowed_ips(allowed_ips or self.DEFAULT_ALLOWED_IPS),
            "suspended": False,
            "client_params": dict(base_client_params),
            # Not handed out yet; set by mark_config_issued. None, not absent, so
            # backfill_config_fingerprints leaves it alone.
            "config_issued_fingerprint": None,
            "config_issued_at": None,
        }

        server["clients"].append(client_config)

        # Rewrite the whole file from state rather than appending a peer block, so
        # the config always matches self.config exactly.
        self.write_server_conf(server)
        self.save_config()
        # Check the live interface, not the cached status field: it is only
        # refreshed by GET /api/servers, so a stale "stopped" would skip the
        # hot-reload and leave the new peer absent from the running daemon.
        if self.get_server_status(server_id) == "running":
            self.apply_live_config(server["interface"])
        logger.info(f"Client {client_config['name']} added")
        config_content = self.generate_wireguard_client_config(
            server,
            client_config,
            include_comments=True,
        )
        return client_config, config_content

    def update_client_params(self, server_id, client_id, params, allowed_ips=None):
        """Update a client's J/I parameters and, when given, its AllowedIPs."""
        server = self.get_server(server_id)
        if not server:
            return None

        client = self.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            return None

        raw_client_params = self.default_client_defaults()
        raw_client_params.update(self.extract_client_params(client.get("client_params") or {}))
        if isinstance(params, dict):
            raw_client_params.update(self.extract_client_params(params))

        validated = self.validate_client_params(raw_client_params)
        if allowed_ips is not None:
            client["allowed_ips"] = self.validate_allowed_ips(allowed_ips)
        client["client_params"] = validated
        self.save_config()
        return client

    def update_endpoint_host(self, server_id, endpoint_host):
        """Set the host client configs dial; no restart (the server's .conf has no Endpoint)."""
        server = self.get_server(server_id)
        if not server:
            return None
        server["endpoint_host"] = self.validate_endpoint_host(endpoint_host)
        self.save_config()
        return server

    def rename_server(self, server_id, new_name):
        """Rename a server (display name only)."""
        server = self.get_server(server_id)
        if not server:
            return None
        server["name"] = self.sanitize_name(new_name)
        self.save_config()
        return server

    def rename_client(self, server_id, client_id, new_name):
        """Rename a client and rewrite server .conf to keep comment markers in sync."""
        server = self.get_server(server_id)
        if not server:
            return None
        client = self.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            return None
        client["name"] = self.sanitize_name(new_name)
        self.write_server_conf(server)
        self.save_config()
        return client

    def toggle_client_suspend(self, server_id, client_id):
        """Toggle the suspended state of a client."""
        server = self.get_server(server_id)
        if not server:
            return None

        client = self.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            return None

        client["suspended"] = not client.get("suspended", False)
        self.write_server_conf(server)

        self.save_config()

        if self.get_server_status(server_id) == "running":
            self.apply_live_config(server["interface"])

        return client

    def delete_client(self, server_id, client_id):
        """Delete a client from a server and update the config file"""
        server = self.get_server(server_id)
        if not server:
            return False

        client = next((c for c in server["clients"] if c["id"] == client_id), None)
        if not client:
            return False

        server["clients"] = [c for c in server["clients"] if c["id"] != client_id]

        # Rebuild the config from state. Previously this stripped the peer block by
        # matching a "# Client: <name>" comment, which deleted both blocks when two
        # clients shared a name.
        self.write_server_conf(server)

        self.save_config()

        # Apply live config if server is running
        if self.get_server_status(server_id) == "running":
            self.apply_live_config(server["interface"])
        logger.info(f"Client {server['name']}:{client['name']} removed")
        return True

    def generate_wireguard_client_config(self, server, client_config, include_comments=True):
        """Generate WireGuard client configuration"""
        config = ""

        # Add comments only if requested
        if include_comments:
            config = f"""# AmneziaWG Client Configuration
# Server: {server["name"]}
# Client: {client_config["name"]}
# Generated: {time.ctime()}
# Server IP: {self.endpoint_of(server)}:{server["port"]}

"""

        config += f"""[Interface]
PrivateKey = {client_config["client_private_key"]}
Address = {client_config["client_ip"]}/32
DNS = {", ".join(server["dns"])}
MTU = {server["mtu"]}
"""

        params = self.build_effective_client_params(
            server,
            client_config.get("client_params") or {},
        )
        if params:

            def _opt_line(key):
                return self._config_line(params, key)

            i_lines = []
            for key in ("I1", "I2", "I3", "I4", "I5"):
                value = sanitize_config_value(params.get(key, ""))
                if value:
                    i_lines.append(f"{key} = {value}")

            config += f"""Jc = {params.get("Jc", 0)}
Jmin = {params.get("Jmin", 0)}
Jmax = {params.get("Jmax", 0)}
{_opt_line("S1")}{_opt_line("S2")}{_opt_line("S3")}{_opt_line("S4")}H1 = {params.get("H1", 0)}
H2 = {params.get("H2", 0)}
H3 = {params.get("H3", 0)}
H4 = {params.get("H4", 0)}
"""

            if i_lines:
                config += "\n".join(i_lines) + "\n"

            # AWG 3.x. HeaderProtectionKey is server-side, so it has to match the
            # server config exactly; the rest are client-side and optional.
            if self.protocol_supports_awg3(server.get("protocol")):
                config += _opt_line("HeaderProtectionKey")
                config += _opt_line("ContentPaddingAddition")
                for key in self.CLIENT_TIMING_PARAM_KEYS:
                    config += _opt_line(key)
            if self.protocol_supports_awg31(server.get("protocol")):
                for key in self.TRANSPORT_AWG31_PARAM_KEYS:
                    config += _opt_line(key)

        config += f"""
[Peer]
PublicKey = {server["server_public_key"]}
PresharedKey = {client_config["preshared_key"]}
Endpoint = {self.endpoint_of(server)}:{server["port"]}
AllowedIPs = {client_config.get("allowed_ips") or self.DEFAULT_ALLOWED_IPS}
PersistentKeepalive = 25
"""
        return config

    def config_fingerprint(self, server, client):
        """Hash of the config a device imports (the QR text: no timestamp, no name)."""
        config = self.generate_wireguard_client_config(server, client, include_comments=False)
        return hashlib.sha256(config.encode("utf-8")).hexdigest()[:16]

    def is_config_outdated(self, server, client):
        """True when the config issued now would differ from the one the device got."""
        issued = client.get("config_issued_fingerprint")
        return bool(issued) and issued != self.config_fingerprint(server, client)

    def mark_config_issued(self, server, client):
        client["config_issued_fingerprint"] = self.config_fingerprint(server, client)
        client["config_issued_at"] = time.time()
        self.save_config()

    def backfill_config_fingerprints(self):
        """Give clients from before config tracking their current fingerprint.

        Devices are assumed up to date at upgrade. Returns how many were filled.
        """
        filled = 0
        for server in self.config["servers"]:
            for client in server["clients"]:
                if "config_issued_fingerprint" in client:
                    continue
                try:
                    client["config_issued_fingerprint"] = self.config_fingerprint(server, client)
                except (KeyError, TypeError):
                    logger.warning("Cannot fingerprint client %s: incomplete record", client.get("id"))
                    continue
                client["config_issued_at"] = None
                filled += 1
        return filled

    def _run_iptables_script(self, action, interface, subnet, enable_nat, block_lan_cidrs):
        """Run the setup/cleanup iptables script for an interface.

        interface and subnet originate from API input, so the script is invoked as
        an argv list (no shell) and the toggles are passed through the environment.
        """
        script_path = f"/app/scripts/{action}_iptables.sh"
        if not os.path.exists(script_path):
            logger.warning("iptables %s script not found at %s", action, script_path)
            return False

        env = os.environ.copy()
        if enable_nat is not None:
            env["ENABLE_NAT"] = "1" if enable_nat else "0"
        if block_lan_cidrs is not None:
            env["BLOCK_LAN_CIDRS"] = "1" if block_lan_cidrs else "0"

        try:
            subprocess.run(
                [script_path, str(interface), str(subnet)],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
        except (subprocess.CalledProcessError, OSError) as e:
            logger.error("iptables %s failed for %s: %s", action, interface, e)
            return False

        logger.info("iptables %s completed for %s", action, interface)
        return True

    def setup_iptables(self, interface, subnet, enable_nat=None, block_lan_cidrs=None):
        """Setup iptables rules for WireGuard interface"""
        return self._run_iptables_script("setup", interface, subnet, enable_nat, block_lan_cidrs)

    def cleanup_iptables(self, interface, subnet, enable_nat=None, block_lan_cidrs=None):
        """Cleanup iptables rules for WireGuard interface"""
        return self._run_iptables_script("cleanup", interface, subnet, enable_nat, block_lan_cidrs)

    def daemon_env(self):
        """The environment awg-quick starts amneziawg-go with: the daemon's log level.

        amneziawg-go reads LOG_LEVEL once at process start (main.go) and has no UAPI
        key for it, so a new level takes an interface restart. The panel's own
        LOG_LEVEL is never passed on: with any LOG_LEVEL set the daemon keeps its
        stdout, which here is run_command's pipe, and `awg-quick up` never returns
        (verified: a panel started with LOG_LEVEL=DEBUG hung on the first server).
        Unset, the daemon's output goes to /dev/null.
        """
        env = {k: v for k, v in os.environ.items() if k not in ("LOG_LEVEL", "WG_QUICK_USERSPACE_IMPLEMENTATION")}
        if self.awg_log_level != "off":
            env.update(LOG_LEVEL=self.awg_log_level, WG_QUICK_USERSPACE_IMPLEMENTATION=self.LOGGED_DAEMON)
        return env

    def start_server(self, server_id):
        """Start a WireGuard server using awg-quick with iptables setup"""
        server = self.get_server(server_id)
        if not server:
            return False

        try:
            # Use awg-quick to bring up the interface
            result = self.run_command(["/usr/bin/awg-quick", "up", server["interface"]], env=self.daemon_env())
            if result is not None:
                # Setup iptables rules
                iptables_success = self.setup_iptables(
                    server["interface"],
                    server["subnet"],
                    enable_nat=server.get("enable_nat"),
                    block_lan_cidrs=server.get("block_lan_cidrs"),
                )

                server["status"] = "running"
                self.save_config()

                logger.info(f"Server {server['name']} started successfully")
                if iptables_success:
                    logger.info(f"iptables rules configured for {server['interface']}")
                else:
                    logger.error(f"Warning: iptables setup may have failed for {server['interface']}")
                self.emit_status_after_delay(server_id, "running")
                return True
            else:
                logger.error(f"Failed to start server {server['name']}")
        except Exception as e:
            logger.error("Failed to start server %s: %s", server_id, e)
        return False

    def stop_server(self, server_id):
        """Stop a WireGuard server using awg-quick with iptables cleanup"""
        server = self.get_server(server_id)
        if not server:
            return False

        try:
            # Cleanup iptables rules first
            iptables_cleaned = self.cleanup_iptables(
                server["interface"],
                server["subnet"],
                enable_nat=server.get("enable_nat"),
                block_lan_cidrs=server.get("block_lan_cidrs"),
            )

            # Use awg-quick to bring down the interface
            result = self.run_command(["/usr/bin/awg-quick", "down", server["interface"]])
            if result is not None:
                server["status"] = "stopped"
                self.save_config()

                logger.info(f"Server {server['name']} stopped successfully")
                if iptables_cleaned:
                    logger.info(f"iptables rules cleaned up for {server['interface']}")
                self.emit_status_after_delay(server_id, "stopped")
                return True
            else:
                logger.error(f"Failed to stop server {server['name']}")
        except Exception as e:
            logger.error("Failed to stop server %s: %s", server_id, e)
        return False

    @staticmethod
    def interface_state(interface):
        """The kernel's operstate for an interface, or None when it does not exist."""
        try:
            with open(f"/sys/class/net/{interface}/operstate", encoding="ascii") as f:
                return f.read().strip()
        except OSError:
            return None

    def get_server_status(self, server_id):
        """'running' when the server's interface is up, from sysfs: no subprocess.

        amneziawg-go's tun reports operstate "unknown" (it has no carrier) while it
        runs; a stopped server has no interface at all.
        """
        server = self.get_server(server_id)
        if not server:
            return "not_found"
        return "running" if self.interface_state(server["interface"]) in ("up", "unknown") else "stopped"

    def emit_status_after_delay(self, server_id, status, delay_seconds=2):
        """Push a server_status update to clients once the interface has settled.

        Runs as a Socket.IO background task, which follows whatever async mode the
        server is in (a plain thread under async_mode="threading").
        """

        def emit_later():
            self.socketio.sleep(delay_seconds)
            self.read_telemetry()  # so the reload it prompts sees the interface as it is now
            self.socketio.emit("server_status", {"server_id": server_id, "status": status})

        self.socketio.start_background_task(emit_later)

    def start_traffic_monitoring(self):
        """Read telemetry every 7 s and push each running server's to the browsers."""

        # A Socket.IO background task, so it follows the server's async mode.
        def monitor_traffic():
            while True:
                try:
                    self.read_telemetry()
                    for server in self.config["servers"]:
                        traffic = self.get_traffic_for_server(server["id"])
                        if traffic is not None:
                            self.socketio.emit("traffic_update", {"server_id": server["id"], "traffic": traffic})
                    self.socketio.sleep(7)
                except Exception as e:
                    logger.error("Error in traffic monitoring: %s", e)
                    self.socketio.sleep(7)

        self.socketio.start_background_task(monitor_traffic)

    def get_client_configs(self, server_id=None):
        """All clients, or only those of `server_id` (none for an unknown server)."""
        servers = [s for s in self.config["servers"] if not server_id or s.get("id") == server_id]
        return [client for server in servers for client in server.get("clients", [])]

    @staticmethod
    def parse_dump(output):
        """`awg show all dump` as {interface: {peer public key: {...}}}.

        One tab-separated line per interface, then one per peer (amneziawg-tools
        show.c dump_print). A peer line is: interface, public key, preshared key,
        endpoint ("(none)" before the first packet), allowed ips, latest handshake
        (Unix time, 0 = never), rx bytes, tx bytes, keepalive. The interface's own
        line comes first and has ~30 columns (keys, S/H, I1-I5, ...); it is skipped.
        """
        interfaces, current = {}, None
        for line in output.splitlines():
            fields = line.split("\t")
            name = fields[0]
            if not name:
                continue
            if name != current:
                current = name
                interfaces[name] = {}
                continue
            if len(fields) != 9:
                logger.debug("Skipping an unexpected dump line for %s (%d columns)", name, len(fields))
                continue
            _, public_key, _psk, endpoint, _allowed, handshake, rx, tx, _keepalive = fields
            try:
                interfaces[name][public_key] = {
                    "endpoint": None if endpoint == "(none)" else endpoint,
                    "handshake_at": int(handshake) or None,
                    "rx": int(rx),
                    "tx": int(tx),
                }
            except ValueError:
                logger.debug("Skipping a malformed dump line for %s", name)
        return interfaces

    def read_telemetry(self):
        """One `awg show all dump` for every interface, kept as the last snapshot.

        It replaces an `ip link show` plus an `awg show` per running server, and the
        parsing of "1.39 MiB received" and "1 minute, 2 seconds ago". Only running
        interfaces appear in it.
        """
        output = self.run_command(["/usr/bin/awg", "show", "all", "dump"])
        self._telemetry = {"at": time.time(), "interfaces": self.parse_dump(output or "")}
        return self._telemetry

    @staticmethod
    def endpoint_ip(endpoint):
        """The address of 'ip:port' or '[ipv6]:port', or None."""
        match = re.fullmatch(r"\[([^\]]+)\]:\d+|([^:]+):\d+", endpoint or "")
        return (match.group(1) or match.group(2)) if match else None

    def _peer_telemetry(self, server, client):
        """(the client's dump entry or {}, seconds since its last handshake or None)."""
        peers = self._telemetry["interfaces"].get(server.get("interface")) or {}
        info = peers.get(client.get("client_public_key")) or {}
        handshake_at = info.get("handshake_at")
        seconds = max(0, int(self._telemetry["at"] - handshake_at)) if handshake_at else None
        return info, seconds

    def get_traffic_for_server(self, server_id):
        """Per-client traffic of a running server from the last snapshot, or None when
        the server is unknown or its interface is not running."""
        server = self.get_server(server_id)
        if not server or server["interface"] not in self._telemetry["interfaces"]:
            return None

        traffic = {}
        for client in server.get("clients", []):
            info, seconds = self._peer_telemetry(server, client)
            geo_label, geo_country_code = self.lookup_geoip_cached(self.endpoint_ip(info.get("endpoint")))
            traffic[client.get("id")] = {
                "received_bytes": info.get("rx", 0),
                "sent_bytes": info.get("tx", 0),
                "endpoint": info.get("endpoint"),
                "geo": geo_label,
                "geo_country_code": geo_country_code,
                "latest_handshake_at": info.get("handshake_at"),
                "latest_handshake_seconds": seconds,
                "active": seconds is not None and seconds <= self.ACTIVE_WITHIN_SECONDS,
            }
        return traffic

    def client_status(self, client):
        """'active' after a handshake in the last 5 minutes (last snapshot), else 'inactive'."""
        server = self.get_server(client.get("server_id")) or {}
        _, seconds = self._peer_telemetry(server, client)
        return "active" if seconds is not None and seconds <= self.ACTIVE_WITHIN_SECONDS else "inactive"
