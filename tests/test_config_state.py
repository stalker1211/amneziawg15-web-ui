"""Tests for the persisted state in web_config.json: migrations and the client store.

migrate_config_schema() runs on every load and is the only upgrade path for old
installs, so it is checked against a real v1.5.1-era file (tests/fixtures/), which
must render byte-for-byte the same .conf files as a server created today, and
against a v2.1-era file, which stored every client twice.

Since v2.2 each client lives only in its server's `clients` list. save_config()
still writes the top-level `clients` map v2.1 reads, derived from those lists, so
rolling the image back keeps every client; the mutation tests check that map after
each change, because it is what a rollback would load.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import build_app, build_manager, normalize_conf, read_golden

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def restart(manager):
    """A new manager loaded from `manager`'s files, as after a container restart."""
    return build_manager(
        config_dir=manager.config_dir,
        wireguard_config_dir=manager.wireguard_config_dir,
        config_file=manager.config_file,
    )


def saved(manager):
    return json.loads(Path(manager.config_file).read_text(encoding="utf-8"))


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

    def test_client_params_lifted(self):
        self.assertIs(self.client, self.server["clients"][0])
        expected = {"Jc": 8, "Jmin": 40, "Jmax": 70}
        self.assertEqual({key: self.client["client_params"][key] for key in expected}, expected)
        self.manager.save_config()
        rollback = saved(self.manager)["clients"]["cli151"]
        self.assertEqual({key: rollback["client_params"][key] for key in expected}, expected)

    def test_server_conf_matches_a_server_created_today(self):
        self.manager.write_server_conf(self.server)
        text = Path(self.server["config_path"]).read_text(encoding="utf-8")
        self.assertEqual(normalize_conf(text), read_golden("server-awg15.conf"))

    def test_client_conf_matches_a_client_created_today(self):
        text = self.manager.generate_wireguard_client_config(self.server, self.client, include_comments=True)
        self.assertEqual(normalize_conf(text), read_golden("client-awg15.conf"))

    def test_legacy_obfuscation_dicts_are_not_kept(self):
        self.manager.save_config()
        self.assertNotIn("obfuscation", Path(self.config_file).read_text(encoding="utf-8"))

    def test_migration_is_stable_across_save_and_load(self):
        self.manager.save_config()
        self.assertEqual(restart(self.manager).config, self.manager.config)

    def test_malformed_or_partial_files_load_as_empty_state(self):
        for content in ("[]", "{}", '{"servers": []}', '{"servers": [], "clients": {}}'):
            Path(self.config_file).write_text(content, encoding="utf-8")
            manager = build_manager(config_dir=self.tmp, wireguard_config_dir=self.tmp, config_file=self.config_file)
            self.assertEqual(manager.config, {"servers": []}, content)


class TwoStoreMigrationTests(unittest.TestCase):
    """A v2.1 file: clients in each server's list *and* in a top-level map."""

    def setUp(self):
        first = build_manager()
        self.home = first.create_wireguard_server(
            {"name": "home", "protocol": "AWG 2.0", "subnet": "10.42.0.0/24", "port": 51942, "auto_start": False}
        )
        self.phone, _ = first.add_wireguard_client(self.home["id"], "phone")
        self.first = first

    def _load(self, edit):
        data = saved(self.first)
        edit(data)
        Path(self.first.config_file).write_text(json.dumps(data), encoding="utf-8")
        return restart(self.first)

    def test_each_client_is_held_once_and_the_map_is_gone_from_memory(self):
        manager = self._load(lambda data: None)
        self.assertNotIn("clients", manager.config)
        self.assertEqual([c["id"] for c in manager.get_client_configs()], [self.phone["id"]])
        self.assertIs(manager.get_client(self.phone["id"]), manager.get_server(self.home["id"])["clients"][0])
        self.assertNotIn("server_name", manager.get_client(self.phone["id"]))

    def test_a_client_only_in_the_map_joins_its_servers_list(self):
        def orphan_from_list(data):
            data["servers"][0]["clients"] = []

        manager = self._load(orphan_from_list)
        self.assertEqual([c["id"] for c in manager.get_server(self.home["id"])["clients"]], [self.phone["id"]])
        # And it still owns its address.
        self.assertNotEqual(manager.get_client_ip(manager.get_server(self.home["id"])), self.phone["client_ip"])

    def test_a_client_whose_server_is_gone_is_dropped(self):
        def dangling(data):
            data["clients"]["ghost"] = {"id": "ghost", "server_id": "gone", "client_ip": "10.99.0.2"}

        with self.assertLogs("services.amnezia_manager", "WARNING") as logs:
            manager = self._load(dangling)
        self.assertIsNone(manager.get_client("ghost"))
        self.assertIn("ghost", "\n".join(logs.output))

    def test_the_embedded_copy_wins_when_they_disagree(self):
        def diverge(data):
            data["clients"][self.phone["id"]]["name"] = "stale-map-name"

        manager = self._load(diverge)
        self.assertEqual(manager.get_client(self.phone["id"])["name"], "phone")


class ClientMutationTests(unittest.TestCase):
    """Every mutation route updates the one store, and the rollback map follows it."""

    def setUp(self):
        first = build_manager()
        self.server = self._create_server(first, "home", "10.40.0.0/24", 51940)
        self.phone, _ = first.add_wireguard_client(self.server["id"], "phone")
        self.laptop, _ = first.add_wireguard_client(self.server["id"], "laptop")

        # Mutate a manager loaded from disk, as the running app does.
        self.manager = restart(first)
        self.app, _ = build_app(manager=self.manager)
        self.http = self.app.test_client()

    @staticmethod
    def _create_server(manager, name, subnet, port):
        return manager.create_wireguard_server(
            {"name": name, "protocol": "AWG 2.0", "subnet": subnet, "port": port, "auto_start": False}
        )

    def _url(self, *parts):
        return "/".join(("/api/servers", self.server["id"], *parts))

    def assert_rollback_map_matches(self):
        """The saved `clients` map is exactly what v2.1 expects: every client, once, with
        its server's id and current name -- nothing stale, nothing missing."""
        data = saved(self.manager)
        expected = {
            client["id"]: {**client, "server_id": server["id"], "server_name": server["name"]}
            for server in data["servers"]
            for client in server["clients"]
        }
        self.assertEqual(data["clients"], expected)
        self.assertEqual(
            {c["id"] for c in restart(self.manager).get_client_configs()},
            {c["id"] for c in self.manager.get_client_configs()},
        )

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
        self.assert_rollback_map_matches()

    def test_rename_server_is_seen_by_every_client(self):
        response = self.http.post(self._url("rename"), json={"name": "cabin"})
        self.assertEqual(response.status_code, 200)
        listed = self.http.get(self._url("clients")).get_json()
        self.assertEqual({c["server_name"] for c in listed}, {"cabin"})
        self.assertEqual({c["server_name"] for c in saved(self.manager)["clients"].values()}, {"cabin"})
        self.assert_rollback_map_matches()

    def test_suspend_and_reactivate(self):
        url = self._url("clients", self.phone["id"], "suspend")
        self.assertTrue(self.http.post(url, json={}).get_json()["suspended"])
        self.assertNotIn(self._peer(self.phone), self._conf())
        self.assertIn(self._peer(self.laptop), self._conf())
        self.assert_rollback_map_matches()

        self.assertFalse(self.http.post(url, json={}).get_json()["suspended"])
        self.assertIn(self._peer(self.phone), self._conf())
        self.assert_rollback_map_matches()

    def test_update_client_params(self):
        params = {"Jc": 5, "Jmin": 30, "Jmax": 60}
        response = self.http.post(self._url("clients", self.phone["id"], "client-params"), json={"client_params": params})
        self.assertEqual(response.status_code, 200)
        stored = self.manager.get_client(self.phone["id"])["client_params"]
        self.assertEqual({key: stored[key] for key in params}, params)
        self.assert_rollback_map_matches()

    def test_update_transport_params(self):
        payload = {"protocol": "AWG 2.0", "S1": 70, "S2": 80, "S3": 30, "S4": 25,
                   "H1": "5000", "H2": "6000", "H3": "7000", "H4": "8000"}  # fmt: skip
        response = self.http.post(self._url("transport-params"), json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn("S1 = 70", self._conf())
        self.assert_rollback_map_matches()

    def test_delete_client(self):
        response = self.http.delete(self._url("clients", self.phone["id"]), json={})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.manager.get_client(self.phone["id"]))
        self.assertIsNotNone(self.manager.get_client(self.laptop["id"]))
        self.assertNotIn(self._peer(self.phone), self._conf())
        self.assertIn(self._peer(self.laptop), self._conf())
        self.assert_rollback_map_matches()

    def test_delete_server_removes_only_its_clients(self):
        other = self._create_server(self.manager, "office", "10.41.0.0/24", 51941)
        desk, _ = self.manager.add_wireguard_client(other["id"], "desk")

        response = self.http.delete(self._url(), json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({c["id"] for c in self.manager.get_client_configs()}, {desk["id"]})
        self.assertFalse(os.path.exists(self.server["config_path"]))
        self.assert_rollback_map_matches()

    def test_client_ids_are_scoped_to_their_server(self):
        other = self._create_server(self.manager, "office", "10.41.0.0/24", 51941)
        foreign = "/".join(("/api/servers", other["id"], "clients", self.phone["id"]))

        self.assertEqual(self.http.post(foreign + "/rename", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.http.post(foreign + "/suspend", json={}).status_code, 404)
        self.assertEqual(self.http.post(foreign + "/client-params", json={"client_params": {"Jc": 5}}).status_code, 404)
        self.assertEqual(self.http.delete(foreign, json={}).status_code, 404)

        phone = self.manager.get_client(self.phone["id"])
        self.assertEqual((phone["name"], phone["suspended"]), ("phone", False))
        self.assert_rollback_map_matches()


if __name__ == "__main__":
    unittest.main()
