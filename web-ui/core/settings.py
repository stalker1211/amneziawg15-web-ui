"""Panel settings: stored in web_config.json, pinned by environment variables.

One rule for every setting (DEVELOPMENT.md §11, "Settings: one rule"):

  * a set, non-empty environment variable wins, is copied into the stored settings
    at boot ("writes through"), and its field is read-only in the drawer;
  * otherwise the stored value, as last saved in the drawer (or written through);
  * otherwise the built-in default.

Removing a variable therefore keeps its last value, now editable, and the first 2.4
boot copies today's environment, so upgrading changes nothing. An invalid value is
logged and ignored. Deployment-only variables (NGINX_PORT, ALLOWED_ORIGINS,
AWG_LOG_FILE) are not settings and stay in app.py.

The Basic Auth credential is not stored here but in /etc/amnezia/.htpasswd, which
nginx reads directly (see Access).
"""

import grp
import hmac
import ipaddress
import os
import subprocess
import tempfile

from core.helpers import is_valid_ip, parse_daemon_log_level, to_bool
from core.logging_setup import get_logger

logger = get_logger(__name__)

PANEL_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _mtu(raw):
    value = int(str(raw).strip())
    if not 1280 <= value <= 1440:
        raise ValueError(f"MTU must be between 1280 and 1440, got {value}")
    return value


def _port(raw):
    value = int(str(raw).strip())
    if not 1 <= value <= 65535:
        raise ValueError(f"Port must be between 1 and 65535, got {value}")
    return value


def _subnet(raw):
    network = ipaddress.ip_network(str(raw).strip(), strict=False)
    if network.version != 4 or network.prefixlen > 30:
        raise ValueError(f"Subnet must be an IPv4 /30 or larger, got {raw}")
    return str(network)


def _dns(raw):
    servers = [part.strip() for part in (raw if isinstance(raw, list) else str(raw).split(",")) if str(part).strip()]
    if not servers:
        raise ValueError("At least one DNS server is required")
    for server in servers:
        if not is_valid_ip(server):
            raise ValueError(f"Invalid DNS server IP: {server}")
    return ", ".join(servers)


def _flag(raw):
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
        raise ValueError(f"Expected on or off, got {raw!r}")
    return to_bool(text)


def _daemon_level(raw):
    value = parse_daemon_log_level(raw)
    if value is None:
        raise ValueError(f"Daemon log level must be off, error or debug, got {raw!r}")
    return value


def _panel_level(raw):
    value = str(raw).strip().upper()
    if value not in PANEL_LOG_LEVELS:
        raise ValueError(f"Panel log level must be one of {', '.join(PANEL_LOG_LEVELS)}, got {raw!r}")
    return value


# key: (environment variable, parser, built-in default)
FIELDS = {
    "geoip": ("ENABLE_GEOIP", _flag, True),
    "awg_log_level": ("AWG_LOG_LEVEL", _daemon_level, "error"),
    "log_level": ("LOG_LEVEL", _panel_level, "INFO"),
    "default_mtu": ("DEFAULT_MTU", _mtu, 1280),
    "default_subnet": ("DEFAULT_SUBNET", _subnet, "10.0.0.0/24"),
    "default_port": ("DEFAULT_PORT", _port, 51820),
    "default_dns": ("DEFAULT_DNS", _dns, "8.8.8.8, 1.1.1.1"),
    "enable_nat": ("ENABLE_NAT", _flag, True),
    "block_lan_cidrs": ("BLOCK_LAN_CIDRS", _flag, True),
}


class Settings:
    """The resolved values, where each came from, and the one rule for changing them."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        self.values = {key: default for key, (_env, _parse, default) in FIELDS.items()}
        self.sources = dict.fromkeys(FIELDS, "default")

    def env_value(self, key):
        """The variable's value when it is set and non-empty (empty counts as unset)."""
        return str(self.environ.get(FIELDS[key][0]) or "").strip()

    def resolve(self, stored):
        """Resolve every setting against `stored` (the config's `settings` dict), writing
        pinned values through into it. Returns True when `stored` changed."""
        changed = False
        for key, (env, parse, default) in FIELDS.items():
            raw = self.env_value(key)
            if raw:
                try:
                    value = parse(raw)
                except (TypeError, ValueError) as exc:
                    logger.warning("Ignoring %s=%r: %s", env, raw, exc)
                else:
                    self.values[key], self.sources[key] = value, "env"
                    if stored.get(key) != value:
                        stored[key], changed = value, True
                    continue
            if key in stored:
                try:
                    self.values[key], self.sources[key] = parse(stored[key]), "panel"
                    continue
                except (TypeError, ValueError) as exc:
                    logger.warning("Ignoring the stored %s=%r: %s", key, stored[key], exc)
            self.values[key], self.sources[key] = default, "default"
        return changed

    def check(self, changes):
        """Validate a partial update without applying it: (normalized values, errors)."""
        values, errors = {}, []
        for key, raw in changes.items():
            if key not in FIELDS:
                errors.append(f"Unknown setting '{key}'")
                continue
            try:
                value = FIELDS[key][1](raw)
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
                continue
            if self.sources[key] == "env":
                # A pinned field comes back with the form; only a different value is refused.
                if value != self.values[key]:
                    errors.append(f"{key} is set by {FIELDS[key][0]}; remove the variable to change it here")
                continue
            values[key] = value
        return values, errors

    def update(self, stored, changes):
        """Apply a validated partial update to the values and `stored`; returns the changed keys."""
        values, errors = self.check(changes)
        if errors:
            raise ValueError(errors[0])
        changed = [key for key, value in values.items() if value != self.values[key]]
        for key in changed:
            self.values[key] = stored[key] = values[key]
            self.sources[key] = "panel"
        return changed

    def payload(self):
        return {
            "values": dict(self.values),
            "sources": dict(self.sources),
            "env": {key: env for key, (env, _parse, _default) in FIELDS.items()},
        }


class Access:
    """The one Basic Auth credential: /etc/amnezia/.htpasswd, which nginx reads directly.

    scripts/start.sh writes it at boot: from NGINX_PASSWORD when set (and NGINX_USER
    renames it), else it keeps the stored one, else admin/changeme. The drawer changes
    it here, after checking the current password. Hashes are SHA-512 crypt ($6$) from
    `openssl passwd`, which reads the password on stdin, never argv.
    """

    DEFAULT_USER = "admin"
    DEFAULT_PASSWORD = "changeme"

    def __init__(self, htpasswd_path, environ=None):
        self.path = htpasswd_path
        self.environ = os.environ if environ is None else environ
        self._default_checked = (None, False)  # (the file's mtime and size, the answer)

    def _entry(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                user, _, password_hash = f.readline().strip().partition(":")
                return user, password_hash
        except OSError:
            return None, ""

    def user(self):
        return self._entry()[0] or self.DEFAULT_USER

    def pinned(self, variable):
        return bool(str(self.environ.get(variable) or "").strip())

    def is_default(self):
        """True while the password is still `changeme`, whoever set it (NGINX_PASSWORD
        included). Checked by hashing, once per change of the file."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return False
        key = (stat.st_mtime_ns, stat.st_size)
        if self._default_checked[0] != key:
            self._default_checked = (key, self.verify(self.DEFAULT_PASSWORD))
        return self._default_checked[1]

    def payload(self):
        stored = self._entry()[0] is not None
        return {
            "user": self.user(),
            "user_source": "env" if self.pinned("NGINX_USER") else ("panel" if stored else "default"),
            "password_source": "env" if self.pinned("NGINX_PASSWORD") else ("default" if self.is_default() else "panel"),
            "password_is_default": self.is_default(),
            # Nothing to change in the drawer when both come from the environment.
            "editable": not (self.pinned("NGINX_USER") and self.pinned("NGINX_PASSWORD")),
        }

    @staticmethod
    def hash_password(password, salt=None):
        argv = ["openssl", "passwd", "-6", *(["-salt", salt] if salt else []), "-stdin"]
        result = subprocess.run(argv, input=password, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def verify(self, password):
        _user, stored = self._entry()
        parts = stored.split("$")
        if len(parts) < 4 or parts[1] != "6":
            return False
        return hmac.compare_digest(self.hash_password(password, salt=parts[2]), stored)

    @staticmethod
    def check_new(user, password):
        """Errors in a proposed user name or password (they go into a colon-separated file)."""
        errors = []
        if user is not None and (not user or ":" in user or any(c.isspace() for c in user)):
            errors.append("The user name must be non-empty, without spaces or ':'")
        if password is not None and (len(password) < 8 or "\n" in password or "\r" in password):
            errors.append("The new password must be at least 8 characters, on one line")
        return errors

    def change(self, current_password, user=None, password=None):
        """Change the user name and/or password, the current password required."""
        if user is not None and self.pinned("NGINX_USER"):
            raise ValueError("The user name is set by NGINX_USER; remove the variable to change it here")
        if password is not None and self.pinned("NGINX_PASSWORD"):
            raise ValueError("The password is set by NGINX_PASSWORD; remove the variable to change it here")
        errors = self.check_new(user, password)
        if errors:
            raise ValueError(errors[0])
        if not self.verify(current_password or ""):
            raise ValueError("The current password is wrong")
        _old_user, old_hash = self._entry()
        new_hash = self.hash_password(password) if password is not None else old_hash
        self._write(user if user is not None else self.user(), new_hash)

    def _write(self, user, password_hash):
        """Atomically, root:nginx 640: nginx's workers run as nginx (a root-only file is a 500)."""
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(prefix=".htpasswd-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{user}:{password_hash}\n")
            try:
                os.chown(tmp, 0, grp.getgrnam("nginx").gr_gid)
            except (KeyError, PermissionError):
                pass  # outside the container (tests): no nginx group to hand it to
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
