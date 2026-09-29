"""Tests for panel settings (core/settings.py, routes/settings.py).

The rule under test: a set, non-empty environment variable pins a setting and writes
it through into web_config.json; otherwise the stored value; otherwise the built-in
default. And the Basic Auth credential: changed only with the current password,
never served, written as nginx needs it.
"""

import json
import logging
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access,wrong-import-order
from core.settings import Access, Settings

from tests.support import build_app, build_manager

HAVE_OPENSSL = shutil.which("openssl") is not None


class ResolveTests(unittest.TestCase):
    def test_env_pins_and_writes_through(self):
        stored = {}
        settings = Settings({"DEFAULT_MTU": "1420", "ENABLE_NAT": "0"})
        self.assertTrue(settings.resolve(stored))
        self.assertEqual(stored, {"default_mtu": 1420, "enable_nat": False})
        self.assertEqual((settings.values["default_mtu"], settings.sources["default_mtu"]), (1420, "env"))
        self.assertEqual(settings.sources["default_port"], "default")
        self.assertFalse(settings.resolve(stored))  # the second boot changes nothing

    def test_a_removed_variable_keeps_its_last_value_now_editable(self):
        stored = {}
        Settings({"AWG_LOG_LEVEL": "debug"}).resolve(stored)
        later = Settings({})
        later.resolve(stored)
        self.assertEqual((later.values["awg_log_level"], later.sources["awg_log_level"]), ("debug", "panel"))
        self.assertEqual(later.update(stored, {"awg_log_level": "error"}), ["awg_log_level"])
        self.assertEqual(stored["awg_log_level"], "error")

    def test_empty_counts_as_unset_and_invalid_is_ignored(self):
        stored = {"default_port": 51900}
        settings = Settings({"AWG_LOG_LEVEL": "", "DEFAULT_PORT": "99999", "LOG_LEVEL": "loud"})
        with self.assertLogs("core.settings", "WARNING") as logs:
            settings.resolve(stored)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual((settings.values["default_port"], settings.sources["default_port"]), (51900, "panel"))
        self.assertEqual((settings.values["awg_log_level"], settings.sources["awg_log_level"]), ("error", "default"))
        self.assertEqual(settings.values["log_level"], "INFO")
        self.assertEqual(stored, {"default_port": 51900})

    def test_upgrading_keeps_todays_environment(self):
        # Production's compose today: the first 2.4 boot copies it all.
        env = {"DEFAULT_MTU": "1280", "ENABLE_NAT": "0", "AWG_LOG_LEVEL": "debug", "NGINX_PORT": "8091"}
        stored = {}
        Settings(env).resolve(stored)
        self.assertEqual(stored, {"default_mtu": 1280, "enable_nat": False, "awg_log_level": "debug"})

    def test_a_pinned_setting_can_come_back_unchanged_but_not_change(self):
        settings = Settings({"DEFAULT_MTU": "1420"})
        settings.resolve({})
        self.assertEqual(settings.check({"default_mtu": "1420"}), ({}, []))
        _, errors = settings.check({"default_mtu": 1300})
        self.assertEqual(errors, ["default_mtu is set by DEFAULT_MTU; remove the variable to change it here"])

    def test_values_are_validated(self):
        settings = Settings({})
        settings.resolve({})
        bad = {"default_mtu": 900, "default_subnet": "10.0.0.0/31", "default_dns": "8.8.8.8, nope", "geoip": "maybe",
               "awg_log_level": "loud", "log_level": "chatty", "default_port": 0, "unknown": 1}  # fmt: skip
        _, errors = settings.check(bad)
        self.assertEqual(len(errors), len(bad))
        values, errors = settings.check({"default_dns": " 1.1.1.1 ,9.9.9.9", "geoip": "off", "awg_log_level": "verbose"})
        self.assertEqual((values, errors), ({"default_dns": "1.1.1.1, 9.9.9.9", "geoip": False, "awg_log_level": "debug"}, []))


@unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
class AccessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="awg-access-")
        self.access = Access(os.path.join(self.dir, ".htpasswd"), os.path.join(self.dir, ".htpasswd.default"), environ={})
        Path(self.access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")
        Path(self.access.marker).touch()

    def test_the_default_credential(self):
        self.assertTrue(self.access.verify("changeme"))
        self.assertFalse(self.access.verify("wrong"))
        self.assertEqual(
            self.access.payload(),
            {"user": "admin", "user_source": "panel", "password_source": "default", "password_is_default": True},
        )

    def test_a_change_needs_the_current_password_and_clears_the_banner(self):
        before = Path(self.access.path).read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "current password is wrong"):
            self.access.change("nope", password="n3w-secret")
        self.assertEqual(Path(self.access.path).read_text(encoding="utf-8"), before)

        self.access.change("changeme", password="n3w-secret")
        self.assertTrue(self.access.verify("n3w-secret"))
        self.assertFalse(self.access.is_default())
        self.assertTrue(Path(self.access.path).read_text(encoding="utf-8").startswith("admin:$6$"))
        self.assertEqual(stat.S_IMODE(os.stat(self.access.path).st_mode), 0o640)

        self.access.change("n3w-secret", user="dmitry")
        self.assertEqual(self.access.user(), "dmitry")
        self.assertTrue(self.access.verify("n3w-secret"))

    def test_rules_for_the_new_credential(self):
        for user, password in (("", None), ("a b", None), ("a:b", None), (None, "short"), (None, "two\nlines!!")):
            with self.assertRaises(ValueError, msg=(user, password)):
                self.access.change("changeme", user=user, password=password)

    def test_an_env_pinned_credential_is_read_only(self):
        pinned = Access(self.access.path, self.access.marker, environ={"NGINX_PASSWORD": "x", "NGINX_USER": "ops"})
        with self.assertRaisesRegex(ValueError, "set by NGINX_PASSWORD"):
            pinned.change("changeme", password="whatever-long")
        with self.assertRaisesRegex(ValueError, "set by NGINX_USER"):
            pinned.change("changeme", user="other")
        self.assertEqual(pinned.payload()["password_source"], "env")


class ManagerSettingsTests(unittest.TestCase):
    def test_resolved_settings_become_the_managers_defaults_and_are_saved(self):
        manager = build_manager(settings=Settings({"DEFAULT_MTU": "1300", "DEFAULT_DNS": "9.9.9.9", "ENABLE_GEOIP": "0"}))
        self.assertEqual((manager.default_mtu, manager.dns_servers, manager.enable_geoip), (1300, ["9.9.9.9"], False))
        self.assertEqual(manager.awg_log_level, "error")  # the new default
        stored = json.loads(Path(manager.config_file).read_text(encoding="utf-8"))["settings"]
        self.assertEqual(stored, {"default_mtu": 1300, "default_dns": "9.9.9.9", "geoip": False})
        server = manager.create_wireguard_server({"name": "x", "auto_start": False})
        self.assertEqual((server["mtu"], server["dns"]), (1300, ["9.9.9.9"]))


@unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
class SettingsRouteTests(unittest.TestCase):
    def setUp(self):
        manager = build_manager(settings=Settings({"DEFAULT_PORT": "51900"}))
        self.access = Access(
            os.path.join(manager.config_dir, ".htpasswd"), os.path.join(manager.config_dir, ".htpasswd.default"), environ={}
        )
        Path(self.access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")
        Path(self.access.marker).touch()
        self.app, self.manager = build_app(manager=manager, access=self.access)
        self.http = self.app.test_client()

    def post(self, body, status=200):
        response = self.http.post("/api/settings", json=body)
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def stored(self):
        return json.loads(Path(self.manager.config_file).read_text(encoding="utf-8")).get("settings", {})

    def test_get_shows_values_sources_and_never_the_password(self):
        payload = self.http.get("/api/settings").get_json()
        self.assertEqual(payload["values"]["default_port"], 51900)
        self.assertEqual(payload["sources"]["default_port"], "env")
        self.assertEqual(payload["env"]["default_port"], "DEFAULT_PORT")
        self.assertTrue(payload["access"]["password_is_default"])
        self.assertEqual(payload["about"]["build_label"], "test")
        body = json.dumps(payload)
        self.assertNotIn("$6$", body)
        self.assertNotIn(Path(self.access.path).read_text(encoding="utf-8").split(":", 1)[1].strip(), body)

    def test_a_change_is_applied_at_once_and_saved(self):
        saved = self.post({"settings": {"default_mtu": 1400, "geoip": False, "log_level": "WARNING"}})
        self.assertEqual(sorted(saved["changed"]), ["default_mtu", "geoip", "log_level"])
        self.assertEqual((self.manager.default_mtu, self.manager.enable_geoip), (1400, False))
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(self.stored()["default_mtu"], 1400)
        logging.getLogger().setLevel(logging.INFO)

    def test_invalid_or_pinned_changes_apply_nothing(self):
        self.post({"settings": {"default_mtu": 1400, "default_port": 1}}, status=400)
        self.post({"settings": {"default_mtu": 5000}}, status=400)
        self.assertEqual(self.manager.default_mtu, 1280)

    def test_a_new_daemon_level_offers_to_restart_the_running_servers(self):
        server = self.manager.create_wireguard_server({"name": "x", "auto_start": False, "port": 51820})
        with mock.patch.object(self.manager, "get_server_status", return_value="running"):
            saved = self.post({"settings": {"awg_log_level": "debug"}})
            self.assertEqual((saved["restart_needed"], self.manager.awg_log_level), (1, "debug"))
            with (
                mock.patch.object(self.manager, "stop_server", return_value=True) as stop,
                mock.patch.object(self.manager, "start_server", return_value=True) as start,
            ):
                result = self.http.post("/api/settings/restart-servers", json={}).get_json()
        self.assertEqual(result, {"restarted": ["x"], "failed": []})
        stop.assert_called_once_with(server["id"])
        start.assert_called_once_with(server["id"])

    def test_a_password_change_rotates_the_session_key(self):
        key = self.app.secret_key
        self.post({"access": {"current_password": "wrong", "password": "n3w-secret"}, "settings": {"geoip": False}}, 400)
        self.assertEqual((self.app.secret_key, self.manager.enable_geoip), (key, True))  # nothing applied

        saved = self.post({"access": {"current_password": "changeme", "password": "n3w-secret"}})
        self.assertTrue(saved["access_changed"])
        self.assertFalse(saved["access"]["password_is_default"])
        self.assertNotEqual(self.app.secret_key, key)
        self.assertTrue(self.access.verify("n3w-secret"))

    def test_the_drawer_is_checked_through_validate(self):
        def validate(body):
            return self.http.post("/api/validate", json=body).get_json()

        self.assertEqual(validate({"settings": {"default_mtu": 1400}}), {"errors": [], "warnings": []})
        self.assertEqual(
            validate({"settings": {"default_mtu": 5000}})["errors"], ["MTU must be between 1280 and 1440, got 5000"]
        )
        self.assertEqual(len(validate({"access": {"password": "short"}})["errors"]), 1)
        self.assertEqual(len(validate({"settings": {"awg_log_level": "debug"}})["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
