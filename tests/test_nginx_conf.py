"""Tests for nginx's part of the settings (core/nginx_conf.py): the generated include.

What is pinned: which $awg_log_* variable each panel level picks (the maps live in
config/nginx.conf, test_guards checks those), the trusted proxies as set_real_ip_from,
a rewrite only when the content changes, `nginx -t` before a reload and the old file
put back when it fails, and the boot's file from the stored settings and the
environment by the panel's own rule.
"""

import json
import os
import shutil
import tempfile
import unittest

# pylint: disable=missing-function-docstring,missing-class-docstring,wrong-import-order
from core import nginx_conf


def values(log_level="INFO", trusted_proxies="172.17.0.0/16"):
    return {"geoip": True, "awg_log_level": "error", "log_level": log_level, "trusted_proxies": trusted_proxies}


class RenderTests(unittest.TestCase):
    def test_the_panel_level_picks_the_request_lines(self):
        expected = {"DEBUG": "debug", "INFO": "warning", "WARNING": "warning", "ERROR": "error"}
        for level, gate in expected.items():
            text = nginx_conf.render(values(log_level=level))
            self.assertIn(f"access_log /dev/stdout awg if=$awg_log_{gate};", text, level)
            self.assertIn(f"error_log stderr {'error' if level == 'ERROR' else 'warn'};", text, level)

    def test_trusted_proxies_become_set_real_ip_from(self):
        text = nginx_conf.render(values(trusted_proxies="192.168.1.10, fd00::/64"))
        self.assertIn("set_real_ip_from 192.168.1.10;\nset_real_ip_from fd00::/64;\n", text)
        self.assertNotIn("set_real_ip_from", nginx_conf.render(values(trusted_proxies="")))

    def test_it_says_it_is_generated(self):
        self.assertTrue(nginx_conf.render(values()).startswith("# Generated from the panel's settings"))


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "awg", "settings.conf")
        self.ran = []
        self.answers = {}

    def run_command(self, args):
        self.ran.append(args)
        return self.answers.get(tuple(args), "")

    def test_writes_tests_and_reloads_then_leaves_an_unchanged_file_alone(self):
        self.assertTrue(nginx_conf.write(values(), self.path, self.run_command))
        self.assertEqual(self.ran, [["nginx", "-t"], ["nginx", "-s", "reload"]])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), nginx_conf.render(values()))
        self.ran.clear()
        self.assertFalse(nginx_conf.write(values(), self.path, self.run_command))
        self.assertEqual(self.ran, [])

    def test_a_file_nginx_refuses_is_put_back_and_not_reloaded(self):
        nginx_conf.write(values(), self.path, self.run_command)
        self.ran.clear()
        self.answers[("nginx", "-t")] = None
        with self.assertLogs("core.nginx_conf", "ERROR"):
            self.assertFalse(nginx_conf.write(values(log_level="DEBUG"), self.path, self.run_command))
        self.assertEqual(self.ran, [["nginx", "-t"]])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), nginx_conf.render(values()))

    def test_a_failed_reload_is_logged(self):
        self.answers[("nginx", "-s", "reload")] = None
        with self.assertLogs("core.nginx_conf", "ERROR"):
            self.assertFalse(nginx_conf.write(values(), self.path, self.run_command))


class BootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, "web_config.json")
        self.path = os.path.join(self.tmp, "awg", "settings.conf")

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_stored_settings_and_the_environment_by_the_panels_rule(self):
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"servers": [], "settings": {"log_level": "DEBUG", "trusted_proxies": "10.0.0.1"}}, f)
        nginx_conf.main(self.config, self.path, environ={"TRUSTED_PROXIES": "192.168.1.10"})
        self.assertEqual(self.read(), nginx_conf.render(values(log_level="DEBUG", trusted_proxies="192.168.1.10")))
        with open(self.config, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["settings"]["trusted_proxies"], "10.0.0.1")  # nothing written back

    def test_no_config_yet_means_the_defaults(self):
        nginx_conf.main(os.path.join(self.tmp, "missing.json"), self.path, environ={})
        self.assertEqual(self.read(), nginx_conf.render(values()))

    def test_an_unreadable_config_means_the_defaults_with_a_warning(self):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertLogs("core.nginx_conf", "WARNING"):
            nginx_conf.main(self.config, self.path, environ={})
        self.assertEqual(self.read(), nginx_conf.render(values()))


if __name__ == "__main__":
    unittest.main()
