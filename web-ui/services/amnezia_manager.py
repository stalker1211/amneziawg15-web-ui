"""Core service logic for managing AmneziaWG servers and clients."""

import base64
import ipaddress
import json
import os
import random
import re
import subprocess
import threading
import time
import uuid

import requests
from core.helpers import is_valid_ip, sanitize_config_value, to_bool
from requests.adapters import HTTPAdapter

# pylint: disable=broad-exception-caught,too-many-lines,too-many-instance-attributes,too-many-public-methods
# pylint: disable=too-many-arguments,missing-function-docstring,invalid-name,too-many-locals
# pylint: disable=too-many-branches,too-many-statements,too-many-return-statements
# pylint: disable=too-many-boolean-expressions,no-else-return


class AmneziaManager:
    """Manage VPN server lifecycle, clients, configs, and runtime telemetry."""

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
        enable_obfuscation=True,
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

        self.enable_obfuscation = enable_obfuscation
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
                            print(f"Detected public IP: {ip}")
                            return ip
                except Exception:
                    continue

            # Fallback: try to get from network interfaces
            try:
                result = self.execute_command("ip route get 1 | awk '{print $7}' | head -1")
                if result and self.is_valid_ip(result):
                    print(f"Detected local IP: {result}")
                    return result
            except Exception:
                pass

        except Exception as e:
            print(f"Failed to detect public IP: {e}")

        return "YOUR_SERVER_IP"  # Fallback

    def is_valid_ip(self, ip):
        """Check if the string is a valid IP address"""
        return is_valid_ip(ip)

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

    def detect_public_ip_from_source(self, source_ip):
        """Detect external IP for traffic originating from a specific source IP."""
        if not self.is_valid_ip(source_ip):
            raise ValueError(f"Invalid source IP: {source_ip}")

        with requests.Session() as session:
            adapter = self._SourceAddressAdapter(source_ip)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            service = "https://1.1.1.1/cdn-cgi/trace"
            try:
                response = session.get(service, timeout=8)
                if response.status_code != 200:
                    raise RuntimeError(f"{service}: HTTP {response.status_code}")

                body = response.text.strip()
                ip = None
                for line in body.splitlines():
                    if line.startswith("ip="):
                        ip = line.split("=", 1)[1].strip()
                        break

                if ip and self.is_valid_ip(ip):
                    return ip, service

                raise RuntimeError(f"{service}: invalid trace response")
            except Exception as e:
                raise RuntimeError(f"{service}: {e}") from e

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

        probe = {
            "source_ip": source_ip,
            "route": route,
            "checked_at": int(time.time()),
            "external_ip": None,
            "service": None,
            "error": None,
        }

        try:
            external_ip, service = self.detect_public_ip_from_source(source_ip)
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
        if isinstance(cached, dict) and (now - cached.get("ts", 0)) < 24 * 3600:
            return (cached.get("label"), cached.get("country_code"))

        def format_geo_label(raw):
            if not isinstance(raw, dict):
                return None
            country = raw.get("country") or raw.get("country_name") or raw.get("countryCode")
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
            cc = raw.get("country") or raw.get("country_code") or raw.get("countryCode")
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
                self._geoip_cache[ip] = {
                    "ts": now,
                    "label": None,
                    "country_code": None,
                    "raw": {"status": resp.status_code},
                }
                return (None, None)

            content_type = resp.headers.get("content-type", "")
            data = resp.json() if content_type.startswith("application/json") else {}
            label = format_geo_label(data)
            country_code = extract_country_code(data)
            self._geoip_cache[ip] = {
                "ts": now,
                "label": label,
                "country_code": country_code,
                "raw": data,
            }
            return (label, country_code)
        except Exception:
            self._geoip_cache[ip] = {
                "ts": now,
                "label": None,
                "country_code": None,
                "raw": {"error": "lookup_failed"},
            }
            return (None, None)

    def auto_start_servers(self):
        """Auto-start servers that have config files and were running before"""
        print("Checking for existing servers to auto-start...")
        for server in self.config["servers"]:
            if os.path.exists(server["config_path"]):
                current_status = self.get_server_status(server["id"])
                if current_status == "stopped" and server.get("auto_start", True):
                    print(f"Auto-starting server: {server['name']}")
                    try:
                        self.start_server(server["id"])
                    except Exception as e:
                        # Never crash the Web UI on boot due to a VPN startup failure.
                        server_name = server.get("name", server.get("id"))
                        print(f"Auto-start failed for server '{server_name}': {e}")

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"servers": [], "clients": {}}

    def save_config(self):
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def execute_command(self, command):
        """Execute shell command and return result"""
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"Command failed: {e}")
            return None

    def generate_wireguard_keys(self):
        """Generate real WireGuard keys"""
        try:
            private_key = self.execute_command("wg genkey")
            if private_key:
                public_key = self.execute_command(f"echo '{private_key}' | wg pubkey")
                return {"private_key": private_key, "public_key": public_key}
        except Exception as e:
            print(f"Key generation failed: {e}")

        # Fallback - generate random keys
        fake_private = base64.b64encode(os.urandom(32)).decode("utf-8")
        fake_public = base64.b64encode(os.urandom(32)).decode("utf-8")
        return {"private_key": fake_private, "public_key": fake_public}

    def generate_preshared_key(self):
        """Generate preshared key"""
        try:
            return self.execute_command("wg genpsk")
        except Exception:
            return base64.b64encode(os.urandom(32)).decode("utf-8")

    def generate_obfuscation_params(self, mtu=1420):
        S1 = random.randint(15, min(150, mtu - 148))
        # S2 must not be S1+56
        s2_candidates = [s for s in range(15, min(150, mtu - 92) + 1) if s != S1 + 56]
        S2 = random.choice(s2_candidates)
        # S3/S4: message paddings (same value-space as S1/S2)
        S3 = random.randint(15, 150)
        S4 = random.randint(15, 150)
        Jmin = random.randint(4, mtu - 2)
        Jmax = random.randint(Jmin + 1, mtu)
        return {
            "Jc": random.randint(4, 12),
            "Jmin": Jmin,
            "Jmax": Jmax,
            "S1": S1,
            "S2": S2,
            "S3": S3,
            "S4": S4,
            "H1": random.randint(10000, 100000),
            "H2": random.randint(100000, 200000),
            "H3": random.randint(200000, 300000),
            "H4": random.randint(300000, 400000),
            "I1": "",
            "I2": "",
            "I3": "",
            "I4": "",
            "I5": "",
            "MTU": mtu,
        }

    def create_wireguard_server(self, server_data):
        """Create a new WireGuard server configuration with environment defaults"""
        server_name = server_data.get("name", "New Server")
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

        # Validate DNS servers
        for dns in dns_servers:
            if not self.is_valid_ip(dns):
                raise ValueError(f"Invalid DNS server IP: {dns}")

        # Fixed values for other settings
        enable_obfuscation = server_data.get("obfuscation", self.enable_obfuscation)
        auto_start = server_data.get("auto_start", self.auto_start_servers_enabled)
        enable_nat = to_bool(server_data.get("enable_nat"), self.default_enable_nat)
        block_lan_cidrs = to_bool(server_data.get("block_lan_cidrs"), self.default_block_lan_cidrs)

        server_id = str(uuid.uuid4())[:6]
        interface_name = f"wg-{server_id}"
        config_path = os.path.join(self.wireguard_config_dir, f"{interface_name}.conf")

        # Generate server keys
        server_keys = self.generate_wireguard_keys()

        # Generate and use provided obfuscation parameters if enabled
        obfuscation_params = None
        if enable_obfuscation:
            if "obfuscation_params" in server_data:
                obfuscation_params = server_data["obfuscation_params"]
            else:
                obfuscation_params = self.generate_obfuscation_params(mtu)

            # Ensure new I1-I5 keys exist (empty defaults are OK)
            if isinstance(obfuscation_params, dict):
                for key in ("I1", "I2", "I3", "I4", "I5"):
                    obfuscation_params.setdefault(key, "")

        # Parse subnet for server IP
        subnet_parts = subnet.split("/")
        network = subnet_parts[0]
        prefix = subnet_parts[1] if len(subnet_parts) > 1 else "24"
        server_ip = self.get_server_ip(network)

        # Create WireGuard server configuration
        server_config_content = f"""[Interface]
PrivateKey = {server_keys["private_key"]}
Address = {server_ip}/{prefix}
ListenPort = {port}
SaveConfig = false
MTU = {mtu}
"""

        # Add obfuscation parameters if enabled
        if enable_obfuscation and obfuscation_params:

            def _opt_line(key):
                # Allow empty/omitted S params: AWG treats missing as 0.
                v = obfuscation_params.get(key, None)
                if v is None or v == "":
                    return ""
                return f"{key} = {v}\n"

            server_config_content += f"""Jc = {obfuscation_params["Jc"]}
Jmin = {obfuscation_params["Jmin"]}
Jmax = {obfuscation_params["Jmax"]}
{_opt_line("S1")}{_opt_line("S2")}{_opt_line("S3")}{_opt_line("S4")}H1 = {obfuscation_params["H1"]}
H1 = {obfuscation_params["H1"]}
H2 = {obfuscation_params["H2"]}
H3 = {obfuscation_params["H3"]}
H4 = {obfuscation_params["H4"]}
"""

        server_config = {
            "id": server_id,
            "name": server_name,
            "protocol": "wireguard",
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
            "obfuscation_enabled": enable_obfuscation,
            "obfuscation_params": obfuscation_params,
            "auto_start": auto_start,
            "enable_nat": enable_nat,
            "block_lan_cidrs": block_lan_cidrs,
            "dns": dns_servers,  # Store DNS servers
            "clients": [],
            "created_at": time.time(),
        }

        # Save WireGuard config file
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(server_config_content)

        self.config["servers"].append(server_config)
        self.save_config()

        # Auto-start if enabled (from environment or request)
        if auto_start:
            print(f"Auto-starting new server: {server_name}")
            self.start_server(server_id)

        return server_config

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

        if server.get("obfuscation_enabled") and isinstance(server.get("obfuscation_params"), dict):
            p = server.get("obfuscation_params") or {}

            def _opt_line(key):
                v = p.get(key, None)
                if v is None or v == "":
                    return ""
                return f"{key} = {v}\n"

            content += f"""Jc = {p.get("Jc", 0)}
Jmin = {p.get("Jmin", 0)}
Jmax = {p.get("Jmax", 0)}
{_opt_line("S1")}{_opt_line("S2")}{_opt_line("S3")}{_opt_line("S4")}H1 = {p.get("H1", 0)}
H2 = {p.get("H2", 0)}
H3 = {p.get("H3", 0)}
H4 = {p.get("H4", 0)}
"""

        for client in server.get("clients") or []:
            try:
                content += f"""

# Client: {client.get("name", client.get("id", "client"))}
[Peer]
PublicKey = {client["client_public_key"]}
PresharedKey = {client["preshared_key"]}
AllowedIPs = {client["client_ip"]}/32
"""
            except Exception as e:
                print(f"Failed to render client peer block: {e}")

        return content

    def update_server_obfuscation_params(self, server_id, params):
        """Update server obfuscation params, rewrite server config file, and restart if running.

        This updates server AND all existing clients' shared obfuscation params (J/S/H).
        Per-client I1-I5 are preserved.
        """
        server = self.get_server(server_id)
        if not server:
            return None

        if not server.get("obfuscation_enabled"):
            raise ValueError("Obfuscation is disabled for this server")

        if not isinstance(params, dict):
            raise ValueError("Invalid payload")

        mtu = int(server.get("mtu", self.default_mtu))

        def as_int(key):
            if key not in params:
                raise ValueError(f"Missing '{key}'")
            try:
                return int(params.get(key))
            except Exception as exc:
                raise ValueError(f"'{key}' must be an integer") from exc

        def as_opt_int(key):
            # Allow null/empty string to mean “unset” (omit from config)
            if key not in params:
                return None
            value = params.get(key)
            if value is None:
                return None
            if isinstance(value, str) and value.strip() == "":
                return None
            try:
                return int(value)
            except Exception as exc:
                raise ValueError(f"'{key}' must be an integer or empty") from exc

        next_params = {
            "Jc": as_int("Jc"),
            "Jmin": as_int("Jmin"),
            "Jmax": as_int("Jmax"),
            "S1": as_opt_int("S1"),
            "S2": as_opt_int("S2"),
            "S3": as_opt_int("S3"),
            "S4": as_opt_int("S4"),
            "H1": as_int("H1"),
            "H2": as_int("H2"),
            "H3": as_int("H3"),
            "H4": as_int("H4"),
        }

        # Validation (aligned with frontend rules)
        if not 4 <= next_params["Jc"] <= 12:
            raise ValueError(f"Jc must be in [4, 12], got {next_params['Jc']}")

        if not next_params["Jmin"] < next_params["Jmax"] <= mtu and next_params["Jmin"] < mtu:
            raise ValueError(
                f"Jmin/Jmax invalid for MTU {mtu}: "
                f"Jmin={next_params['Jmin']}, Jmax={next_params['Jmax']}"
            )

        if next_params["S1"] is not None:
            if not 15 <= next_params["S1"] <= 150 and next_params["S1"] <= (mtu - 148):
                raise ValueError(
                    f"S1 must be in [15, 150] and ≤ (MTU - 148) ({mtu - 148}), "
                    f"got {next_params['S1']}"
                )

        if next_params["S2"] is not None:
            if not 15 <= next_params["S2"] <= 150 and next_params["S2"] <= (mtu - 92):
                raise ValueError(
                    f"S2 must be in [15, 150] and ≤ (MTU - 92) ({mtu - 92}), "
                    f"got {next_params['S2']}"
                )

        if next_params["S1"] is not None and next_params["S2"] is not None:
            if next_params["S1"] + 56 == next_params["S2"]:
                raise ValueError(
                    "S1 + 56 must not equal S2 "
                    f"(S1={next_params['S1']}, S2={next_params['S2']})"
                )

        for k in ("S3", "S4"):
            if next_params[k] is not None and not 15 <= next_params[k] <= 150:
                raise ValueError(f"{k} must be in [15, 150], got {next_params[k]}")

        for k in ("H1", "H2", "H3", "H4"):
            if next_params[k] < 0:
                raise ValueError(f"{k} must be non-negative")

        # Apply to server (preserve I1-I5). Allow clearing S params.
        if not isinstance(server.get("obfuscation_params"), dict):
            server["obfuscation_params"] = {}
        for k, v in next_params.items():
            if k in ("S1", "S2", "S3", "S4") and v is None:
                server["obfuscation_params"].pop(k, None)
            else:
                server["obfuscation_params"][k] = v
        for key in ("I1", "I2", "I3", "I4", "I5"):
            server["obfuscation_params"].setdefault(key, "")

        # Apply to embedded server clients + global client dict (preserve per-client I1-I5)
        def apply_to_client_obj(client_obj):
            if not isinstance(client_obj, dict):
                return
            if not isinstance(client_obj.get("obfuscation_params"), dict):
                client_obj["obfuscation_params"] = {}
            for k, v in next_params.items():
                if k in ("S1", "S2", "S3", "S4") and v is None:
                    client_obj["obfuscation_params"].pop(k, None)
                else:
                    client_obj["obfuscation_params"][k] = v
            for key in ("I1", "I2", "I3", "I4", "I5"):
                client_obj["obfuscation_params"].setdefault(key, "")

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
        content = self._build_server_config_content(server)
        with open(server["config_path"], "w", encoding="utf-8") as f:
            f.write(content)

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
        """Apply the latest config to the running WireGuard interface using wg syncconf."""
        try:
            # Use bash -c to support process substitution
            command = f"bash -c 'awg syncconf {interface} <(awg-quick strip {interface})'"
            result = self.execute_command(command)
            if result is not None:
                print(f"Live config applied to {interface}")
                return True
            else:
                print(f"Failed to apply live config to {interface}")
                return False
        except Exception as e:
            print(f"Error applying live config to {interface}: {e}")
            return False

    def get_server_ip(self, network):
        """Get server IP from network (first usable IP)"""
        parts = network.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.1"
        return "10.0.0.1"

    def get_client_ip(self, server, client_index):
        """Get client IP from server subnet"""
        parts = server["server_ip"].split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.{client_index + 2}"
        return f"10.0.0.{client_index + 2}"

    def get_server(self, server_id):
        return next((s for s in self.config.get("servers", []) if s.get("id") == server_id), None)

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

        # Stop the server if running
        if server["status"] == "running":
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

    def add_wireguard_client(self, server_id, client_name, i_params=None):
        """Add a client to a WireGuard server"""
        server = self.get_server(server_id)
        if not server:
            return None

        client_id = str(uuid.uuid4())[:6]

        # Generate client keys
        client_keys = self.generate_wireguard_keys()
        preshared_key = self.generate_preshared_key()

        # Assign client IP
        client_ip = self.get_client_ip(server, len(server["clients"]))

        # Copy server defaults so future edits to the server do not affect existing clients.
        server_obf_params = (
            server.get("obfuscation_params")
            if server.get("obfuscation_enabled")
            else None
        )
        if isinstance(server_obf_params, dict):
            client_obf_params = dict(server_obf_params)
        else:
            client_obf_params = server_obf_params

        # Optional per-client I1-I5 overrides (client-only)
        if isinstance(client_obf_params, dict) and isinstance(i_params, dict):
            for key in ("I1", "I2", "I3", "I4", "I5"):
                if key in i_params:
                    client_obf_params[key] = sanitize_config_value(i_params.get(key, ""))

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
            "obfuscation_enabled": server["obfuscation_enabled"],
            "obfuscation_params": client_obf_params,
        }

        # Add client to server config
        client_peer_config = f"""
# Client: {client_config["name"]}
[Peer]
PublicKey = {client_keys["public_key"]}
PresharedKey = {preshared_key}
AllowedIPs = {client_ip}/32
"""

        # Append client to server config file
        with open(server["config_path"], "a", encoding="utf-8") as f:
            f.write(client_peer_config)

        server["clients"].append(client_config)

        # Store in global clients dict
        self.config["clients"][client_id] = client_config
        self.save_config()
        # Apply live config if server is running
        if server["status"] == "running":
            self.apply_live_config(server["interface"])
        print(f"Client {client_config['name']} added")

        config_content = self.generate_wireguard_client_config(
            server,
            client_config,
            include_comments=True,
        )
        return client_config, config_content

    def update_server_i_params(self, server_id, i_params):
        """Update server-level default I1-I5 parameters (used for NEW clients only).

        These parameters are NOT written to the server config file and do not
        require restarting the server. Existing clients are NOT modified.
        """
        server = self.get_server(server_id)
        if not server:
            return None

        if (
            not server.get("obfuscation_params")
            or not isinstance(server.get("obfuscation_params"), dict)
        ):
            server["obfuscation_params"] = {}

        for key in ("I1", "I2", "I3", "I4", "I5"):
            if key in i_params:
                server["obfuscation_params"][key] = sanitize_config_value(i_params.get(key, ""))
            else:
                server["obfuscation_params"].setdefault(key, "")

        self.save_config()
        return server

    def update_client_i_params(self, server_id, client_id, i_params):
        """Update client-only I1-I5 parameters for a specific client."""
        server = self.get_server(server_id)
        if not server:
            return None

        client = self.get_client(client_id)
        if not client or client.get("server_id") != server_id:
            return None

        if (
            not client.get("obfuscation_params")
            or not isinstance(client.get("obfuscation_params"), dict)
        ):
            client["obfuscation_params"] = {}

        for key in ("I1", "I2", "I3", "I4", "I5"):
            if key in i_params:
                client["obfuscation_params"][key] = sanitize_config_value(i_params.get(key, ""))
            else:
                client["obfuscation_params"].setdefault(key, "")

        # Mirror update into the server-embedded client list too
        for embedded in server.get("clients", []):
            if embedded.get("id") != client_id:
                continue
            if (
                not embedded.get("obfuscation_params")
                or not isinstance(embedded.get("obfuscation_params"), dict)
            ):
                embedded["obfuscation_params"] = {}
            for key in ("I1", "I2", "I3", "I4", "I5"):
                embedded["obfuscation_params"][key] = client["obfuscation_params"].get(key, "")
            break

        self.save_config()
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

        # Rewrite the config file without the deleted client's [Peer] block
        self.rewrite_server_conf_without_client(server, client)

        self.save_config()

        # Apply live config if server is running
        if server["status"] == "running":
            self.apply_live_config(server["interface"])
        print(f"Client {server['name']}:{client['name']} removed")

        return True

    def rewrite_server_conf_without_client(self, server, client):
        """Rewrite the server conf file without the specified client's [Peer] block"""
        if not os.path.exists(server["config_path"]):
            return

        with open(server["config_path"], "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        skip = False
        client_marker = f"# Client: {client['name']}"

        for line in lines:
            stripped = line.strip()

            # Start skipping when we find the client marker line
            if stripped == client_marker:
                skip = True
                continue

            # Stop skipping when we hit the next client marker line
            if skip and stripped.startswith("# Client:"):
                skip = False

            # If skipping, skip all lines until next client marker
            if skip:
                continue

            # Otherwise, keep the line
            new_lines.append(line)

        # Remove trailing blank lines if any
        while new_lines and new_lines[-1].strip() == "":
            new_lines.pop()

        with open(server["config_path"], "w", encoding="utf-8") as f:
            f.writelines(new_lines)

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

        # Add obfuscation parameters if enabled
        if client_config["obfuscation_enabled"] and client_config["obfuscation_params"]:
            params = client_config["obfuscation_params"]

            def _opt_line(key):
                v = params.get(key, None)
                if v is None or v == "":
                    return ""
                return f"{key} = {v}\n"

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

        config += f"""
[Peer]
PublicKey = {server["server_public_key"]}
PresharedKey = {client_config["preshared_key"]}
Endpoint = {server["public_ip"]}:{server["port"]}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
        return config

    def setup_iptables(self, interface, subnet, enable_nat=None, block_lan_cidrs=None):
        """Setup iptables rules for WireGuard interface"""
        try:
            script_path = "/app/scripts/setup_iptables.sh"
            if os.path.exists(script_path):
                env_parts = []
                if enable_nat is not None:
                    env_parts.append(f"ENABLE_NAT={'1' if enable_nat else '0'}")
                if block_lan_cidrs is not None:
                    env_parts.append(f"BLOCK_LAN_CIDRS={'1' if block_lan_cidrs else '0'}")
                env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
                result = self.execute_command(f"{env_prefix}{script_path} {interface} {subnet}")
                if result is not None:
                    print(f"iptables setup completed for {interface}")
                    return True
                else:
                    print(f"iptables setup failed for {interface}")
                    return False
            else:
                print(f"iptables script not found at {script_path}")
                return False
        except Exception as e:
            print(f"Error setting up iptables for {interface}: {e}")
            return False

    def cleanup_iptables(self, interface, subnet, enable_nat=None, block_lan_cidrs=None):
        """Cleanup iptables rules for WireGuard interface"""
        try:
            script_path = "/app/scripts/cleanup_iptables.sh"
            if os.path.exists(script_path):
                env_parts = []
                if enable_nat is not None:
                    env_parts.append(f"ENABLE_NAT={'1' if enable_nat else '0'}")
                if block_lan_cidrs is not None:
                    env_parts.append(f"BLOCK_LAN_CIDRS={'1' if block_lan_cidrs else '0'}")
                env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
                result = self.execute_command(f"{env_prefix}{script_path} {interface} {subnet}")
                if result is not None:
                    print(f"iptables cleanup completed for {interface}")
                    return True
                else:
                    print(f"iptables cleanup failed for {interface}")
                    return False
            else:
                print(f"iptables cleanup script not found at {script_path}")
                return False
        except Exception as e:
            print(f"Error cleaning up iptables for {interface}: {e}")
            return False

    def start_server(self, server_id):
        """Start a WireGuard server using awg-quick with iptables setup"""
        server = self.get_server(server_id)
        if not server:
            return False

        try:
            # Use awg-quick to bring up the interface
            result = self.execute_command(f"/usr/bin/awg-quick up {server['interface']}")
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

                print(f"Server {server['name']} started successfully")
                if iptables_success:
                    print(f"iptables rules configured for {server['interface']}")
                else:
                    print(f"Warning: iptables setup may have failed for {server['interface']}")

                threading.Thread(
                    target=self.simulate_server_operation,
                    args=(server_id, "running"),
                ).start()
                return True
            else:
                print(f"Failed to start server {server['name']}")
        except Exception as e:
            print(f"Failed to start server {server_id}: {e}")

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
            result = self.execute_command(f"/usr/bin/awg-quick down {server['interface']}")
            if result is not None:
                server["status"] = "stopped"
                self.save_config()

                print(f"Server {server['name']} stopped successfully")
                if iptables_cleaned:
                    print(f"iptables rules cleaned up for {server['interface']}")

                threading.Thread(
                    target=self.simulate_server_operation,
                    args=(server_id, "stopped"),
                ).start()
                return True
            else:
                print(f"Failed to stop server {server['name']}")
        except Exception as e:
            print(f"Failed to stop server {server_id}: {e}")

        return False

    def get_server_status(self, server_id):
        """Check actual server status by checking interface"""
        server = self.get_server(server_id)
        if not server:
            return "not_found"

        try:
            # Check if interface exists and is up
            result = self.execute_command(f"ip link show {server['interface']} 2>/dev/null")
            if result and "state UNKNOWN" in result:
                return "running"
            else:
                return "stopped"
        except Exception:
            return "stopped"

    def simulate_server_operation(self, server_id, status):
        """Simulate server operation with status updates"""
        time.sleep(2)
        self.socketio.emit("server_status", {"server_id": server_id, "status": status})

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
                    print(f"Error in traffic monitoring: {e}")
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
        output = self.execute_command(f"/usr/bin/awg show {interface}")
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
                    print(f"Failed to persist client status updates: {e}")

        return clients_traffic
