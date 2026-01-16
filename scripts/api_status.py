#!/usr/bin/env python3
"""AmneziaWG Web UI status viewer."""

import argparse
import os
import sys
import requests
from requests.auth import HTTPBasicAuth


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GRAY = "\033[90m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


def _colorize(text, *styles, enabled):
    """Colorize text only when TTY is available."""
    return f"{''.join(styles)}{text}{_Ansi.RESET}" if enabled and styles else text


def _status_color(status):
    """Map status string to color."""
    s = (status or "").lower()
    if any(w in s for w in ("inactive", "stop", "stopped", "down", "dead", "failed", "error", "exited")):
        return _Ansi.RED
    if any(w in s for w in ("run", "running", "active", "up", "online")):
        return _Ansi.GREEN
    return _Ansi.YELLOW


def _handshake_color(handshake):
    """Color for handshake age."""
    s = (handshake or "").lower()
    return _Ansi.RED if "never" in s else (_Ansi.DIM if not s or s == "-" else _Ansi.YELLOW)


def _http_session(user, password, token):
    """Create session with optional basic auth and API token."""
    s = requests.Session()
    if user and password:
        s.auth = HTTPBasicAuth(user, password)
    if token:
        s.headers["X-API-Token"] = token  # avoids clobbering Basic Auth
    return s


def _get_json(s, url, timeout):
    """Fetch JSON, returning (payload, error)."""
    try:
        resp = s.get(url, timeout=timeout)
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        return resp.json(), None
    except (requests.RequestException, ValueError) as e:
        return None, f"invalid response: {e}"


def _val(v):
    """Normalize None to '-' for display."""
    return "-" if v is None else str(v)


def _compact_transfer(v):
    """Make traffic fields shorter."""
    return _val(v).replace(" received", "").replace(" sent", "")


def _print_client(client, traffic, color_enabled):
    """Render a single client block."""
    cid = _val(client.get("id"))
    tinfo = traffic.get(cid, {}) if isinstance(traffic, dict) else {}

    status_value = _val(client.get("status"))
    name = _colorize(_val(client.get("name")), _Ansi.BOLD, _Ansi.YELLOW, enabled=color_enabled)
    status = _colorize(status_value, _status_color(status_value), _Ansi.BOLD, enabled=color_enabled)
    print(f"\t{name} ({cid}) : {status}")

    endpoint = _colorize(_val(tinfo.get("endpoint")), _Ansi.MAGENTA, enabled=color_enabled)
    g, c = _val(tinfo.get("geo")), _val(tinfo.get("geo_country_code"))
    geo = f"{c} / {g}" if c != "-" and g != "-" else (c if c != "-" else g)
    geo_str = _colorize(f" ({geo})", _Ansi.GRAY, enabled=color_enabled) if geo != "-" else ""
    print(f"\t  ip = {client.get('client_ip', '-')}  endpoint = {endpoint}{geo_str}")

    hs_val = _val(tinfo.get("latest_handshake"))
    hs = _colorize(hs_val, _handshake_color(hs_val), enabled=color_enabled)
    rx = _colorize(_compact_transfer(tinfo.get("received")), _Ansi.BLUE, enabled=color_enabled)
    tx = _colorize(_compact_transfer(tinfo.get("sent")), _Ansi.BLUE, enabled=color_enabled)
    print(f"\t  last handshake: {hs}  rx = {rx}  tx = {tx}")


def main():
    p = argparse.ArgumentParser(description="Show AmneziaWG Web UI server/client status")
    p.add_argument("--base-url", required=True, help="Base URL (e.g. http://192.168.1.3:8080)")
    p.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout (default: 5s)")
    p.add_argument("--user", default=os.getenv("AMNEZIA_API_USER"), help="Basic auth user (or AMNEZIA_API_USER)")
    p.add_argument("--password", default=os.getenv("AMNEZIA_API_PASSWORD"), help="Basic auth password (or AMNEZIA_API_PASSWORD)")
    p.add_argument("--token", default=os.getenv("AMNEZIA_API_TOKEN"), help="Bearer token (or AMNEZIA_API_TOKEN)")
    args = p.parse_args()

    color_enabled = sys.stdout.isatty()
    s = _http_session(args.user, args.password, args.token)

    base = args.base_url.rstrip("/")
    servers, err = _get_json(s, f"{base}/api/servers", args.timeout)
    if err:
        print(f"Failed to fetch servers: {err}", file=sys.stderr)
        return 2

    if not isinstance(servers, list):
        print(f"Unexpected /api/servers payload: {type(servers)}", file=sys.stderr)
        return 2

    if not servers:
        print("No servers")
        return 0

    for server in servers:
        if not isinstance(server, dict):
            continue

        sid = server.get("id")
        sname = _colorize(server.get("name", "-"), _Ansi.BOLD, _Ansi.CYAN, enabled=color_enabled)
        sstatus = _colorize(server.get("status", "-"), _status_color(server.get("status", "")), _Ansi.BOLD, enabled=color_enabled)
        print(f"{sname} ({sid}) : {sstatus}  [{server.get('public_ip', '-')}:{server.get('port', '-')}]")

        clients, err = _get_json(s, f"{base}/api/servers/{sid}/clients", args.timeout)
        if err:
            print(f"\t<error: {err}>")
            continue

        traffic, _ = _get_json(s, f"{base}/api/servers/{sid}/traffic", args.timeout)
        
        if not clients:
            print("\t<no clients>")
            continue

        for client in clients:
            if isinstance(client, dict):
                _print_client(client, traffic or {}, color_enabled)

        print("")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
