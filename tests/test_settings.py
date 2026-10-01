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
        settings = Settings({"LOG_LEVEL": "warning", "ENABLE_GEOIP": "0"})
        self.assertTrue(settings.resolve(stored))
        self.assertEqual(stored, {"log_level": "WARNING", "geoip": False})
        self.assertEqual((settings.values["log_level"], settings.sources["log_level"]), ("WARNING", "env"))
        self.assertEqual(settings.sources["awg_log_level"], "default")
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
        stored = {"geoip": False}
        settings = Settings({"AWG_LOG_LEVEL": "", "ENABLE_GEOIP": "maybe", "LOG_LEVEL": "loud"})
        with self.assertLogs("core.settings", "WARNING") as logs:
            settings.resolve(stored)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual((settings.values["geoip"], settings.sources["geoip"]), (False, "panel"))
        self.assertEqual((settings.values["awg_log_level"], settings.sources["awg_log_level"]), ("error", "default"))
        self.assertEqual(settings.values["log_level"], "INFO")
        self.assertEqual(stored, {"geoip": False})

    def test_upgrading_keeps_todays_environment(self):
        # Production's compose before 2.4: the first 2.4 boot copied it all.
        env = {"AWG_LOG_LEVEL": "debug", "LOG_LEVEL": "DEBUG", "NGINX_PORT": "8091"}
        stored = {}
        Settings(env).resolve(stored)
        self.assertEqual(stored, {"awg_log_level": "debug", "log_level": "DEBUG"})

    def test_retired_settings_leave_the_store(self):
        # Production's store at 2.4: two new-server defaults (gone in 2.5) beside a live one.
        stored = {"default_mtu": 1280, "enable_nat": False, "geoip": True}
        settings = Settings({"DEFAULT_MTU": "1300"})
        with self.assertLogs("core.settings", "INFO") as logs:
            self.assertTrue(settings.resolve(stored))
        self.assertEqual(stored, {"geoip": True})
        self.assertIn("Dropped retired settings: default_mtu, enable_nat", logs.output[0])
        self.assertNotIn("default_mtu", settings.values)
        self.assertFalse(settings.resolve(stored))

    def test_a_pinned_setting_can_come_back_unchanged_but_not_change(self):
        settings = Settings({"LOG_LEVEL": "WARNING"})
        settings.resolve({})
        self.assertEqual(settings.check({"log_level": "warning"}), ({}, []))
        _, errors = settings.check({"log_level": "DEBUG"})
        self.assertEqual(errors, ["log_level is set by LOG_LEVEL; remove the variable to change it here"])

    def test_values_are_validated(self):
        settings = Settings({})
        settings.resolve({})
        bad = {"geoip": "maybe", "awg_log_level": "loud", "log_level": "chatty", "default_mtu": 1420, "unknown": 1}
        _, errors = settings.check(bad)
        self.assertEqual(len(errors), len(bad))
        self.assertIn("Unknown setting 'default_mtu'", errors)
        values, errors = settings.check({"geoip": "off", "awg_log_level": "verbose", "log_level": " info "})
        self.assertEqual((values, errors), ({"geoip": False, "awg_log_level": "debug", "log_level": "INFO"}, []))


@unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
class RetiredVariableTests(unittest.TestCase):
    def test_only_a_set_retired_variable_is_reported(self):
        from core.settings import retired_variables

        found = retired_variables({"AUTO_START_SERVERS": "false", "API_TOKEN": " ", "NGINX_PORT": "8080"})
        self.assertEqual([name for name, _note in found], ["AUTO_START_SERVERS"])
        self.assertEqual(retired_variables({}), [])

    def test_the_new_server_defaults_are_retired(self):
        from core.settings import retired_variables

        six = ("DEFAULT_MTU", "DEFAULT_SUBNET", "DEFAULT_PORT", "DEFAULT_DNS", "ENABLE_NAT", "BLOCK_LAN_CIDRS")
        found = retired_variables(dict.fromkeys(six, "1"))
        self.assertEqual([name for name, _note in found], list(six))
        self.assertIn("newest server", found[0][1])


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="awg-access-")
        self.access = Access(os.path.join(self.dir, ".htpasswd"), environ={})
        Path(self.access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")

    def test_the_default_credential(self):
        self.assertTrue(self.access.verify("changeme"))
        self.assertFalse(self.access.verify("wrong"))
        self.assertEqual(
            self.access.payload(),
            {
                "user": "admin",
                "user_source": "panel",
                "password_source": "default",
                "password_is_default": True,
                "editable": True,
            },
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
        pinned = Access(self.access.path, environ={"NGINX_PASSWORD": "x", "NGINX_USER": "ops"})
        with self.assertRaisesRegex(ValueError, "set by NGINX_PASSWORD"):
            pinned.change("changeme", password="whatever-long")
        with self.assertRaisesRegex(ValueError, "set by NGINX_USER"):
            pinned.change("changeme", user="other")
        self.assertEqual(pinned.payload()["password_source"], "env")
        # Pinned to changeme it is still the default: the banner must not go away.
        self.assertTrue(pinned.is_default())
        self.assertFalse(pinned.payload()["editable"])


class ManagerSettingsTests(unittest.TestCase):
    def test_resolved_settings_reach_the_manager_and_are_saved(self):
        manager = build_manager(settings=Settings({"ENABLE_GEOIP": "0"}))
        self.assertEqual((manager.netinfo.enable_geoip, manager.awg_log_level), (False, "error"))  # error: the default
        stored = json.loads(Path(manager.config_file).read_text(encoding="utf-8"))["settings"]
        self.assertEqual(stored, {"geoip": False})

    def test_a_stored_new_server_default_no_longer_applies(self):
        # A 2.4 store with MTU 1300 and NAT off: dropped at the first boot, and a server
        # created through the API without those fields gets the built-in values.
        tmp = tempfile.mkdtemp(prefix="awg-settings-")
        config_file = os.path.join(tmp, "web_config.json")
        settings = {"default_mtu": 1300, "enable_nat": False, "geoip": True}
        Path(config_file).write_text(json.dumps({"servers": [], "settings": settings}), encoding="utf-8")
        manager = build_manager(settings=Settings({}), config_dir=tmp, wireguard_config_dir=tmp, config_file=config_file)
        self.assertEqual(json.loads(Path(config_file).read_text(encoding="utf-8"))["settings"], {"geoip": True})
        server = manager.create_wireguard_server({"name": "x", "auto_start": False})
        self.assertEqual((server["mtu"], server["enable_nat"]), (1420, True))  # build_manager's built-in values


@unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
class SettingsRouteTests(unittest.TestCase):
    def setUp(self):
        manager = build_manager(settings=Settings({"ENABLE_GEOIP": "1"}))
        self.access = Access(os.path.join(manager.config_dir, ".htpasswd"), environ={})
        Path(self.access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")
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
        self.assertEqual(sorted(payload["values"]), ["awg_log_level", "geoip", "log_level"])
        self.assertEqual((payload["values"]["geoip"], payload["sources"]["geoip"]), (True, "env"))
        self.assertEqual(payload["env"]["geoip"], "ENABLE_GEOIP")
        self.assertTrue(payload["access"]["password_is_default"])
        self.assertEqual(payload["about"]["build_label"], "test")
        body = json.dumps(payload)
        self.assertNotIn("$6$", body)
        self.assertNotIn(Path(self.access.path).read_text(encoding="utf-8").split(":", 1)[1].strip(), body)

    def test_a_change_is_applied_at_once_and_saved(self):
        self.addCleanup(logging.getLogger().setLevel, logging.getLogger().level)
        saved = self.post({"settings": {"awg_log_level": "debug", "log_level": "WARNING"}})
        self.assertEqual(sorted(saved["changed"]), ["awg_log_level", "log_level"])
        self.assertEqual(self.manager.awg_log_level, "debug")
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(self.stored()["log_level"], "WARNING")

    def test_invalid_or_pinned_changes_apply_nothing(self):
        self.post({"settings": {"log_level": "WARNING", "geoip": False}}, status=400)  # geoip is pinned
        self.post({"settings": {"log_level": "loud"}}, status=400)
        self.post({"settings": {"default_mtu": 1400}}, status=400)  # retired in 2.5
        self.assertEqual(self.manager.settings.values["log_level"], "INFO")

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

    def test_a_password_change_with_a_wrong_current_password_applies_nothing(self):
        body = {"access": {"current_password": "wrong", "password": "n3w-secret"}, "settings": {"awg_log_level": "debug"}}
        self.post(body, 400)
        self.assertEqual(self.manager.awg_log_level, "error")  # not even the setting sent with it
        self.assertFalse(self.access.verify("n3w-secret"))

        saved = self.post({"access": {"current_password": "changeme", "password": "n3w-secret"}})
        self.assertTrue(saved["access_changed"])
        self.assertFalse(saved["access"]["password_is_default"])
        self.assertTrue(self.access.verify("n3w-secret"))

    def test_the_drawer_is_checked_through_validate(self):
        def validate(body):
            return self.http.post("/api/validate", json=body).get_json()

        self.assertEqual(validate({"settings": {"log_level": "DEBUG"}}), {"errors": [], "warnings": []})
        self.assertEqual(
            validate({"settings": {"log_level": "loud"}})["errors"],
            ["Panel log level must be one of DEBUG, INFO, WARNING, ERROR, got 'loud'"],
        )
        self.assertEqual(len(validate({"access": {"password": "short"}})["errors"]), 1)
        self.assertEqual(len(validate({"settings": {"awg_log_level": "debug"}})["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
