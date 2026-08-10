"""Tests for logging configuration.

Cheap insurance: a broken LOG_LEVEL must not stop the app booting, and no module
should regress to bare print() (which loses timestamps and levels in the supervisord
logs, exactly what makes them hard to read).
"""

import io
import logging
import os
import re
import unittest

from tests.support import WEB_UI_DIR

BACKEND_FILES = [
    "app.py",
    os.path.join("core", "runtime.py"),
    os.path.join("core", "helpers.py"),
    os.path.join("core", "logging_setup.py"),
    os.path.join("services", "amnezia_manager.py"),
    os.path.join("routes", "servers.py"),
    os.path.join("routes", "system.py"),
]


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        from core.logging_setup import configure_logging  # noqa: PLC0415
        self.configure_logging = configure_logging
        self._saved_level = os.environ.get("LOG_LEVEL")

    def tearDown(self):
        if self._saved_level is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = self._saved_level
        self.configure_logging("INFO")

    def test_default_level_is_info(self):
        os.environ.pop("LOG_LEVEL", None)
        self.assertEqual(self.configure_logging().level, logging.INFO)

    def test_level_from_environment(self):
        os.environ["LOG_LEVEL"] = "debug"
        self.assertEqual(self.configure_logging().level, logging.DEBUG)

    def test_invalid_level_falls_back_to_info(self):
        os.environ["LOG_LEVEL"] = "nonsense"
        self.assertEqual(self.configure_logging().level, logging.INFO)

    def test_repeated_configuration_does_not_stack_handlers(self):
        first = len(self.configure_logging("INFO").handlers)
        self.configure_logging("INFO")
        self.assertEqual(len(self.configure_logging("INFO").handlers), first)

    def test_noisy_third_party_loggers_are_quietened(self):
        self.configure_logging("DEBUG")
        for name in ("werkzeug", "engineio", "socketio", "urllib3"):
            self.assertEqual(logging.getLogger(name).level, logging.WARNING, name)

    def test_records_include_timestamp_level_and_module(self):
        from core.logging_setup import DATE_FORMAT, LOG_FORMAT  # noqa: PLC0415

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        logger = logging.getLogger("tests.sample")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info("hello %s", "world")
        finally:
            logger.removeHandler(handler)

        line = stream.getvalue().strip()
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO +\[tests\.sample\] hello world$")


class NoBarePrintTests(unittest.TestCase):
    """Backend modules must log, not print."""

    def test_no_print_statements_in_backend(self):
        offenders = []
        for relative in BACKEND_FILES:
            path = os.path.join(WEB_UI_DIR, relative)
            for number, line in enumerate(open(path, encoding="utf-8"), 1):
                # Anchored to a statement start so Blueprint(...) does not match.
                if re.match(r"^\s*print\(", line):
                    offenders.append(f"{relative}:{number}")
        self.assertEqual(offenders, [], f"use logger.* instead of print(): {offenders}")

    def test_every_backend_module_that_logs_has_a_module_logger(self):
        for relative in BACKEND_FILES:
            path = os.path.join(WEB_UI_DIR, relative)
            source = open(path, encoding="utf-8").read()
            if "logger." not in source:
                continue
            if relative.endswith("logging_setup.py"):
                continue
            self.assertIn("logger = get_logger(__name__)", source,
                          f"{relative} logs without defining a module logger")


if __name__ == "__main__":
    unittest.main()
