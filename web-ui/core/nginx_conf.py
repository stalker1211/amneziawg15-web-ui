"""nginx's part of the panel settings: a generated include, reloaded on save (2.7.2).

config/nginx.conf includes INCLUDE inside its server block. What it holds comes from two
settings (core/settings.py):

  * log_level -- which requests nginx logs (`access_log ... if=$awg_log_<level>`, the
    maps in nginx.conf): DEBUG every request, INFO and WARNING the failed ones (4xx but
    401, and 5xx), ERROR 5xx; /static/ and /status never. And `error_log stderr`, warn
    or (ERROR) error. The error log *file* stays at warn: services/authlog.py reads
    failed sign-ins from it.
  * trusted_proxies -- `set_real_ip_from` per address: whose X-Forwarded-For names the
    client in the logs and in failed sign-ins.

scripts/start.sh writes it before nginx starts (`python3 -m core.nginx_conf`); a save in
the drawer rewrites it through AmneziaManager.apply_settings, checks it with `nginx -t`
and reloads nginx. Never edit the file: it is overwritten from the settings.
"""

import json
import os
import tempfile

from core.logging_setup import configure_logging, get_logger
from core.settings import Settings

logger = get_logger(__name__)

INCLUDE = "/etc/nginx/awg/settings.conf"
CONFIG_FILE = "/etc/amnezia/web_config.json"

# The panel's level -> the $awg_log_* variable that gates a request line (nginx.conf).
ACCESS_FILTER = {"DEBUG": "debug", "INFO": "warning", "WARNING": "warning", "ERROR": "error"}


def render(values):
    """The include for these settings values (log_level, trusted_proxies)."""
    level = values["log_level"]
    proxies = [p for p in values["trusted_proxies"].split(", ") if p]
    lines = [
        "# Generated from the panel's settings (web-ui/core/nginx_conf.py); edits are overwritten.",
        f"# log_level={level}, trusted_proxies={values['trusted_proxies'] or '(none)'}",
        *(f"set_real_ip_from {proxy};" for proxy in proxies),
        f"access_log /dev/stdout awg if=$awg_log_{ACCESS_FILTER[level]};",
        f"error_log stderr {'error' if level == 'ERROR' else 'warn'};",
    ]
    return "\n".join(lines) + "\n"


def _replace(path, text):
    """Write `text` to `path` atomically, 0644 (nginx's master reads it as root anyway)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write(values, path, run_command):
    """Bring `path` in line with the settings and reload nginx; True when it reloaded.

    Unchanged content is left alone (the boot's apply_settings finds what start.sh
    wrote). A file `nginx -t` refuses is put back as it was, and nginx keeps running on
    the old one: the setting stays saved, and the next save tries again.
    """
    text = render(values)
    old = _read(path)
    if old == text:
        return False
    _replace(path, text)
    # run_command returns stdout, "" here (nginx reports on stderr), or None on failure.
    if run_command(["nginx", "-t"]) is None:
        if old is None:
            os.unlink(path)
        else:
            _replace(path, old)
        logger.error("nginx refused the generated %s; kept the previous one", path)
        return False
    if run_command(["nginx", "-s", "reload"]) is None:
        logger.error("nginx did not reload; %s applies at its next start", path)
        return False
    logger.info("nginx reloaded with %s", text.splitlines()[1].lstrip("# "))
    return True


def main(config_file=CONFIG_FILE, path=INCLUDE, environ=None):
    """At boot, before nginx starts: the include from the stored settings and the
    environment, resolved by the same rule as the panel (nothing written back)."""
    stored = {}
    try:
        with open(config_file, encoding="utf-8") as f:
            stored = dict(json.load(f).get("settings") or {})
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        logger.warning("Could not read the settings from %s (%s); nginx starts on the defaults", config_file, exc)
    settings = Settings(environ)
    settings.resolve(stored)
    _replace(path, render(settings.values))


if __name__ == "__main__":
    configure_logging()
    main()
