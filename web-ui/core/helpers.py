"""Shared utility helpers for configuration, validation and outbound HTTPS."""

import http.client
import ipaddress
import ssl
from typing import NamedTuple
from urllib.parse import urlparse


def sanitize_config_value(value):
    """Make sure config values are single-line to keep config format intact."""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def to_bool(value, default=False):
    """Convert common truthy/falsey representations to a boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "none", "null"):
            return default
        return s not in ("0", "false", "no", "off")
    return bool(value)


def parse_daemon_log_level(raw):
    """amneziawg-go's log level as off | error | debug, or None when unrecognised.

    Empty means off; upstream's "verbose" is the same as "debug" and "silent" is off.
    """
    value = str(raw or "").strip().lower()
    value = {"": "off", "0": "off", "false": "off", "no": "off", "silent": "off", "verbose": "debug"}.get(value, value)
    return value if value in ("off", "error", "debug") else None


def is_valid_ip(ip):
    """True for a dotted-quad IPv4 address.

    Parsed by ipaddress: the old int() check let through "+1.2.3.4" and "1_0.0.0.1".
    """
    try:
        ipaddress.IPv4Address(str(ip))
    except ValueError:
        return False
    return True


class HttpsResponse(NamedTuple):
    status: int
    content_type: str
    text: str


# ipapi.co answers a request without a User-Agent with 429.
USER_AGENT = "amneziawg-web-ui"
MAX_BODY_BYTES = 64 * 1024


def https_get(url, timeout, source_ip=None, headers=None):
    """GET an https:// URL with the standard library.

    The panel's three outbound calls -- the public IP, the egress probe and GeoIP --
    are small GETs, so this replaced `requests` and its four dependencies in 2.5. The
    certificate and host name are checked against the system's store (Alpine's
    ca-certificates, refreshed by every image build), not a bundle frozen at a pin.
    With `source_ip` the connection leaves from that address: the egress probe asks
    from a tunnel's own address. Redirects are not followed; none of the services
    sends one. The body is read up to MAX_BODY_BYTES.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Not an https URL: {url}")
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port or 443,
        timeout=timeout,
        source_address=(source_ip, 0) if source_ip else None,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", path, headers={"User-Agent": USER_AGENT, **(headers or {})})
        response = connection.getresponse()
        body = response.read(MAX_BODY_BYTES)
        return HttpsResponse(response.status, response.getheader("Content-Type", ""), body.decode("utf-8", errors="replace"))
    finally:
        connection.close()
