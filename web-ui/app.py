#!/usr/bin/env python3
import os
import io
import json
import subprocess
import uuid
import base64
import random
import requests
import re
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
from flask_socketio import SocketIO
import threading
import time

# Get the absolute path to the current directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# Essential environment variables
NGINX_PORT = os.getenv('NGINX_PORT', '80')
AUTO_START_SERVERS = os.getenv('AUTO_START_SERVERS', 'true').lower() == 'true'
DEFAULT_MTU = int(os.getenv('DEFAULT_MTU', '1280'))
DEFAULT_SUBNET = os.getenv('DEFAULT_SUBNET', '10.0.0.0/24')
DEFAULT_PORT = int(os.getenv('DEFAULT_PORT', '51820'))
DEFAULT_DNS = os.getenv('DEFAULT_DNS', '8.8.8.8,1.1.1.1')
DEFAULT_ENABLE_NAT = os.getenv('ENABLE_NAT', '1').strip().lower() not in ('0', 'false', 'no', 'off')
DEFAULT_BLOCK_LAN_CIDRS = os.getenv('BLOCK_LAN_CIDRS', '1').strip().lower() not in ('0', 'false', 'no', 'off')

# Parse DNS servers from comma-separated string
DNS_SERVERS = [dns.strip() for dns in DEFAULT_DNS.split(',') if dns.strip()]

# Fixed values for other settings
WEB_UI_PORT = 5000
CONFIG_DIR = '/etc/amnezia'
WIREGUARD_CONFIG_DIR = os.path.join(CONFIG_DIR, 'amneziawg')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'web_config.json')
PUBLIC_IP_SERVICE = 'http://ifconfig.me'
ENABLE_OBFUSCATION = True
ENABLE_GEOIP = os.getenv('ENABLE_GEOIP', '1').strip().lower() not in ('0', 'false', 'no', 'off')

# API Token Auth (optional, for defense-in-depth)
API_TOKEN = os.getenv('API_TOKEN', '').strip()

# Socket.IO CORS origins (comma-separated list or '*' for all)
# Empty/not set = same-origin only (recommended for production)
ALLOWED_ORIGINS_RAW = os.getenv('ALLOWED_ORIGINS', '').strip()
if ALLOWED_ORIGINS_RAW == '*':
    ALLOWED_ORIGINS = '*'
elif ALLOWED_ORIGINS_RAW:
    # Parse comma-separated list and strip whitespace
    ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS_RAW.split(',') if origin.strip()]
else:
    # Default: same-origin only (let Flask-SocketIO use its default behavior)
    ALLOWED_ORIGINS = []

print(f"Base directory: {BASE_DIR}")
print(f"Template directory: {TEMPLATE_DIR}")
print(f"Static directory: {STATIC_DIR}")
# Print environment configuration for debugging
print("=== Environment Configuration ===")
print(f"NGINX_PORT: {NGINX_PORT}")
print(f"AUTO_START_SERVERS: {AUTO_START_SERVERS}")
print(f"DEFAULT_MTU: {DEFAULT_MTU}")
print(f"DEFAULT_SUBNET: {DEFAULT_SUBNET}")
print(f"DEFAULT_PORT: {DEFAULT_PORT}")
print(f"DEFAULT_DNS: {DEFAULT_DNS}")
print(f"DEFAULT_ENABLE_NAT: {DEFAULT_ENABLE_NAT}")
print(f"DEFAULT_BLOCK_LAN_CIDRS: {DEFAULT_BLOCK_LAN_CIDRS}")
print(f"DNS_SERVERS: {DNS_SERVERS}")
print("==================================")
print("Fixed Configuration:")
print(f"WEB_UI_PORT: {WEB_UI_PORT} (internal)")
print(f"CONFIG_DIR: {CONFIG_DIR}")
print(f"ENABLE_OBFUSCATION: {ENABLE_OBFUSCATION}")
print(f"API_TOKEN: {'<set>' if API_TOKEN else '<not set>'}")
print(f"ALLOWED_ORIGINS: {ALLOWED_ORIGINS if ALLOWED_ORIGINS else '<same-origin only>'}")
print("==================================")

# Check if directories exist
print(f"Templates exist: {os.path.exists(TEMPLATE_DIR)}")
print(f"Static exist: {os.path.exists(STATIC_DIR)}")
if os.path.exists(TEMPLATE_DIR):
    print(f"Template files: {os.listdir(TEMPLATE_DIR)}")
if os.path.exists(STATIC_DIR):
    print(f"Static files: {os.listdir(STATIC_DIR)}")

app = Flask(__name__,
    template_folder=TEMPLATE_DIR,
    static_folder=STATIC_DIR
)
app.secret_key = os.urandom(24)

# Configure Socket.IO with CORS restrictions
if ALLOWED_ORIGINS:
    socketio = SocketIO(
        app,
        async_mode='eventlet',
        cors_allowed_origins=ALLOWED_ORIGINS,
        path='/socket.io'
    )
else:
    # Default: same-origin only (no cors_allowed_origins parameter)
    socketio = SocketIO(
        app,
        async_mode='eventlet',
        path='/socket.io'
    )

# API Token Auth decorator
def require_token(f):
    """Enforce API token auth if API_TOKEN is set (defense-in-depth with Nginx Basic Auth)."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        # If no API_TOKEN is configured, allow access (rely on Nginx Basic Auth)
        if not API_TOKEN:
            return f(*args, **kwargs)

        # Support either:
        # - Authorization: Bearer <token> (useful when there is no proxy auth)
        # - X-API-Token: <token>         (works alongside Nginx Basic Auth)
        token = (request.headers.get('X-API-Token') or '').strip()
        if not token:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:].strip()  # Remove 'Bearer ' prefix

        if not token:
            return jsonify({"error": "Missing API token (use X-API-Token header or Authorization: Bearer ...)"}), 401

        if token != API_TOKEN:
            return jsonify({"error": "Invalid API token"}), 401
        
        return f(*args, **kwargs)
    return decorated


def _sanitize_config_value(value):
    """Make sure config values are single-line to keep config format intact."""
    return str(value).replace('\r', ' ').replace('\n', ' ').strip()


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('', 'none', 'null'):
            return default
        return s not in ('0', 'false', 'no', 'off')
    return bool(value)


class AmneziaManager:
    def __init__(self):
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
        if AUTO_START_SERVERS:
            self.auto_start_servers()
        
        # Start real-time traffic monitoring
        self.start_traffic_monitoring()

    def ensure_directories(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        os.makedirs(WIREGUARD_CONFIG_DIR, exist_ok=True)
        os.makedirs('/var/log/amnezia', exist_ok=True)

    def detect_public_ip(self):
        """Detect the public IP address of the server"""
        try:
            # Try multiple services in case one fails
            services = [
                'http://ifconfig.me',
                'https://api.ipify.org',
                'https://ident.me'
            ]

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
        try:
            parts = ip.split('.')
            if len(parts) != 4:
                return False
            for part in parts:
                if not 0 <= int(part) <= 255:
                    return False
            return True
        except Exception:
            return False

    def auto_start_servers(self):
        """Auto-start servers that have config files and were running before"""
        print("Checking for existing servers to auto-start...")
        for server in self.config["servers"]:
            if os.path.exists(server['config_path']):
                current_status = self.get_server_status(server['id'])
                if current_status == 'stopped' and server.get('auto_start', True):
                    print(f"Auto-starting server: {server['name']}")
                    self.start_server(server['id'])

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        return {"servers": [], "clients": {}}

    def save_config(self):
        with open(CONFIG_FILE, 'w') as f:
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
                return {
                    "private_key": private_key,
                    "public_key": public_key
                }
        except Exception as e:
            print(f"Key generation failed: {e}")

        # Fallback - generate random keys
        fake_private = base64.b64encode(os.urandom(32)).decode('utf-8')
        fake_public = base64.b64encode(os.urandom(32)).decode('utf-8')
        return {
            "private_key": fake_private,
            "public_key": fake_public
        }

    def generate_preshared_key(self):
        """Generate preshared key"""
        try:
            return self.execute_command("wg genpsk")
        except Exception:
            return base64.b64encode(os.urandom(32)).decode('utf-8')

    def generate_obfuscation_params(self, mtu=1420):
        S1 = random.randint(15, min(150, mtu - 148))
        # S2 must not be S1+56
        s2_candidates = [s for s in range(15, min(150, mtu - 92) + 1) if s != S1 + 56]
        S2 = random.choice(s2_candidates)
        Jmin = random.randint(4, mtu - 2)
        Jmax = random.randint(Jmin + 1, mtu)
        return {
            "Jc": random.randint(4, 12),
            "Jmin": Jmin,
            "Jmax": Jmax,
            "S1": S1,
            "S2": S2,
            "H1": random.randint(10000, 100000),
            "H2": random.randint(100000, 200000),
            "H3": random.randint(200000, 300000),
            "H4": random.randint(300000, 400000),
            "I1": "",
            "I2": "",
            "I3": "",
            "I4": "",
            "I5": "",
            "MTU": mtu
        }

    def create_wireguard_server(self, server_data):
        """Create a new WireGuard server configuration with environment defaults"""
        server_name = server_data.get('name', 'New Server')
        port = server_data.get('port', DEFAULT_PORT)
        subnet = server_data.get('subnet', DEFAULT_SUBNET)
        mtu = server_data.get('mtu', DEFAULT_MTU)

        # Get DNS servers from request or use environment default
        custom_dns = server_data.get('dns')
        if custom_dns:
            # Parse custom DNS from request
            if isinstance(custom_dns, str):
                dns_servers = [dns.strip() for dns in custom_dns.split(',') if dns.strip()]
            elif isinstance(custom_dns, list):
                dns_servers = custom_dns
            else:
                dns_servers = DNS_SERVERS
        else:
            dns_servers = DNS_SERVERS

        # Validate MTU
        if mtu < 1280 or mtu > 1440:
            raise ValueError(f"MTU must be between 1280 and 1440, got {mtu}")

        # Validate DNS servers
        for dns in dns_servers:
            if not self.is_valid_ip(dns):
                raise ValueError(f"Invalid DNS server IP: {dns}")

        # Fixed values for other settings
        enable_obfuscation = server_data.get('obfuscation', ENABLE_OBFUSCATION)
        auto_start = server_data.get('auto_start', AUTO_START_SERVERS)
        enable_nat = _to_bool(server_data.get('enable_nat'), DEFAULT_ENABLE_NAT)
        block_lan_cidrs = _to_bool(server_data.get('block_lan_cidrs'), DEFAULT_BLOCK_LAN_CIDRS)

        server_id = str(uuid.uuid4())[:6]
        interface_name = f"wg-{server_id}"
        config_path = os.path.join(WIREGUARD_CONFIG_DIR, f"{interface_name}.conf")

        # Generate server keys
        server_keys = self.generate_wireguard_keys()

        # Generate and use provided obfuscation parameters if enabled
        obfuscation_params = None
        if enable_obfuscation:
            if 'obfuscation_params' in server_data:
                obfuscation_params = server_data['obfuscation_params']
            else:
                obfuscation_params = self.generate_obfuscation_params(mtu)

            # Ensure new I1-I5 keys exist (empty defaults are OK)
            if isinstance(obfuscation_params, dict):
                for key in ("I1", "I2", "I3", "I4", "I5"):
                    obfuscation_params.setdefault(key, "")

        # Parse subnet for server IP
        subnet_parts = subnet.split('/')
        network = subnet_parts[0]
        prefix = subnet_parts[1] if len(subnet_parts) > 1 else "24"
        server_ip = self.get_server_ip(network)

        # Create WireGuard server configuration
        server_config_content = f"""[Interface]
PrivateKey = {server_keys['private_key']}
Address = {server_ip}/{prefix}
ListenPort = {port}
SaveConfig = false
MTU = {mtu}
"""

        # Add obfuscation parameters if enabled
        if enable_obfuscation and obfuscation_params:

            server_config_content += f"""Jc = {obfuscation_params['Jc']}
Jmin = {obfuscation_params['Jmin']}
Jmax = {obfuscation_params['Jmax']}
S1 = {obfuscation_params['S1']}
S2 = {obfuscation_params['S2']}
H1 = {obfuscation_params['H1']}
H2 = {obfuscation_params['H2']}
H3 = {obfuscation_params['H3']}
H4 = {obfuscation_params['H4']}
"""

        server_config = {
            "id": server_id,
            "name": server_name,
            "protocol": "wireguard",
            "port": port,
            "status": "stopped",
            "interface": interface_name,
            "config_path": config_path,
            "server_public_key": server_keys['public_key'],
            "server_private_key": server_keys['private_key'],
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
            "created_at": time.time()
        }

        # Save WireGuard config file
        with open(config_path, 'w') as f:
            f.write(server_config_content)

        self.config["servers"].append(server_config)
        self.save_config()

        # Auto-start if enabled (from environment or request)
        if auto_start:
            print(f"Auto-starting new server: {server_name}")
            self.start_server(server_id)

        return server_config
    
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
        parts = network.split('.')
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.1"
        return "10.0.0.1"

    def get_client_ip(self, server, client_index):
        """Get client IP from server subnet"""
        parts = server['server_ip'].split('.')
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.{client_index + 2}"
        return f"10.0.0.{client_index + 2}"

    def get_server(self, server_id):
        return next((s for s in self.config.get('servers', []) if s.get('id') == server_id), None)

    def reapply_iptables_for_server(self, server):
        """Reapply iptables rules for a running server after networking changes."""
        if not server:
            return False
        self.cleanup_iptables(
            server['interface'],
            server['subnet'],
            enable_nat=server.get('enable_nat'),
            block_lan_cidrs=server.get('block_lan_cidrs')
        )
        return self.setup_iptables(
            server['interface'],
            server['subnet'],
            enable_nat=server.get('enable_nat'),
            block_lan_cidrs=server.get('block_lan_cidrs')
        )

    def get_client(self, client_id):
        return self.config.get('clients', {}).get(client_id)

    def delete_server(self, server_id):
        """Delete a server and all its clients"""
        server = self.get_server(server_id)
        if not server:
            return False

        # Stop the server if running
        if server['status'] == 'running':
            self.stop_server(server_id)

        # Remove config file
        if os.path.exists(server['config_path']):
            os.remove(server['config_path'])

        # Remove all clients associated with this server
        self.config["clients"] = {k: v for k, v in self.config["clients"].items()
                                if v.get("server_id") != server_id}

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
        client_ip = self.get_client_ip(server, len(server['clients']))

        # Copy server defaults so future edits to the server do not affect existing clients
        server_obf_params = server.get("obfuscation_params") if server.get("obfuscation_enabled") else None
        if isinstance(server_obf_params, dict):
            client_obf_params = dict(server_obf_params)
        else:
            client_obf_params = server_obf_params

        # Optional per-client I1-I5 overrides (client-only)
        if isinstance(client_obf_params, dict) and isinstance(i_params, dict):
            for key in ("I1", "I2", "I3", "I4", "I5"):
                if key in i_params:
                    client_obf_params[key] = _sanitize_config_value(i_params.get(key, ''))

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
            "obfuscation_params": client_obf_params
        }

        # Add client to server config
        client_peer_config = f"""
# Client: {client_config['name']}
[Peer]
PublicKey = {client_keys['public_key']}
PresharedKey = {preshared_key}
AllowedIPs = {client_ip}/32
"""

        # Append client to server config file
        with open(server['config_path'], 'a') as f:
            f.write(client_peer_config)

        server["clients"].append(client_config)

        # Store in global clients dict
        self.config["clients"][client_id] = client_config
        self.save_config()
        
        # Apply live config if server is running
        if server['status'] == 'running':
            self.apply_live_config(server['interface'])
            
        print(f"Client {client_config['name']} added")

        config_content = self.generate_wireguard_client_config(server, client_config, include_comments=True)
        return client_config, config_content

    def update_server_i_params(self, server_id, i_params):
        """Update server-level default I1-I5 parameters (used for NEW clients only).

        These parameters are NOT written to the server config file and do not
        require restarting the server. Existing clients are NOT modified.
        """
        server = self.get_server(server_id)
        if not server:
            return None

        if not server.get('obfuscation_params') or not isinstance(server.get('obfuscation_params'), dict):
            server['obfuscation_params'] = {}

        for key in ("I1", "I2", "I3", "I4", "I5"):
            if key in i_params:
                server['obfuscation_params'][key] = _sanitize_config_value(i_params.get(key, ''))
            else:
                server['obfuscation_params'].setdefault(key, "")

        self.save_config()
        return server

    def update_client_i_params(self, server_id, client_id, i_params):
        """Update client-only I1-I5 parameters for a specific client."""
        server = self.get_server(server_id)
        if not server:
            return None

        client = self.get_client(client_id)
        if not client or client.get('server_id') != server_id:
            return None

        if not client.get('obfuscation_params') or not isinstance(client.get('obfuscation_params'), dict):
            client['obfuscation_params'] = {}

        for key in ("I1", "I2", "I3", "I4", "I5"):
            if key in i_params:
                client['obfuscation_params'][key] = _sanitize_config_value(i_params.get(key, ''))
            else:
                client['obfuscation_params'].setdefault(key, "")

        # Mirror update into the server-embedded client list too
        for embedded in server.get('clients', []):
            if embedded.get('id') != client_id:
                continue
            if not embedded.get('obfuscation_params') or not isinstance(embedded.get('obfuscation_params'), dict):
                embedded['obfuscation_params'] = {}
            for key in ("I1", "I2", "I3", "I4", "I5"):
                embedded['obfuscation_params'][key] = client['obfuscation_params'].get(key, "")
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
        if server['status'] == 'running':
            self.apply_live_config(server['interface'])
            
        print(f"Client {server['name']}:{client['name']} removed")

        return True
    
    def rewrite_server_conf_without_client(self, server, client):
        """Rewrite the server conf file without the specified client's [Peer] block"""
        if not os.path.exists(server['config_path']):
            return

        with open(server['config_path'], 'r') as f:
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
        while new_lines and new_lines[-1].strip() == '':
            new_lines.pop()

        with open(server['config_path'], 'w') as f:
            f.writelines(new_lines)

    def generate_wireguard_client_config(self, server, client_config, include_comments=True):
        """Generate WireGuard client configuration"""
        config = ""
        
        # Add comments only if requested
        if include_comments:
            config = f"""# AmneziaWG Client Configuration
# Server: {server['name']}
# Client: {client_config['name']}
# Generated: {time.ctime()}
# Server IP: {server['public_ip']}:{server['port']}

"""

        config += f"""[Interface]
PrivateKey = {client_config['client_private_key']}
Address = {client_config['client_ip']}/32
DNS = {', '.join(server['dns'])}
MTU = {server['mtu']}
"""

        # Add obfuscation parameters if enabled
        if client_config['obfuscation_enabled'] and client_config['obfuscation_params']:
            params = client_config['obfuscation_params']

            i_lines = []
            for key in ("I1", "I2", "I3", "I4", "I5"):
                value = _sanitize_config_value(params.get(key, ''))
                if value:
                    i_lines.append(f"{key} = {value}")

            config += f"""Jc = {params['Jc']}
Jmin = {params['Jmin']}
Jmax = {params['Jmax']}
S1 = {params['S1']}
S2 = {params['S2']}
H1 = {params['H1']}
H2 = {params['H2']}
H3 = {params['H3']}
H4 = {params['H4']}
"""

            if i_lines:
                config += "\n".join(i_lines) + "\n"

        config += f"""
[Peer]
PublicKey = {server['server_public_key']}
PresharedKey = {client_config['preshared_key']}
Endpoint = {server['public_ip']}:{server['port']}
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
                    server['interface'],
                    server['subnet'],
                    enable_nat=server.get('enable_nat'),
                    block_lan_cidrs=server.get('block_lan_cidrs')
                )

                server['status'] = 'running'
                self.save_config()

                print(f"Server {server['name']} started successfully")
                if iptables_success:
                    print(f"iptables rules configured for {server['interface']}")
                else:
                    print(f"Warning: iptables setup may have failed for {server['interface']}")

                threading.Thread(target=self.simulate_server_operation, args=(server_id, 'running')).start()
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
                server['interface'],
                server['subnet'],
                enable_nat=server.get('enable_nat'),
                block_lan_cidrs=server.get('block_lan_cidrs')
            )

            # Use awg-quick to bring down the interface
            result = self.execute_command(f"/usr/bin/awg-quick down {server['interface']}")
            if result is not None:
                server['status'] = 'stopped'
                self.save_config()

                print(f"Server {server['name']} stopped successfully")
                if iptables_cleaned:
                    print(f"iptables rules cleaned up for {server['interface']}")

                threading.Thread(target=self.simulate_server_operation, args=(server_id, 'stopped')).start()
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
        socketio.emit('server_status', {
            'server_id': server_id,
            'status': status
        })

    def start_traffic_monitoring(self):
        """Start background thread for real-time traffic monitoring"""
        # Use Socket.IO background tasks so this works correctly under eventlet.
        def monitor_traffic():
            while True:
                try:
                    # Get all running servers and their traffic
                    for server in self.config["servers"]:
                        # Check actual status, not cached
                        actual_status = self.get_server_status(server['id'])
                        if actual_status == 'running':
                            traffic = self.get_traffic_for_server(server['id'])
                            if traffic:
                                socketio.emit(
                                    'traffic_update',
                                    {
                                        'server_id': server['id'],
                                        'traffic': traffic
                                    }
                                )

                    socketio.sleep(7)  # Update every 7 seconds
                except Exception as e:
                    print(f"Error in traffic monitoring: {e}")
                    socketio.sleep(7)

        socketio.start_background_task(monitor_traffic)

    def get_client_configs(self, server_id=None):
        """Get all client configs, optionally filtered by server"""
        if server_id:
            return [client for client in self.config["clients"].values()
                   if client.get("server_id") == server_id]
        return list(self.config["clients"].values())

    def get_traffic_for_server(self, server_id):
        server = self.get_server(server_id)
        if not server:
            return None

        interface = server['interface']
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
                transfer_line = line[len("transfer:"):].strip()
                # Parse received and sent
                parts = transfer_line.split(',')
                received = parts[0].strip() if len(parts) > 0 else ""
                sent = parts[1].strip() if len(parts) > 1 else ""
                peer_data.setdefault(current_peer, {})["received"] = received
                peer_data.setdefault(current_peer, {})["sent"] = sent
                current_peer = None

        def extract_ip_from_endpoint(endpoint_value):
            if not endpoint_value or endpoint_value == '(none)':
                return None
            # IPv6 endpoint format: [ip]:port
            m = re.match(r'^\[([^\]]+)\]:(\d+)$', endpoint_value)
            if m:
                return m.group(1)
            # IPv4 endpoint format: ip:port
            m = re.match(r'^([^:]+):(\d+)$', endpoint_value)
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
            if 'never' in s:
                return None
            if 'just now' in s:
                return 0

            total = 0
            unit_seconds = {
                'second': 1,
                'minute': 60,
                'hour': 3600,
                'day': 86400,
            }
            for m in re.finditer(r'(\d+)\s+(second|minute|hour|day)s?', s):
                try:
                    n = int(m.group(1))
                    unit = m.group(2)
                    total += n * unit_seconds.get(unit, 0)
                except Exception:
                    continue
            return total if total > 0 else None

        def format_geo_label(raw):
            if not isinstance(raw, dict):
                return None
            country = raw.get('country') or raw.get('country_name') or raw.get('countryCode')
            city = raw.get('city')
            region = raw.get('region') or raw.get('regionName')

            loc_parts = [p for p in [city, region] if p]
            loc = ', '.join(loc_parts).strip()

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
            # ipapi.co typically returns ISO 3166-1 alpha-2 in `country`
            cc = raw.get('country') or raw.get('country_code') or raw.get('countryCode')
            if isinstance(cc, str):
                cc = cc.strip().upper()
                if re.fullmatch(r'[A-Z]{2}', cc):
                    return cc
            return None

        def geoip_lookup(ip):
            if not ENABLE_GEOIP:
                return (None, None)
            if not ip:
                return (None, None)

            # Skip obvious local addresses
            if ip.startswith('10.') or ip.startswith('192.168.') or ip.startswith('172.16.') or ip.startswith('172.17.') or ip.startswith('172.18.') or ip.startswith('172.19.') or ip.startswith('172.2') or ip.startswith('172.3') or ip.startswith('127.'):
                return (None, None)

            now = time.time()
            cached = self._geoip_cache.get(ip)
            if isinstance(cached, dict) and (now - cached.get('ts', 0)) < 24 * 3600:
                return (cached.get('label'), cached.get('country_code'))

            try:
                # ipapi.co uses HTTPS and returns JSON
                resp = requests.get(f"https://ipapi.co/{ip}/json/", timeout=2, headers={"User-Agent": "amneziawg-web-ui"})
                if resp.status_code != 200:
                    self._geoip_cache[ip] = {"ts": now, "label": None, "country_code": None, "raw": {"status": resp.status_code}}
                    return (None, None)
                data = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
                label = format_geo_label(data)
                country_code = extract_country_code(data)
                self._geoip_cache[ip] = {"ts": now, "label": label, "country_code": country_code, "raw": data}
                return (label, country_code)
            except Exception:
                self._geoip_cache[ip] = {"ts": now, "label": None, "country_code": None, "raw": {"error": "lookup_failed"}}
                return (None, None)

        # Map peer data to clients by matching public keys
        clients_traffic = {}
        for client_id, client in self.config["clients"].items():
            if client.get("server_id") == server_id:
                pubkey = client.get("client_public_key")
                info = peer_data.get(pubkey) if pubkey else None
                received = (info or {}).get('received') or "0 B"
                sent = (info or {}).get('sent') or "0 B"
                endpoint = (info or {}).get('endpoint')
                latest_handshake = (info or {}).get('latest_handshake')
                latest_handshake_seconds = parse_handshake_seconds(latest_handshake)
                active = latest_handshake_seconds is not None and latest_handshake_seconds <= 5 * 60
                endpoint_ip = extract_ip_from_endpoint(endpoint)
                geo_label, geo_country_code = geoip_lookup(endpoint_ip)

                # Persist derived status into the existing client config field.
                # This makes /api/* clients reflect live activity without the UI needing traffic.
                try:
                    desired_status = 'active' if active else 'inactive'
                    if client.get('status') != desired_status:
                        client['status'] = desired_status
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
                    "active": active
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


amnezia_manager = AmneziaManager()

# API Routes
@app.route('/')
def index():
    print("Serving index.html")
    # Cache-bust static assets so browsers pick up new JS/CSS immediately.
    try:
        js_path = os.path.join(STATIC_DIR, 'js', 'app.js')
        css_path = os.path.join(STATIC_DIR, 'css', 'style.css')
        cache_bust = int(max(os.path.getmtime(js_path), os.path.getmtime(css_path)))
    except OSError:
        cache_bust = int(time.time())

    return render_template('index.html', cache_bust=cache_bust)

# Explicit static file route to ensure they're served
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route('/api/servers', methods=['POST'])
@require_token
def create_server():
    data = request.get_json(silent=True) or {}
    server = amnezia_manager.create_wireguard_server(data)
    return jsonify(server)

@app.route('/api/servers/<server_id>', methods=['DELETE'])
@require_token
def delete_server(server_id):
    if amnezia_manager.delete_server(server_id):
        return jsonify({"status": "deleted", "server_id": server_id})
    return jsonify({"error": "Server not found"}), 404

@app.route('/api/servers/<server_id>/start', methods=['POST'])
@require_token
def start_server(server_id):
    if amnezia_manager.start_server(server_id):
        return jsonify({"status": "started"})
    return jsonify({"error": "Server not found or failed to start"}), 404

@app.route('/api/servers/<server_id>/stop', methods=['POST'])
@require_token
def stop_server(server_id):
    if amnezia_manager.stop_server(server_id):
        return jsonify({"status": "stopped"})
    return jsonify({"error": "Server not found or failed to stop"}), 404

@app.route('/api/servers/<server_id>/clients', methods=['GET'])
@require_token
def get_server_clients(server_id):
    clients = amnezia_manager.get_client_configs(server_id)
    return jsonify(clients)

@app.route('/api/servers/<server_id>/clients', methods=['POST'])
@require_token
def add_client(server_id):
    data = request.get_json(silent=True) or {}
    client_name = data.get('name', 'New Client')

    # Optional per-client I1-I5 overrides
    i_params = None
    if isinstance(data, dict):
        raw = data.get('i_params')
        if raw is None:
            raw = data.get('obfuscation_params')
        if isinstance(raw, dict):
            i_params = {k: raw.get(k, "") for k in ("I1", "I2", "I3", "I4", "I5") if k in raw}

    result = amnezia_manager.add_wireguard_client(server_id, client_name, i_params=i_params)
    if result:
        client_config, config_content = result
        return jsonify({
            "client": client_config,
            "config": config_content
        })
    return jsonify({"error": "Server not found"}), 404

@app.route('/api/servers/<server_id>/clients/<client_id>', methods=['DELETE'])
@require_token
def delete_client(server_id, client_id):
    if amnezia_manager.delete_client(server_id, client_id):
        return jsonify({"status": "deleted", "client_id": client_id})
    return jsonify({"error": "Client not found"}), 404

@app.route('/api/servers/<server_id>/clients/<client_id>/config')
@require_token
def download_client_config(server_id, client_id):
    """Download client configuration file (with comments)"""
    client = amnezia_manager.get_client(client_id)
    if not client or client.get("server_id") != server_id:
        return jsonify({"error": "Client not found"}), 404

    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    # Use full version with comments for download
    config_content = amnezia_manager.generate_wireguard_client_config(
        server, client, include_comments=True
    )

    raw_filename = f"{client.get('name', 'client')}_{server.get('name', 'server')}.conf"

    def sanitize_filename(value):
        # Keep it simple and dependency-free: allow alnum, dot, dash, underscore.
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value)).strip('._')
        # Avoid pathological lengths and empty names
        return (safe[:200] or "client")

    filename = sanitize_filename(raw_filename) + ".conf" if not raw_filename.endswith('.conf') else sanitize_filename(raw_filename)
    if not filename.lower().endswith('.conf'):
        filename += '.conf'

    data = io.BytesIO(config_content.encode('utf-8'))
    data.seek(0)
    return send_file(
        data,
        as_attachment=True,
        download_name=filename,
        mimetype='text/plain; charset=utf-8'
    )

@app.route('/api/clients', methods=['GET'])
@require_token
def get_all_clients():
    clients = amnezia_manager.get_client_configs()
    return jsonify(clients)

@app.route('/api/system/status')
@require_token
def system_status():
    status = {
        "awg_available": os.path.exists("/usr/bin/awg") and os.path.exists("/usr/bin/awg-quick"),
        "public_ip": amnezia_manager.public_ip,
        "total_servers": len(amnezia_manager.config["servers"]),
        "total_clients": len(amnezia_manager.config["clients"]),
        "active_servers": len([s for s in amnezia_manager.config["servers"]
                             if amnezia_manager.get_server_status(s["id"]) == "running"]),
        "timestamp": time.time(),
        "environment": {
            "nginx_port": NGINX_PORT,
            "auto_start_servers": AUTO_START_SERVERS,
            "default_mtu": DEFAULT_MTU,
            "default_subnet": DEFAULT_SUBNET,
            "default_port": DEFAULT_PORT,
            "default_dns": DEFAULT_DNS
        }
    }
    return jsonify(status)

@app.route('/api/system/refresh-ip')
@require_token
def refresh_ip():
    """Refresh public IP address"""
    new_ip = amnezia_manager.detect_public_ip()
    amnezia_manager.public_ip = new_ip

    # Update all servers with new IP
    for server in amnezia_manager.config["servers"]:
        server["public_ip"] = new_ip

    amnezia_manager.save_config()
    return jsonify({"public_ip": new_ip})

@app.route('/api/servers/<server_id>/config')
@require_token
def get_server_config(server_id):
    """Get the raw WireGuard server configuration"""
    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    try:
        # Read the actual config file
        if os.path.exists(server['config_path']):
            with open(server['config_path'], 'r') as f:
                config_content = f.read()

            return jsonify({
                "server_id": server_id,
                "server_name": server['name'],
                "config_path": server['config_path'],
                "config_content": config_content,
                "interface": server['interface'],
                "public_key": server['server_public_key']
            })
        else:
            return jsonify({"error": "Config file not found"}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to read config: {str(e)}"}), 500

@app.route('/api/servers/<server_id>/config/download')
@require_token
def download_server_config(server_id):
    """Download the WireGuard server configuration file"""
    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    try:
        if os.path.exists(server['config_path']):
            return send_file(
                server['config_path'],
                as_attachment=True,
                download_name=f"{server['interface']}.conf"
            )
        else:
            return jsonify({"error": "Config file not found"}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to download config: {str(e)}"}), 500

@app.route('/api/servers/<server_id>/info')
@require_token
def get_server_info(server_id):
    """Get detailed server information including config preview"""
    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    # Get current status
    current_status = amnezia_manager.get_server_status(server_id)
    server['current_status'] = current_status

    # Try to read config file for preview
    config_preview = ""
    if os.path.exists(server['config_path']):
        try:
            with open(server['config_path'], 'r') as f:
                # Read first 10 lines for preview
                lines = f.readlines()
                config_preview = ''.join(lines[:min(10, len(lines))])
        except Exception:
            config_preview = "Unable to read config file"

    # Ensure MTU is included (handle both old and new servers)
    mtu_value = server.get('mtu', 1420)  # Default to 1420 if not set

    server_info = {
        "id": server['id'],
        "name": server['name'],
        "protocol": server['protocol'],
        "port": server['port'],
        "status": current_status,
        "interface": server['interface'],
        "config_path": server['config_path'],
        "public_ip": server['public_ip'],
        "server_ip": server['server_ip'],
        "subnet": server['subnet'],
        "mtu": mtu_value,  # Make sure MTU is included
        "obfuscation_enabled": server['obfuscation_enabled'],
        "obfuscation_params": server.get('obfuscation_params', {}),
        "enable_nat": server.get('enable_nat', DEFAULT_ENABLE_NAT),
        "block_lan_cidrs": server.get('block_lan_cidrs', DEFAULT_BLOCK_LAN_CIDRS),
        "clients_count": len(server['clients']),
        "created_at": server['created_at'],
        "config_preview": config_preview,
        "public_key": server['server_public_key'],
        "dns": server['dns']
    }

    return jsonify(server_info)


@app.route('/api/servers/<server_id>/networking', methods=['POST'])
@require_token
def update_server_networking(server_id):
    """Update per-server networking flags (ENABLE_NAT/BLOCK_LAN_CIDRS)."""
    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    data = request.get_json(silent=True) or {}
    enable_nat = _to_bool(data.get('enable_nat'), server.get('enable_nat', DEFAULT_ENABLE_NAT))
    block_lan_cidrs = _to_bool(data.get('block_lan_cidrs'), server.get('block_lan_cidrs', DEFAULT_BLOCK_LAN_CIDRS))

    server['enable_nat'] = enable_nat
    server['block_lan_cidrs'] = block_lan_cidrs
    amnezia_manager.save_config()

    iptables_status = 'skipped'
    if amnezia_manager.get_server_status(server_id) == 'running':
        ok = amnezia_manager.reapply_iptables_for_server(server)
        iptables_status = 'reapplied' if ok else 'failed'

    return jsonify({
        "status": "updated",
        "server_id": server_id,
        "enable_nat": enable_nat,
        "block_lan_cidrs": block_lan_cidrs,
        "iptables": iptables_status
    })


@app.route('/api/servers/<server_id>/i-params', methods=['POST'])
@require_token
def update_server_i_params(server_id):
    """Update server-level default I1-I5 (used for NEW clients only)."""
    data = request.json or {}
    i_params = {}
    for key in ("I1", "I2", "I3", "I4", "I5"):
        if key in data:
            i_params[key] = data.get(key, "")

    updated_server = amnezia_manager.update_server_i_params(server_id, i_params)
    if not updated_server:
        return jsonify({"error": "Server not found"}), 404

    return jsonify({
        "status": "updated",
        "server_id": server_id,
        "obfuscation_params": updated_server.get('obfuscation_params', {})
    })


@app.route('/api/servers/<server_id>/clients/<client_id>/i-params', methods=['POST'])
@require_token
def update_client_i_params(server_id, client_id):
    """Update client-only I1-I5 parameters for a specific client."""
    data = request.json or {}
    i_params = {}
    for key in ("I1", "I2", "I3", "I4", "I5"):
        if key in data:
            i_params[key] = data.get(key, "")

    updated_client = amnezia_manager.update_client_i_params(server_id, client_id, i_params)
    if not updated_client:
        return jsonify({"error": "Client not found"}), 404

    return jsonify({
        "status": "updated",
        "server_id": server_id,
        "client_id": client_id,
        "obfuscation_params": updated_client.get('obfuscation_params', {})
    })

@app.route('/api/servers', methods=['GET'])
@require_token
def get_servers():
    # Update server status based on actual interface state
    for server in amnezia_manager.config["servers"]:
        server["status"] = amnezia_manager.get_server_status(server["id"])
        # Ensure MTU is included in basic server list
        if 'mtu' not in server:
            server['mtu'] = 1420  # Default value
        if 'enable_nat' not in server:
            server['enable_nat'] = DEFAULT_ENABLE_NAT
        if 'block_lan_cidrs' not in server:
            server['block_lan_cidrs'] = DEFAULT_BLOCK_LAN_CIDRS

    amnezia_manager.save_config()
    return jsonify(amnezia_manager.config["servers"])

@app.route('/api/system/iptables-test')
@require_token
def iptables_test():
    """Test iptables setup for a specific server"""
    server_id = request.args.get('server_id')
    if not server_id:
        return jsonify({"error": "server_id parameter required"}), 400

    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    # Test iptables rules
    try:
        # Check if rules exist
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
    
@app.route('/api/servers/<server_id>/clients/<client_id>/config-both')
@require_token
def get_client_config_both(server_id, client_id):
    """Get both clean and full client configurations"""
    client = amnezia_manager.get_client(client_id)
    if not client or client.get("server_id") != server_id:
        return jsonify({"error": "Client not found"}), 404

    server = amnezia_manager.get_server(server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    # Generate both versions
    clean_config = amnezia_manager.generate_wireguard_client_config(
        server, client, include_comments=False
    )
    
    full_config = amnezia_manager.generate_wireguard_client_config(
        server, client, include_comments=True
    )
    
    return jsonify({
        "server_id": server_id,
        "client_id": client_id,
        "client_name": client['name'],
        "clean_config": clean_config,
        "full_config": full_config,
        "clean_length": len(clean_config),
        "full_length": len(full_config)
    })
    
@app.route('/api/servers/<server_id>/traffic')
@require_token
def get_server_traffic(server_id):
    traffic = amnezia_manager.get_traffic_for_server(server_id)
    if traffic is None:
        return jsonify({"error": "Server not found or no traffic data"}), 404
    return jsonify(traffic)

@app.route('/status')
def get_container_uptime():
    # Get the modification time of /proc/1/cmdline (container start time epoch)
    result = subprocess.check_output(["stat", "-c %Y", "/proc/1/cmdline"], text=True)
    uptime_seconds_epoch = int(result.strip())

    now_epoch = int(time.time())
    
    uptime_seconds = now_epoch - uptime_seconds_epoch
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    
    return f"Container Uptime: {days}d {hours}h {minutes}m {seconds}s"

@socketio.on('connect')
def handle_connect():
    print(f"WebSocket connected from {request.remote_addr}")
    
    # Include the port in the status message
    socketio.emit('status', {
        'message': 'Connected to AmneziaWG Web UI',
        'public_ip': amnezia_manager.public_ip,
        'nginx_port': NGINX_PORT,
        'server_port': request.environ.get('SERVER_PORT', 'unknown'),
        'client_port': request.environ.get('HTTP_X_FORWARDED_PORT', 'unknown')
    })

@socketio.on('disconnect')
def handle_disconnect():
    print(f"WebSocket disconnected from {request.remote_addr}")

if __name__ == '__main__':
    print("AmneziaWG Web UI starting...")
    print("Configuration:")
    print(f"  NGINX Port: {NGINX_PORT}")
    print(f"  Auto-start: {AUTO_START_SERVERS}")
    print(f"  Default MTU: {DEFAULT_MTU}")
    print(f"  Default Subnet: {DEFAULT_SUBNET}")
    print(f"  Default Port: {DEFAULT_PORT}")
    print(f"Detected public IP: {amnezia_manager.public_ip}")

    if AUTO_START_SERVERS:
        print("Auto-starting existing servers...")

    socketio.run(app, host='0.0.0.0', port=WEB_UI_PORT, debug=False, allow_unsafe_werkzeug=True)