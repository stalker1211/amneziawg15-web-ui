"""Tests for the persisted state in web_config.json: legacy migration and the two client stores.

migrate_config_schema() runs on every load and is the only upgrade path for old
installs, so it is checked against a real v1.5.1-era file (tests/fixtures/), which
must render byte-for-byte the same .conf files as a server created today.

Clients are stored twice (servers[].clients[] and the top-level clients{} map).
A freshly added client is one dict referenced from both, so the mirroring code is
only exercised once the config has been through a save/load -- the tests restart
the manager from disk before mutating.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import build_app, build_manager, normalize_conf, read_golden

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Fields both client stores must agree on. `status` is a cached display value and
# `obfuscation_params` is derived; neither is mirrored.
MIRRORED_KEYS = ("name", "server_name", "suspended", "client_ip", "client_public_key", "preshared_key", "client_params")


def restart(manager):
    """A new manager loaded from `manager`'s files, as after a container restart."""
    return build_manager(
        config_dir=manager.config_dir,
        wireguard_config_dir=manager.wireguard_config_dir,
        config_file=manager.config_file,
    )


class LegacyConfigMigrationTests(unittest.TestCase):
    """A web_config.json written by v1.5.1: one obfuscation_params dict, no protocol."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="awg-legacy-")
        config = json.loads((FIXTURES / "web_config-v1.5.1.json").read_text(encoding="utf-8"))
        for server in config["servers"]:
            server["config_path"] = os.path.join(self.tmp, os.path.basename(server["config_path"]))
        self.config_file = os.path.join(self.tmp, "web_config.json")
        Path(self.config_file).write_text(json.dumps(config), encoding="utf-8")
        self.manager = build_manager(config_dir=self.tmp, wireguard_config_dir=self.tmp, config_file=self.config_file)
        self.server = self.manager.get_server("leg151")
        self.client = self.manager.get_client("cli151")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unversioned_protocol_becomes_awg15(self):
        self.assertEqual(self.server["protocol"], "AWG 1.5")

    def test_transport_params_lifted_from_obfuscation_params(self):
        transport = self.server["transport_params"]
        self.assertEqual(
            {key: str(transport[key]) for key in ("S1", "S2", "H1", "H2", "H3", "H4")},
            {"S1": "50", "S2": "60", "H1": "1000", "H2": "2000", "H3": "3000", "H4": "4000"},
        )
        # AWG 1.5 has no S3/S4; J* are per-client and do not belong in transport params.
        for key in ("S3", "S4", "Jc", "Jmin", "Jmax"):
            self.assertNotIn(key, transport)
        self.assertIn("client_defaults", self.server)

    def test_client_params_lifted_into_both_stores(self):
        embedded = self.server["clients"][0]
        for client in (embedded, self.client):
            self.assertEqual(
                {key: client["client_params"][key] for key in ("Jc", "Jmin", "Jmax")},
                {"Jc": 8, "Jmin": 40, "Jmax": 70},
            )
        self.assertEqual(embedded["client_params"], self.client["client_params"])

    def test_server_conf_matches_a_server_created_today(self):
        self.manager.write_server_conf(self.server)
        text = Path(self.server["config_path"]).read_text(encoding="utf-8")
        self.assertEqual(normalize_conf(text), read_golden("server-awg15.conf"))

    def test_client_conf_matches_a_client_created_today(self):
        text = self.manager.generate_wireguard_client_config(self.server, self.client, include_comments=True)
        self.assertEqual(normalize_conf(text), read_golden("client-awg15.conf"))

    def test_migration_is_stable_across_save_and_load(self):
        self.manager.save_config()
        self.assertEqual(restart(self.manager).config, self.manager.config)

    def test_malformed_or_partial_files_load_as_empty_state(self):
        for content in ("[]", "{}", '{"servers": []}'):
            Path(self.config_file).write_text(content, encoding="utf-8")
            manager = build_manager(config_dir=self.tmp, wireguard_config_dir=self.tmp, config_file=self.config_file)
            self.assertEqual(manager.config, {"servers": [], "clients": {}}, content)


class ClientStoreConsistencyTests(unittest.TestCase):
    """Every mutation route must leave servers[].clients[] and clients{} in agreement."""

    def setUp(self):
        first = build_manager()
        self.server = self._create_server(first, "home", "10.40.0.0/24", 51940)
        self.phone, _ = first.add_wireguard_client(self.server["id"], "phone")
        self.laptop, _ = first.add_wireguard_client(self.server["id"], "laptop")

        self.manager = restart(first)
        self.app, _ = build_app(manager=self.manager)
        self.http = self.app.test_client()
        # The point of restarting: the two stores are now separate objects.
        embedded = self.manager.get_server(self.server["id"])["clients"][0]
        self.assertIsNot(embedded, self.manager.config["clients"][embedded["id"]])

    @staticmethod
    def _create_server(manager, name, subnet, port):
        return manager.create_wireguard_server(
            {"name": name, "protocol": "AWG 2.0", "subnet": subnet, "port": port, "auto_start": False}
        )

    def _url(self, *parts):
        return "/".join(("/api/servers", self.server["id"], *parts))

    def assert_consistent(self):
        for manager in (self.manager, restart(self.manager)):  # in memory and as persisted
            embedded = {c["id"]: (s["id"], c) for s in manager.config["servers"] for c in s["clients"]}
            self.assertEqual(set(embedded), set(manager.config["clients"]))
            for client_id, (server_id, client) in embedded.items():
                stored = manager.config["clients"][client_id]
                self.assertEqual(stored["server_id"], server_id)
                for key in MIRRORED_KEYS:
                    self.assertEqual(client.get(key), stored.get(key), f"{client_id}.{key}")

    def _conf(self):
        return Path(self.manager.get_server(self.server["id"])["config_path"]).read_text(encoding="utf-8")

    @staticmethod
    def _peer(client):
        # The stub hands every client the same key pair; the address is what differs.
        return f"AllowedIPs = {client['client_ip']}/32"

    def test_rename_client(self):
        response = self.http.post(self._url("clients", self.phone["id"], "rename"), json={"name": "tablet"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.get_client(self.phone["id"])["name"], "tablet")
        self.assertIn("# Client: tablet", self._conf())
        self.assert_consistent()

    def test_rename_server_updates_every_client(self):
        response = self.http.post(self._url("rename"), json={"name": "cabin"})
        self.assertEqual(response.status_code, 200)
        for client_id in (self.phone["id"], self.laptop["id"]):
            self.assertEqual(self.manager.get_client(client_id)["server_name"], "cabin")
        self.assert_consistent()

    def test_suspend_and_reactivate(self):
        url = self._url("clients", self.phone["id"], "suspend")
        self.assertTrue(self.http.post(url, json={}).get_json()["suspended"])
        self.assertNotIn(self._peer(self.phone), self._conf())
        self.assertIn(self._peer(self.laptop), self._conf())
        self.assert_consistent()

        self.assertFalse(self.http.post(url, json={}).get_json()["suspended"])
        self.assertIn(self._peer(self.phone), self._conf())
        self.assert_consistent()

    def test_update_client_params(self):
        params = {"Jc": 5, "Jmin": 30, "Jmax": 60}
        response = self.http.post(self._url("clients", self.phone["id"], "client-params"), json={"client_params": params})
        self.assertEqual(response.status_code, 200)
        stored = self.manager.get_client(self.phone["id"])["client_params"]
        self.assertEqual({key: stored[key] for key in params}, params)
        self.assert_consistent()

    def test_update_transport_params(self):
        payload = {"protocol": "AWG 2.0", "S1": 70, "S2": 80, "S3": 30, "S4": 25,
                   "H1": "5000", "H2": "6000", "H3": "7000", "H4": "8000"}  # fmt: skip
        response = self.http.post(self._url("transport-params"), json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn("S1 = 70", self._conf())
        self.assert_consistent()

    def test_delete_client(self):
        response = self.http.delete(self._url("clients", self.phone["id"]), json={})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.manager.get_client(self.phone["id"]))
        self.assertIsNotNone(self.manager.get_client(self.laptop["id"]))
        self.assertNotIn(self._peer(self.phone), self._conf())
        self.assertIn(self._peer(self.laptop), self._conf())
        self.assert_consistent()

    def test_delete_server_removes_only_its_clients(self):
        other = self._create_server(self.manager, "office", "10.41.0.0/24", 51941)
        desk, _ = self.manager.add_wireguard_client(other["id"], "desk")

        response = self.http.delete(self._url(), json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.manager.config["clients"]), {desk["id"]})
        self.assertFalse(os.path.exists(self.server["config_path"]))
        self.assert_consistent()

    def test_client_ids_are_scoped_to_their_server(self):
        other = self._create_server(self.manager, "office", "10.41.0.0/24", 51941)
        foreign = "/".join(("/api/servers", other["id"], "clients", self.phone["id"]))

        self.assertEqual(self.http.post(foreign + "/rename", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.http.post(foreign + "/suspend", json={}).status_code, 404)
        self.assertEqual(self.http.post(foreign + "/client-params", json={"client_params": {"Jc": 5}}).status_code, 404)
        self.assertEqual(self.http.delete(foreign, json={}).status_code, 404)

        phone = self.manager.get_client(self.phone["id"])
        self.assertEqual((phone["name"], phone["suspended"]), ("phone", False))
        self.assert_consistent()


if __name__ == "__main__":
    unittest.main()
