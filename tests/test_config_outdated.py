"""Tests for outdated-config flags.

The server remembers a fingerprint of the config each device last received (the QR
text) and flags a client when the config it would issue now is different. No edit
route sets the flag, so these tests pin which changes flag whom: every cause is
caught, a revert clears it, and changes that leave the device's config alone flag
nothing.
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.support import PUBLIC_IP, SystemPaths, build_app, build_manager, build_real_manager

TRANSPORT_CHANGE = {"protocol": "AWG 2.0", "S1": 70, "S2": 80, "S3": 30, "S4": 25,
                    "H1": "5000", "H2": "6000", "H3": "7000", "H4": "8000"}  # fmt: skip


def restart(manager):
    """A new manager loaded from `manager`'s files, as after a container restart."""
    return build_manager(
        config_dir=manager.config_dir,
        wireguard_config_dir=manager.wireguard_config_dir,
        config_file=manager.config_file,
    )


class OutdatedFlagTests(unittest.TestCase):
    """Two servers, three issued clients, driven through the HTTP routes."""

    def setUp(self):
        self.paths = SystemPaths().start(self)
        manager, self.fake = build_real_manager(self)
        self.app, self.manager = build_app(manager=manager)
        self.http = self.app.test_client()
        self.home = self._create("home", "10.40.0.0/24", 51940)
        self.office = self._create("office", "10.41.0.0/24", 51941)
        self.phone = self._add(self.home, "phone")
        self.laptop = self._add(self.home, "laptop")
        self.desk = self._add(self.office, "desk")
        for client in (self.phone, self.laptop, self.desk):
            self._issue(client)

    def _create(self, name, subnet, port):
        body = {"name": name, "protocol": "AWG 2.0", "subnet": subnet, "port": port, "auto_start": False}
        response = self.http.post("/api/servers", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def _add(self, server, name):
        return self.http.post(f"/api/servers/{server['id']}/clients", json={"name": name}).get_json()["client"]

    @staticmethod
    def _url(client, *parts):
        return "/".join(("/api/servers", client["server_id"], "clients", client["id"], *parts))

    def _issue(self, client):
        response = self.http.post(self._url(client, "issued"), json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["client"]

    def outdated(self):
        return {c["name"] for c in self.http.get("/api/clients").get_json() if c["config_outdated"]}

    def _refresh_ip(self, address):
        with mock.patch.object(self.manager, "detect_public_ip", return_value=address), \
             mock.patch.object(self.manager, "lookup_geoip", return_value=("Somewhere", "NL")):  # fmt: skip
            self.assertEqual(self.http.post("/api/system/refresh-ip", json={}).status_code, 200)

    def test_issued_clients_are_up_to_date(self):
        self.assertEqual(self.outdated(), set())
        issued = self._issue(self.phone)
        self.assertFalse(issued["config_outdated"])
        self.assertIsInstance(issued["config_issued_at"], float)
        on_list = next(c for c in self.http.get("/api/servers").get_json()[0]["clients"] if c["name"] == "phone")
        self.assertEqual(on_list["config_issued_at"], issued["config_issued_at"])

    def test_a_transport_change_flags_every_client_of_that_server(self):
        response = self.http.post(f"/api/servers/{self.home['id']}/transport-params", json=TRANSPORT_CHANGE)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.outdated(), {"phone", "laptop"})

    def test_a_client_params_change_flags_only_that_client(self):
        # A new server's default Jc is random (4-12), so change it relative to the current one.
        jc = self.manager.get_client(self.phone["id"])["client_params"]["Jc"]
        response = self.http.post(self._url(self.phone, "client-params"), json={"client_params": {"Jc": jc + 1}})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["client"]["config_outdated"])
        self.assertEqual(self.outdated(), {"phone"})

    def test_a_new_public_ip_flags_every_client(self):
        self._refresh_ip(PUBLIC_IP)  # same address: nothing changes
        self.assertEqual(self.outdated(), set())
        self._refresh_ip("198.51.100.20")
        self.assertEqual(self.outdated(), {"phone", "laptop", "desk"})

    def test_changes_that_leave_the_device_config_alone_flag_nothing(self):
        server_url = f"/api/servers/{self.home['id']}"
        calls = [
            (f"{server_url}/rename", {"name": "cabin"}),
            (self._url(self.phone, "rename"), {"name": "tablet"}),
            (self._url(self.phone, "suspend"), {}),
            (self._url(self.laptop, "suspend"), {}),
            (self._url(self.laptop, "suspend"), {}),
            (f"{server_url}/networking", {"enable_nat": False, "block_lan_cidrs": True}),
            (f"{server_url}/start", {}),
            (f"{server_url}/stop", {}),
        ]
        for url, body in calls:
            self.assertEqual(self.http.post(url, json=body).status_code, 200, url)
        self.assertEqual(self.outdated(), set())

    def test_issuing_again_clears_the_flag(self):
        self.http.post(f"/api/servers/{self.home['id']}/transport-params", json=TRANSPORT_CHANGE)
        self.assertFalse(self._issue(self.phone)["config_outdated"])
        self.assertEqual(self.outdated(), {"laptop"})

    def test_reverting_a_change_clears_the_flag(self):
        original = dict(self.manager.get_client(self.phone["id"])["client_params"])
        url = self._url(self.phone, "client-params")
        self.http.post(url, json={"client_params": {**original, "Jc": original["Jc"] + 1}})
        self.assertEqual(self.outdated(), {"phone"})
        self.http.post(url, json={"client_params": original})
        self.assertEqual(self.outdated(), set())

    def test_a_client_never_issued_is_not_flagged(self):
        tablet = self._add(self.home, "tablet")
        self.assertIsNone(tablet["config_issued_at"])
        self.http.post(f"/api/servers/{self.home['id']}/transport-params", json=TRANSPORT_CHANGE)
        self.assertEqual(self.outdated(), {"phone", "laptop"})

    def test_the_fingerprint_is_stored_but_never_served(self):
        stored = json.loads(Path(self.manager.config_file).read_text(encoding="utf-8"))
        fingerprint = stored["servers"][0]["clients"][0]["config_issued_fingerprint"]
        self.assertRegex(fingerprint, r"^[0-9a-f]{16}$")
        for url in ("/api/servers", "/api/clients", f"/api/servers/{self.home['id']}/clients"):
            body = self.http.get(url).get_data(as_text=True)
            self.assertNotIn("config_issued_fingerprint", body, url)
            self.assertNotIn(fingerprint, body, url)


class FingerprintBackfillTests(unittest.TestCase):
    """Clients stored before config tracking are assumed up to date at upgrade."""

    def setUp(self):
        first = build_manager()
        server = first.create_wireguard_server(
            {"name": "home", "protocol": "AWG 2.0", "subnet": "10.40.0.0/24", "port": 51940, "auto_start": False}
        )
        self.server_id = server["id"]
        self.old, _ = first.add_wireguard_client(server["id"], "old")
        self.new, _ = first.add_wireguard_client(server["id"], "new")
        self.config_file = first.config_file
        self.first = first

        # As written by 2.2: the old client has neither field. The new one keeps its
        # explicit None -- created on this version but never handed out.
        data = json.loads(Path(self.config_file).read_text(encoding="utf-8"))
        for client in data["servers"][0]["clients"]:
            if client["id"] == self.old["id"]:
                client.pop("config_issued_fingerprint")
                client.pop("config_issued_at")
        Path(self.config_file).write_text(json.dumps(data), encoding="utf-8")

    def stored_client(self, client_id):
        data = json.loads(Path(self.config_file).read_text(encoding="utf-8"))
        return next(c for c in data["servers"][0]["clients"] if c["id"] == client_id)

    def test_an_old_client_gets_its_current_fingerprint_and_it_is_saved_at_once(self):
        manager = restart(self.first)
        server, old = manager.get_server(self.server_id), manager.get_client(self.old["id"])
        self.assertEqual(old["config_issued_fingerprint"], manager.config_fingerprint(server, old))
        self.assertIsNone(old["config_issued_at"])
        self.assertFalse(manager.is_config_outdated(server, old))
        self.assertEqual(self.stored_client(self.old["id"])["config_issued_fingerprint"], old["config_issued_fingerprint"])

        # Because it was saved, a later public IP change is caught even across a restart.
        server["public_ip"] = "198.51.100.20"
        manager.save_config()
        again = restart(manager)
        self.assertTrue(again.is_config_outdated(again.get_server(self.server_id), again.get_client(self.old["id"])))

    def test_a_never_issued_client_stays_unissued(self):
        manager = restart(self.first)
        self.assertIsNone(manager.get_client(self.new["id"])["config_issued_fingerprint"])
        self.assertIsNone(self.stored_client(self.new["id"])["config_issued_fingerprint"])

    def test_a_load_with_nothing_to_fill_writes_nothing(self):
        restart(self.first)  # fills the old client and saves
        os.utime(self.config_file, ns=(0, 0))
        restart(self.first)
        self.assertEqual(os.stat(self.config_file).st_mtime_ns, 0)


if __name__ == "__main__":
    unittest.main()
