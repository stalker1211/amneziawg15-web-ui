"""Core service logic for managing AmneziaWG servers and clients."""

import base64
import ipaddress
import json
import os
import random
import re
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlparse

import requests
from core.helpers import is_valid_ip, sanitize_config_value, to_bool
from core.logging_setup import get_logger
from requests.adapters import HTTPAdapter

# pylint: disable=broad-exception-caught,too-many-lines,too-many-instance-attributes,too-many-public-methods
# pylint: disable=too-many-arguments,missing-function-docstring,invalid-name,too-many-locals
# pylint: disable=too-many-branches,too-many-statements,too-many-return-statements
# pylint: disable=too-many-boolean-expressions,no-else-return

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

    # amneziawg-go uses the first 12 bytes of each packet's S-padding as the header
    # protection cipher nonce, so every S value must be at least this large once a
    # HeaderProtectionKey is set (device/noise-types.go: HeaderCipherNonceSize).
    HEADER_CIPHER_NONCE_SIZE = 12

    # GeoIP lookups are cached to avoid rate limits; bounded so the dict cannot grow
    # without limit as new client endpoints appear.
    GEOIP_CACHE_TTL_SECONDS = 24 * 3600
    GEOIP_CACHE_MAX_ENTRIES = 512

    EGRESS_PROBE_SERVICES = (
        "https://api.ipify.org",
        "https://ident.me",
        "https://icanhazip.com",
    )

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
    ):
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

        self.config = self.load_config()
        self.ensure_directories()
        self.public_ip = self.detect_public_ip()

        # Track derived client status updates (active/inactive) and persist with throttling.
        self._client_status_dirty = False
        self._last_client_status_persist_ts = 0.0

        # Cache GeoIP lookups to avoid rate limits and latency
        # { ip: {"ts": epoch_seconds, "label": str, "raw": dict} }
        self._geoip_cache = {}

        # Auto-start servers based on environment variable
        if self.auto_start_servers_enabled:
            self.auto_start_servers()

        # Start real-time traffic monitoring
        self.start_traffic_monitoring()

    def ensure_directories(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.wireguard_config_dir, exist_ok=True)
        os.makedirs("/var/log/amnezia", exist_ok=True)

    def detect_public_ip(self):
        """Detect the public IP address of the server"""
        try:
            # Try multiple services in case one fails
            services = ["http://ifconfig.me", "https://api.ipify.org", "https://ident.me"]

            for service in services:
                try:
                    response = requests.get(service, timeout=5)
                    if response.status_code == 200:
                        ip = response.text.strip()
                        if self.is_valid_ip(ip):
                            logger.info("Detected public IP: %s", ip)
                            return ip
                except Exception:
                    continue

            # Fallback: read the source address the kernel picks for outbound traffic.
            # Parsed here instead of piping through awk/head so no shell is needed.
            route = self.run_command(["ip", "route", "get", "1.1.1.1"])
            if route:
                match = re.search(r"\bsrc\s+(\S+)", route)
                local_ip = match.group(1) if match else None
                if local_ip and self.is_valid_ip(local_ip):
                    logger.info("Detected local IP: %s", local_ip)
                    return local_ip

        except Exception as e:
            logger.error("Failed to detect public IP: %s", e)
        return "YOUR_SERVER_IP"  # Fallback

    def is_valid_ip(self, ip):
        """Check if the string is a valid IP address"""
        return is_valid_ip(ip)

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
        if not self.is_valid_ip(source_ip):
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
                if body and self.is_valid_ip(body):
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

        if not self.is_valid_ip(source_ip):
            return result

        try:
            proc = subprocess.run(
                ["ip", "route", "get", destination, "from", source_ip],
                capture_output=True,
                text=True,
                check=True,
            )
            raw = (proc.stdout or "").strip().splitlines()
            line = raw[0] if raw else ""
            result["raw"] = line

            if line:
                dev_match = re.search(r"\bdev\s+(\S+)", line)
                via_match = re.search(r"\bvia\s+(\S+)", line)
                src_match = re.search(r"\bsrc\s+(\S+)", line)
                result["dev"] = dev_match.group(1) if dev_match else None
                result["via"] = via_match.group(1) if via_match else None
                result["src"] = src_match.group(1) if src_match else None
        except Exception as e:
            result["raw"] = f"route lookup failed: {e}"

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

    def _cache_geoip(self, ip, now, label, country_code, raw):
        """Store a GeoIP result, evicting expired and then oldest entries.

        Unbounded growth was slow but real: one entry per distinct client endpoint IP,
        never removed.
        """
        self._geoip_cache[ip] = {
            "ts": now,
            "label": label,
            "country_code": country_code,
            "raw": raw,
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

    def lookup_geoip(self, ip):
        """Return (geo label, country code) for a public IP with caching."""
        if not self.enable_geoip:
            return (None, None)
        if not ip or not isinstance(ip, str):
            return (None, None)

        ip = ip.strip()
        try:
            addr = ipaddress.ip_address(ip)
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_multicast
                or addr.is_reserved
                or addr.is_unspecified
            ):
                return (None, None)
        except ValueError:
            return (None, None)

        now = time.time()
        cached = self._geoip_cache.get(ip)
        if isinstance(cached, dict) and (now - cached.get("ts", 0)) < self.GEOIP_CACHE_TTL_SECONDS:
            return (cached.get("label"), cached.get("country_code"))

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
                self._cache_geoip(ip, now, None, None, {"status": resp.status_code})
                return (None, None)

            content_type = resp.headers.get("content-type", "")
            data = resp.json() if content_type.startswith("application/json") else {}
            label = format_geo_label(data)
            country_code = extract_country_code(data)
            self._cache_geoip(ip, now, label, country_code, data)
            return (label, country_code)
        except Exception:
            self._cache_geoip(ip, now, None, None, {"error": "lookup_failed"})
            return (None, None)

    def auto_start_servers(self):
        """Auto-start servers that have config files and were running before"""
        logger.info("Checking for existing servers to auto-start...")
        for server in self.config["servers"]:
            if os.path.exists(server["config_path"]):
                current_status = self.get_server_status(server["id"])
                if current_status == "stopped" and server.get("auto_start", True):
                    logger.info(f"Auto-starting server: {server['name']}")
                    try:
                        self.start_server(server["id"])
                    except Exception as e:
                        # Never crash the Web UI on boot due to a VPN startup failure.
                        server_name = server.get("name", server.get("id"))
                        logger.error("Auto-start failed for server '%s': %s", server_name, e)
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
            return {"raw": f"{start}-{end}", "start": start, "end": end}

        if re.fullmatch(r"\d+", raw):
            number = int(raw)
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
        if transport["S1"] is not None and transport["S2"] is not None and transport["S1"] + 56 == transport["S2"]:
            raise ValueError("S1 + 56 must not equal S2")

        if protocol == "AWG 1.5":
            transport.pop("S3", None)
            transport.pop("S4", None)
        else:
            parsed_headers = [self.parse_header_value(transport[key], protocol) for key in ("H1", "H2", "H3", "H4")]
            for index, current in enumerate(parsed_headers):
                for other in parsed_headers[index + 1:]:
                    if current["start"] <= other["end"] and other["start"] <= current["end"]:
                        raise ValueError(f"H1-H4 ranges must not intersect for {protocol}")

        if self.protocol_supports_awg3(protocol):
            header_protection_key = sanitize_config_value(params.get("HeaderProtectionKey") or "")
            if header_protection_key:
                if not self.is_valid_wireguard_key(header_protection_key):
                    raise ValueError(
                        "HeaderProtectionKey must be a 32-byte base64 key (generate one with 'awg genkey')"
                    )
                # amneziawg-go refuses the config outright if any S value is below
                # the 12-byte header cipher nonce it slices out of the padding.
                for key in ("S1", "S2", "S3", "S4"):
                    value = transport.get(key)
                    if value is None or value < self.HEADER_CIPHER_NONCE_SIZE:
                        raise ValueError(
                            f"{key} must be at least {self.HEADER_CIPHER_NONCE_SIZE} "
                            "when HeaderProtectionKey is set"
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

        if jc <= 0:
            raise ValueError(f"Jc must be positive, got {jc}")
        if jmin <= 0:
            raise ValueError(f"Jmin must be positive, got {jmin}")
        if jmax <= 0:
            raise ValueError(f"Jmax must be positive, got {jmax}")
        if jmin > jmax:
            raise ValueError(f"Jmin must be less than or equal to Jmax, got Jmin={jmin}, Jmax={jmax}")

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

    def build_effective_client_params(self, server, client_params=None):
        transport_params = self.extract_transport_params(server.get("transport_params") or {}, server.get("protocol"))
        effective = dict(transport_params)
        effective.update(self.extract_client_params(client_params or {}))
        return effective

    def migrate_config_schema(self, config):
        if not isinstance(config, dict):
            return {"servers": [], "clients": {}}

        config.setdefault("servers", [])
        config.setdefault("clients", {})

        for server in config.get("servers", []):
            if not isinstance(server, dict):
                continue

            server["protocol"] = self.normalize_protocol(server.get("protocol"))

            legacy_params = server.get("obfuscation_params") if isinstance(server.get("obfuscation_params"), dict) else {}
            transport_params = server.get("transport_params")
            if not isinstance(transport_params, dict):
                transport_params = self.extract_transport_params(legacy_params, server.get("protocol"))
            else:
                transport_params = self.extract_transport_params(transport_params, server.get("protocol"))
            server["transport_params"] = transport_params

            server.setdefault("client_defaults", self.default_client_defaults())

            for client in server.get("clients", []) or []:
                if not isinstance(client, dict):
                    continue
                client_params = client.get("client_params")
                if not isinstance(client_params, dict):
                    client_params = self.extract_client_params(client.get("obfuscation_params") or legacy_params)
                else:
                    client_params = self.extract_client_params(client_params)
                client["client_params"] = client_params
                client["obfuscation_enabled"] = True
                client["obfuscation_params"] = self.build_effective_client_params(server, client_params)

                client_id = client.get("id")
                if client_id and isinstance(config.get("clients"), dict):
                    global_client = config["clients"].get(client_id)
                    if isinstance(global_client, dict):
                        global_client["client_params"] = dict(client_params)
                        global_client["obfuscation_enabled"] = True
                        global_client["obfuscation_params"] = self.build_effective_client_params(server, client_params)

            server["obfuscation_enabled"] = True
            server["obfuscation_params"] = self.build_effective_client_params(server, server.get("client_defaults"))

        return config

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                return self.migrate_config_schema(json.load(f))
        return self.migrate_config_schema({"servers": [], "clients": {}})

    def save_config(self):
        """Persist config atomically: a crash mid-write must not lose server keys."""
        directory = os.path.dirname(self.config_file) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.config_file}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.config_file)
        os.chmod(self.config_file, 0o600)

    def run_command(self, args):
        """Run a command from an argv list and return stdout, or None on failure.

        Always argv, never a shell string: values such as subnet, interface and
        port originate from API input, and argv form cannot be turned into extra
        shell commands.
        """
        try:
            result = subprocess.run(args, capture_output=True, text=True, check=True)
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
        S1 = random.randint(15, min(150, mtu - 148))
        s2_candidates = [s for s in range(15, min(150, mtu - 92) + 1) if s != S1 + 56]
        S2 = random.choice(s2_candidates)
        params = {
            "S1": S1,
            "S2": S2,
            "H1": random.randint(10000, 100000),
            "H2": random.randint(100000, 200000),
            "H3": random.randint(200000, 300000),
            "H4": random.randint(300000, 400000),
            "MTU": mtu,
        }
        if self.protocol_supports_s34(protocol):
            params["S3"] = random.randint(15, 150)
            # S4 pads every transport packet, so it stays small; with header
            # protection the 12-byte nonce is carved out of it, hence the floor.
            s4_low = self.HEADER_CIPHER_NONCE_SIZE if self.protocol_supports_awg3(protocol) else 0
            params["S4"] = random.randint(s4_low, 32)
        if self.protocol_supports_awg3(protocol):
            params["HeaderProtectionKey"] = self.generate_header_protection_key()
        return params

    def generate_client_defaults(self, mtu=1420):
        jmin = random.randint(4, mtu - 2)
        jmax = random.randint(jmin + 1, mtu)
        defaults = self.default_client_defaults()
        defaults.update({
            "Jc": random.randint(4, 12),
            "Jmin": jmin,
            "Jmax": jmax,
        })
        return defaults

    def create_wireguard_server(self, server_data):
        """Create a new WireGuard server configuration with environment defaults"""
        server_name = self.sanitize_name(server_data.get("name"), "New Server")
        port = server_data.get("port", self.default_port)
        subnet = server_data.get("subnet", self.default_subnet)
        mtu = server_data.get("mtu", self.default_mtu)

        # Get DNS servers from request or use environment default
        custom_dns = server_data.get("dns")
        if custom_dns:
            # Parse custom DNS from request
            if isinstance(custom_dns, str):
                dns_servers = [dns.strip() for dns in custom_dns.split(",") if dns.strip()]
            elif isinstance(custom_dns, list):
                dns_servers = custom_dns
            else:
                dns_servers = self.dns_servers
        else:
            dns_servers = self.dns_servers

        # Validate MTU
        if mtu < 1280 or mtu > 1440:
            raise ValueError(f"MTU must be between 1280 and 1440, got {mtu}")

        # Validate port and subnet before they reach config files or the iptables
        # scripts. Both arrive straight from the API.
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Port must be an integer, got {port!r}") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {port}")

        subnet = self.validate_subnet(subnet)
        self.assert_no_conflicts(port, subnet)

        # Validate DNS servers
        for dns in dns_servers:
            if not self.is_valid_ip(dns):
                raise ValueError(f"Invalid DNS server IP: {dns}")

        protocol = self.normalize_protocol(server_data.get("protocol"))
        auto_start = server_data.get("auto_start", self.auto_start_servers_enabled)
        enable_nat = to_bool(server_data.get("enable_nat"), self.default_enable_nat)
        block_lan_cidrs = to_bool(server_data.get("block_lan_cidrs"), self.default_block_lan_cidrs)

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
            raw_client_defaults = self.generate_client_defaults(mtu)
        client_defaults = self.validate_client_params(raw_client_defaults)

        # Parse subnet for server IP
        subnet_parts = subnet.split("/")
        network = subnet_parts[0]
        prefix = subnet_parts[1] if len(subnet_parts) > 1 else "24"
        server_ip = self.get_server_ip(network)

        del prefix  # _build_server_config_content derives this from the subnet.

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
            "transport_params": transport_params,
            "client_defaults": client_defaults,
            "auto_start": auto_start,
            "enable_nat": enable_nat,
            "block_lan_cidrs": block_lan_cidrs,
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

        server_ip = server.get("server_ip") or self.get_server_ip(subnet_parts[0])
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

        mtu = int(server.get("mtu", self.default_mtu))

        next_protocol = self.normalize_protocol(params.get("protocol", server.get("protocol")))
        next_transport_params = self.validate_transport_params(next_protocol, params)

        server["protocol"] = next_protocol
        server["transport_params"] = dict(next_transport_params)

        def apply_to_client_obj(client_obj):
            if not isinstance(client_obj, dict):
                return
            client_params = self.extract_client_params(client_obj.get("client_params") or {})
            client_obj["client_params"] = client_params

        for embedded in server.get("clients") or []:
            apply_to_client_obj(embedded)
            cid = embedded.get("id")
            if (
                cid
                and isinstance(self.config.get("clients"), dict)
                and cid in self.config["clients"]
            ):
                apply_to_client_obj(self.config["clients"][cid])

        # Rewrite server config file
        self.write_server_conf(server)

        self.save_config()

        # Restart if running
        was_running = self.get_server_status(server_id) == "running"
        restarted = False
        if was_running:
            if self.stop_server(server_id):
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

    def get_server_ip(self, network):
        """Get server IP from network (first usable IP)"""
        parts = network.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.1"
        return "10.0.0.1"

    def get_client_ip(self, server):
        """Return the first free client address in the server's subnet.

        Derived from the subnet rather than assuming a /24, and checks both places
        clients are stored so a stale embedded list cannot hand out a duplicate.
        Raises when the subnet is full — silently reusing an address would break the
        existing client that holds it.
        """
        subnet = server.get("subnet") or self.default_subnet
        server_ip = server.get("server_ip")

        try:
            network = ipaddress.ip_network(str(subnet), strict=False)
        except ValueError as exc:
            raise ValueError(f"Server {server.get('id')} has an invalid subnet '{subnet}'") from exc

        used = {server_ip}
        used.update(c.get("client_ip") for c in server.get("clients") or [])
        # The global map is the other half of the double bookkeeping (see DEVELOPMENT
        # §3); a client missing from the embedded list still owns its address.
        server_id = server.get("id")
        for client in (self.config.get("clients") or {}).values():
            if isinstance(client, dict) and client.get("server_id") == server_id:
                used.add(client.get("client_ip"))

        for candidate in network.hosts():
            text = str(candidate)
            if text not in used:
                return text

        raise ValueError(f"No free addresses left in {network} for server {server_id}")

    def get_server(self, server_id):
        return next((s for s in self.config.get("servers", []) if s.get("id") == server_id), None)

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
        return self.config.get("clients", {}).get(client_id)

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

        # Remove all clients associated with this server
        self.config["clients"] = {
            key: value
            for key, value in self.config["clients"].items()
            if value.get("server_id") != server_id
        }

        # Remove the server
        self.config["servers"] = [s for s in self.config["servers"] if s["id"] != server_id]
        self.save_config()
        return True

    def add_wireguard_client(self, server_id, client_name, client_params=None, copy_from_client_id=None):
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
            "server_name": server["name"],
            "status": "inactive",
            "created_at": time.time(),
            "client_private_key": client_keys["private_key"],
            "client_public_key": client_keys["public_key"],
            "preshared_key": preshared_key,
            "client_ip": client_ip,
            "protocol": server.get("protocol", self.DEFAULT_PROTOCOL),
            "suspended": False,
            "client_params": dict(base_client_params),
        }

        server["clients"].append(client_config)

        # Rewrite the whole file from state rather than appending a peer block, so
        # the config always matches self.config exactly.
        self.write_server_conf(server)

        # Store in global clients dict
        self.config["clients"][client_id] = client_config
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

    def update_client_params(self, server_id, client_id, params):
        """Update full client-side J/I parameters for a specific client."""
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

        client_params = self.validate_client_params(raw_client_params)

        client["client_params"] = client_params

        # Mirror update into the server-embedded client list too
        for embedded in server.get("clients", []):
            if embedded.get("id") != client_id:
                continue
            embedded["client_params"] = dict(client_params)
            break

        self.save_config()
        return client

    def rename_server(self, server_id, new_name):
        """Rename a server (display name only)."""
        server = self.get_server(server_id)
        if not server:
            return None
        new_name = self.sanitize_name(new_name)
        server["name"] = new_name
        for client in server.get("clients", []):
            client["server_name"] = new_name
            global_client = self.config.get("clients", {}).get(client["id"])
            if global_client:
                global_client["server_name"] = new_name
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
        new_name = self.sanitize_name(new_name)
        client["name"] = new_name
        for embedded in server.get("clients", []):
            if embedded.get("id") == client_id:
                embedded["name"] = new_name
                break
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

        new_state = not client.get("suspended", False)
        client["suspended"] = new_state

        for embedded in server.get("clients", []):
            if embedded.get("id") == client_id:
                embedded["suspended"] = new_state
                break

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

        # Remove client from server's client list
        server["clients"] = [c for c in server["clients"] if c["id"] != client_id]

        # Remove from global clients dict
        if client_id in self.config["clients"]:
            del self.config["clients"][client_id]

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
# Server IP: {server["public_ip"]}:{server["port"]}

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
Endpoint = {server["public_ip"]}:{server["port"]}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
        return config

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

    def start_server(self, server_id):
        """Start a WireGuard server using awg-quick with iptables setup"""
        server = self.get_server(server_id)
        if not server:
            return False

        try:
            # Use awg-quick to bring up the interface
            result = self.run_command(["/usr/bin/awg-quick", "up", server["interface"]])
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

    def get_server_status(self, server_id):
        """Check actual server status by checking interface"""
        server = self.get_server(server_id)
        if not server:
            return "not_found"

        # run_command returns None when the interface does not exist.
        result = self.run_command(["ip", "link", "show", server["interface"]])
        return "running" if result and "state UNKNOWN" in result else "stopped"

    def emit_status_after_delay(self, server_id, status, delay_seconds=2):
        """Push a server_status update to clients once the interface has settled.

        Runs as a Socket.IO background task rather than a real thread: the app runs
        under eventlet, where a blocking sleep in a plain thread does not cooperate
        with the event loop.
        """

        def emit_later():
            self.socketio.sleep(delay_seconds)
            self.socketio.emit("server_status", {"server_id": server_id, "status": status})

        self.socketio.start_background_task(emit_later)

    def start_traffic_monitoring(self):
        """Start background thread for real-time traffic monitoring"""

        # Use Socket.IO background tasks so this works correctly under eventlet.
        def monitor_traffic():
            while True:
                try:
                    # Get all running servers and their traffic
                    for server in self.config["servers"]:
                        # Check actual status, not cached
                        actual_status = self.get_server_status(server["id"])
                        if actual_status == "running":
                            traffic = self.get_traffic_for_server(server["id"])
                            if traffic:
                                self.socketio.emit(
                                    "traffic_update",
                                    {"server_id": server["id"], "traffic": traffic},
                                )

                    self.socketio.sleep(7)  # Update every 7 seconds
                except Exception as e:
                    logger.error("Error in traffic monitoring: %s", e)
                    self.socketio.sleep(7)

        self.socketio.start_background_task(monitor_traffic)

    def get_client_configs(self, server_id=None):
        """Get all client configs, optionally filtered by server"""
        if server_id:
            return [
                client
                for client in self.config["clients"].values()
                if client.get("server_id") == server_id
            ]
        return list(self.config["clients"].values())

    def get_traffic_for_server(self, server_id):
        server = self.get_server(server_id)
        if not server:
            return None

        interface = server["interface"]
        output = self.run_command(["/usr/bin/awg", "show", interface])
        if not output:
            return None

        # Parse output to get traffic+endpoint per peer public key
        peer_data = {}

        lines = output.splitlines()
        current_peer = None
        for line in lines:
            line = line.strip()
            if line.startswith("peer:"):
                current_peer = line.split("peer:")[1].strip()
                if current_peer:
                    peer_data.setdefault(current_peer, {})
            elif line.startswith("endpoint:") and current_peer:
                # Example: endpoint: 203.0.113.10:51820
                # Example IPv6: endpoint: [2001:db8::1]:51820
                endpoint = line.split("endpoint:", 1)[1].strip()
                peer_data.setdefault(current_peer, {})["endpoint"] = endpoint
            elif line.startswith("latest handshake:") and current_peer:
                # Example: latest handshake: 57 seconds ago
                # Example: latest handshake: 1 minute, 2 seconds ago
                handshake = line.split("latest handshake:", 1)[1].strip()
                peer_data.setdefault(current_peer, {})["latest_handshake"] = handshake
            elif line.startswith("transfer:") and current_peer:
                # Example: transfer: 1.39 MiB received, 6.59 MiB sent
                transfer_line = line[len("transfer:") :].strip()
                # Parse received and sent
                parts = transfer_line.split(",")
                received = parts[0].strip() if len(parts) > 0 else ""
                sent = parts[1].strip() if len(parts) > 1 else ""
                peer_data.setdefault(current_peer, {})["received"] = received
                peer_data.setdefault(current_peer, {})["sent"] = sent
                current_peer = None

        def extract_ip_from_endpoint(endpoint_value):
            if not endpoint_value or endpoint_value == "(none)":
                return None
            # IPv6 endpoint format: [ip]:port
            m = re.match(r"^\[([^\]]+)\]:(\d+)$", endpoint_value)
            if m:
                return m.group(1)
            # IPv4 endpoint format: ip:port
            m = re.match(r"^([^:]+):(\d+)$", endpoint_value)
            if m:
                return m.group(1)
            return None

        def parse_handshake_seconds(handshake_value):
            """Parse 'latest handshake' strings from `awg show` into seconds.

            Examples:
              - '57 seconds ago' -> 57
              - '1 minute, 2 seconds ago' -> 62
              - 'Never' -> None
            """
            if not handshake_value or not isinstance(handshake_value, str):
                return None
            s = handshake_value.strip().lower()
            if not s:
                return None
            if "never" in s:
                return None
            if "just now" in s:
                return 0

            total = 0
            unit_seconds = {
                "second": 1,
                "minute": 60,
                "hour": 3600,
                "day": 86400,
            }
            for m in re.finditer(r"(\d+)\s+(second|minute|hour|day)s?", s):
                try:
                    n = int(m.group(1))
                    unit = m.group(2)
                    total += n * unit_seconds.get(unit, 0)
                except Exception:
                    continue
            return total if total > 0 else None

        def geoip_lookup(ip):
            return self.lookup_geoip(ip)

        # Map peer data to clients by matching public keys
        clients_traffic = {}
        for client_id, client in self.config["clients"].items():
            if client.get("server_id") == server_id:
                pubkey = client.get("client_public_key")
                info = peer_data.get(pubkey) if pubkey else None
                received = (info or {}).get("received") or "0 B"
                sent = (info or {}).get("sent") or "0 B"
                endpoint = (info or {}).get("endpoint")
                latest_handshake = (info or {}).get("latest_handshake")
                latest_handshake_seconds = parse_handshake_seconds(latest_handshake)
                active = latest_handshake_seconds is not None and latest_handshake_seconds <= 5 * 60
                endpoint_ip = extract_ip_from_endpoint(endpoint)
                geo_label, geo_country_code = geoip_lookup(endpoint_ip)

                # Persist derived status into the existing client config field.
                # This makes /api/* clients reflect live activity without the UI needing traffic.
                try:
                    desired_status = "active" if active else "inactive"
                    if client.get("status") != desired_status:
                        client["status"] = desired_status
                        self._client_status_dirty = True
                except Exception:
                    # Don't let status persistence break traffic reporting.
                    pass

                clients_traffic[client_id] = {
                    "received": received,
                    "sent": sent,
                    "endpoint": endpoint,
                    "geo": geo_label,
                    "geo_country_code": geo_country_code,
                    "latest_handshake": latest_handshake,
                    "latest_handshake_seconds": latest_handshake_seconds,
                    "active": active,
                }

        # Throttle config writes: persist derived status at most once per minute.
        if self._client_status_dirty:
            now = time.time()
            if (now - self._last_client_status_persist_ts) >= 60:
                try:
                    self.save_config()
                    self._client_status_dirty = False
                    self._last_client_status_persist_ts = now
                except Exception as e:
                    logger.error("Failed to persist client status updates: %s", e)
        return clients_traffic
