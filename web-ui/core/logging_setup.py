"""Logging configuration.

Everything goes to stdout, which supervisord captures into
/var/log/webui/access.log (and stderr into error.log). Adding timestamps and levels
means those files are usable on their own, without reading the source to work out
what a bare message meant.

Level is set by LOG_LEVEL (default INFO); use DEBUG when chasing a problem.
"""

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level=None):
    """Install a single stdout handler on the root logger. Safe to call twice."""
    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    if resolved not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        resolved = "INFO"

    root = logging.getLogger()
    root.setLevel(resolved)

    # Replace our own handler rather than stacking duplicates on reload.
    for handler in list(root.handlers):
        if getattr(handler, "_amnezia_handler", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler._amnezia_handler = True  # pylint: disable=protected-access
    root.addHandler(handler)

    # These are chatty and drown out our own messages. urllib3 in particular logs
    # every outbound request (public IP detection, GeoIP) at DEBUG.
    for noisy in ("werkzeug", "engineio", "socketio", "urllib3"):
        logging.getLogger(noisy).setLevel("WARNING")

    return root


def get_logger(name):
    """Module-level logger: get_logger(__name__)."""
    return logging.getLogger(name)
