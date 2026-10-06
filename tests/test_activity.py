"""Tests for the Activity events (services/activity.py, DEVELOPMENT.md §3): the
ring, the stdout line, the stream, and the *change* events the panel's actions record.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import (
    HEADER_PROTECTION_KEY,
    FakeEvents,
    SystemPaths,
    build_app,
    build_manager,
    build_real_manager,
)

# One event, the same in the ring, GET /api/activity and the SSE data (the contract).
EVENT_KEYS = {"seq", "ts", "kind", "event", "server_id", "server", "client_id", "client", "detail"}
TS_FORMAT = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        from services.activity import Activity

        tmp = tempfile.mkdtemp(prefix="awg-activity-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = os.path.join(tmp, "events.log")
        self.events = FakeEvents()
        self.activity = Activity(self.events, path=self.path, clock=lambda: 1_790_967_172)
        self.addCleanup(self.activity.close)

    def lines(self):
        return [json.loads(line) for line in Path(self.path).read_text(encoding="utf-8").splitlines()]

    def test_the_ring_keeps_the_newest_thousand(self):
        for _ in range(1003):
            self.activity.change("server.start")
        events = self.activity.payload()["events"]
        self.assertEqual(len(events), 1000)
        self.assertEqual((events[0]["seq"], events[-1]["seq"]), (1003, 4))  # newest first

    def test_an_event_and_its_line(self):
        server, client = {"id": "abc123", "name": "home"}, {"id": "cl1", "name": "iphone"}
        item = self.activity.change("client.params", server=server, client=client, changes=[{"field": "I1"}])
        self.assertEqual(set(item), EVENT_KEYS)
        self.assertEqual(
            item,
            {"seq": 1, "ts": "2026-10-02T18:52:52Z", "kind": "change", "event": "client.params", "server_id": "abc123",
             "server": "home", "client_id": "cl1", "client": "iphone", "detail": {"changes": [{"field": "I1"}]}},
        )  # fmt: skip
        self.activity.record("auth", "auth.fail", detail={"address": "192.0.2.1", "user": None, "count": 3})
        first, second = self.lines()
        # The line is the event plus its source, so Grafana can tell it from supervisord's,
        # and a level, so Grafana shows it like any log.
        self.assertEqual(set(first), EVENT_KEYS | {"src", "level"})
        self.assertEqual(first, {"src": "awg-webui", "level": "info", **item})
        self.assertEqual((second["seq"], second["server_id"], second["client"]), (2, None, None))
        self.assertEqual(second["level"], "warning")
        # The page gets the same object as an SSE `activity` event.
        self.assertEqual(self.events.published[0], ("activity", item))
        self.assertEqual(self.activity.payload()["since"], "2026-10-02T18:52:52Z")

    def test_levels(self):
        # A device that cannot connect is a warning, so a Grafana alert on Loki sees it
        # (2.8, services/probe.py); its recovery and every other event are info.
        for event in ("client.old_config", "client.maybe_blocked", "client.recovered", "client.online"):
            self.activity.record("session", event)
        self.assertEqual([line["level"] for line in self.lines()], ["warning", "warning", "info", "info"])

    def test_names_are_written_as_they_are(self):
        self.activity.change("server.create", server={"id": "a", "name": "Дом"})
        self.assertIn('"server": "Дом"', Path(self.path).read_text(encoding="utf-8"))

    def test_a_failed_write_is_logged_once_and_the_event_kept(self):
        from services.activity import Activity

        activity = Activity(self.events, path=os.path.join(self.path, "no", "such", "dir"))
        with self.assertLogs("services.activity", "WARNING") as logs:
            for _ in range(3):
                activity.change("server.start")
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(activity.payload()["events"]), 3)
        self.assertEqual(len(self.events.published), 3)

    def test_field_changes(self):
        from services.activity import field_changes

        old = {"MTU": 1420, "I1": "<b 0xc0ffee>", "HeaderProtectionKey": "a", "I2": None, "Port": 51820}
        new = {"MTU": 1380, "I1": "<r 16>", "HeaderProtectionKey": "b", "I2": "", "Port": 51820}
        self.assertEqual(
            field_changes(old, new),
            [{"field": "MTU", "old": 1420, "new": 1380}, {"field": "I1"}, {"field": "HeaderProtectionKey"}],
        )
        # The settings show every value; `fields` picks and orders the keys.
        self.assertEqual(
            field_changes({"geoip": True, "x": 1}, {"geoip": False, "x": 2}, fields=["geoip"], values=None),
            [{"field": "geoip", "old": True, "new": False}],
        )


class ChangeEventTests(unittest.TestCase):
    """Every *change* the panel's actions record, through the routes and the real
    start/stop code, in order."""

    I1 = "<b 0xc0ffee>"

    def setUp(self):
        self.paths = SystemPaths().start(self)
        manager, _ = build_real_manager(self)
        self.app, self.manager = build_app(manager=manager)
        http = self.app.test_client()
        self.http = http

        def ok(response):
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()

        server = ok(http.post("/api/servers", json={"name": "s", "protocol": "AWG 3.1", "subnet": "10.71.0.0/24",
                                                     "port": 51971, "auto_start": False}))  # fmt: skip
        url = f"/api/servers/{server['id']}"
        ok(http.post(f"{url}/start", json={}))
        self.paths.interfaces.add(server["interface"])
        ok(http.post(f"{url}/rename", json={"name": "home"}))
        ok(http.post(f"{url}/endpoint-host", json={"endpoint_host": "vpn.example.com"}))
        ok(http.post(f"{url}/networking", json={"enable_nat": False}))
        transport = ok(http.get(f"{url}/info"))["transport_params"]
        self.old_hpk = transport["HeaderProtectionKey"]
        ok(http.post(f"{url}/transport-params", json={**transport, "HeaderProtectionKey": HEADER_PROTECTION_KEY}))
        client = ok(http.post(f"{url}/clients", json={"name": "phone"}))["client"]
        stored = self.manager.get_client(client["id"])
        self.secrets = {self.old_hpk, HEADER_PROTECTION_KEY, stored["client_private_key"], stored["preshared_key"]}
        self.secrets.add(self.manager.get_server(server["id"])["server_private_key"])
        self.old_jc = client["client_params"]["Jc"]
        client_url = f"{url}/clients/{client['id']}"
        ok(http.post(f"{client_url}/rename", json={"name": "iphone"}))
        ok(http.post(f"{client_url}/client-params", json={"client_params": {"Jc": self.old_jc + 1, "I1": self.I1},
                                                            "allowed_ips": "10.0.0.0/8"}))  # fmt: skip
        ok(http.post(f"{client_url}/suspend", json={}))
        ok(http.post(f"{client_url}/suspend", json={}))
        ok(http.delete(client_url, json={}))
        ok(http.post(f"{url}/stop", json={}))
        self.paths.interfaces.discard(server["interface"])
        ok(http.delete(url, json={}))
        self.server = server
        self.body = http.get("/api/activity")
        self.events = list(reversed(self.body.get_json()["events"]))

    def test_the_actions_in_order(self):
        self.assertEqual(
            [(e["event"], e["server"], e["client"]) for e in self.events],
            [
                ("server.create", "s", None),
                ("server.start", "s", None),
                ("server.rename", "home", None),
                ("server.endpoint", "home", None),
                ("server.networking", "home", None),
                ("server.transport", "home", None),
                # A transport change restarts a running server.
                ("server.stop", "home", None),
                ("server.start", "home", None),
                ("client.add", "home", "phone"),
                ("client.rename", "home", "iphone"),
                ("client.params", "home", "iphone"),
                ("client.suspend", "home", "iphone"),
                ("client.resume", "home", "iphone"),
                ("client.delete", "home", "iphone"),
                ("server.stop", "home", None),
                ("server.delete", "home", None),
            ],
        )
        self.assertEqual([e["seq"] for e in self.events], list(range(1, 17)))
        for event in self.events:
            self.assertEqual(set(event), EVENT_KEYS, event["event"])
            self.assertEqual((event["kind"], event["server_id"]), ("change", self.server["id"]))
            self.assertRegex(event["ts"], TS_FORMAT)

    def test_what_changed(self):
        changes = {e["event"]: e["detail"]["changes"] for e in self.events if e["detail"]["changes"]}
        self.assertEqual(
            changes,
            {
                "server.rename": [{"field": "Name", "old": "s", "new": "home"}],
                "server.endpoint": [{"field": "Endpoint host", "old": "", "new": "vpn.example.com"}],
                "server.networking": [{"field": "NAT", "old": True, "new": False}],
                "server.transport": [{"field": "HeaderProtectionKey"}],
                "client.rename": [{"field": "Name", "old": "phone", "new": "iphone"}],
                "client.params": [
                    {"field": "Jc", "old": self.old_jc, "new": self.old_jc + 1},
                    {"field": "I1"},
                    {"field": "AllowedIPs", "old": "0.0.0.0/0", "new": "10.0.0.0/8"},
                ],
            },
        )

    def test_a_save_that_changes_nothing_records_nothing(self):
        server = self.http.post(
            "/api/servers", json={"name": "t", "subnet": "10.72.0.0/24", "port": 51972, "auto_start": False}
        ).get_json()
        before = len(self.manager.activity.payload()["events"])
        self.http.post(f"/api/servers/{server['id']}/rename", json={"name": "t"})
        self.http.post(f"/api/servers/{server['id']}/networking", json={})
        self.assertEqual(len(self.manager.activity.payload()["events"]), before)

    def test_the_stream_and_the_lines_carry_the_same_events(self):
        streamed = [data for name, data in self.manager.events.published if name == "activity"]
        self.assertEqual(streamed, self.events)
        lines = Path(self.manager.activity._path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [json.loads(line) for line in lines], [{"src": "awg-webui", "level": "info", **e} for e in self.events]
        )

    def test_no_secret_in_any_event(self):
        from tests.test_http_api import SECRET_KEYS

        # The fake's `wg genkey` answers one key for every role, so fewer values than roles.
        self.assertTrue(self.secrets and all(len(secret) == 44 for secret in self.secrets))
        for label, text in (
            ("GET /api/activity", self.body.get_data(as_text=True)),
            ("stdout", Path(self.manager.activity._path).read_text(encoding="utf-8")),
        ):
            for secret in self.secrets | {"0xc0ffee"}:
                self.assertNotIn(secret, text, label)
            for field in SECRET_KEYS:
                self.assertNotIn(f'"{field}"', text, label)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class SettingsEventTests(unittest.TestCase):
    def setUp(self):
        from core.settings import Access, Settings

        path = os.path.join(tempfile.mkdtemp(prefix="awg-activity-"), "events.log")
        manager = build_manager(settings=Settings({}), activity_path=path)
        self.addCleanup(manager.activity.close)
        self.access = Access(os.path.join(manager.config_dir, ".htpasswd"), environ={})
        Path(self.access.path).write_text(f"admin:{Access.hash_password('changeme')}\n", encoding="utf-8")
        self.app, self.manager = build_app(manager=manager, access=self.access)
        self.http = self.app.test_client()

    def post(self, body):
        response = self.http.post("/api/settings", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())

    def events(self):
        return list(reversed(self.http.get("/api/activity").get_json()["events"]))

    def test_settings_and_access(self):
        self.post({"settings": {"awg_log_level": "debug", "geoip": True}})
        self.post({"access": {"current_password": "changeme", "password": "n3w-Secret-pw"}})
        self.post({"access": {"current_password": "n3w-Secret-pw", "user": "root"}})
        self.post({"settings": {"awg_log_level": "debug"}})  # unchanged: nothing
        events = self.events()
        self.assertEqual(
            [(e["event"], e["detail"]) for e in events],
            [
                ("settings.save", {"changes": [{"field": "awg_log_level", "old": "error", "new": "debug"}]}),
                ("access.change", {"user_changed": False}),
                ("access.change", {"user_changed": True}),
            ],
        )
        for event in events:
            self.assertEqual(set(event), EVENT_KEYS)
            self.assertEqual((event["kind"], event["server_id"], event["client_id"]), ("change", None, None))
        text = self.http.get("/api/activity").get_data(as_text=True)
        text += Path(self.manager.activity._path).read_text(encoding="utf-8")
        for secret in ("changeme", "n3w-Secret-pw", "root", "$6$"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
