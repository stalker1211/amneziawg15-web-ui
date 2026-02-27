"""Shared utility helpers for configuration and validation."""

def sanitize_config_value(value):
    """Make sure config values are single-line to keep config format intact."""
    return str(value).replace('\r', ' ').replace('\n', ' ').strip()


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
        if s in ('', 'none', 'null'):
            return default
        return s not in ('0', 'false', 'no', 'off')
    return bool(value)


def is_valid_ip(ip):
    """Check if the string is a valid IPv4 address."""
    try:
        parts = str(ip).split('.')
        if len(parts) != 4:
            return False
        for part in parts:
            if not 0 <= int(part) <= 255:
                return False
        return True
    except (TypeError, ValueError):
        return False
