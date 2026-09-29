"""Shared utility helpers for configuration and validation."""

import ipaddress


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
