"""Tests for a client's AllowedIPs (IPv6 through the tunnel, split tunnelling) and a
server's endpoint host (what client configs dial instead of the detected IP).

Both change what a device holds, so the tests pin who gets flagged for Re-import:
nobody at the upgrade, the edited client for AllowedIPs, and every client of the
server for an endpoint host -- after which a new public IP flags none of them.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from tests.support import build_app, build_manager

# pylint: disable=missing-function-docstring,missing-class-docstring


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_allowed_ips(self):
        self.assertEqual(self.m.validate_allowed_ips("0.0.0.0/0,::/0"), "0.0.0.0/0, ::/0")
        self.assertEqual(self.m.validate_allowed_ips(" 10.1.2.3/8 , 10.0.0.0/8, 2001:db8::1/32 "), "10.0.0.0/8, 2001:db8::/32")
        self.assertEqual(self.m.validate_allowed_ips(["192.168.1.0/24"]), "192.168.1.0/24")
        for bad in ("", " , ", "10.0.0.0/33", "example.com", "0.0.0.0/0\nDNS = 1.1.1.1"):
            with self.assertRaises(ValueError, msg=bad):
                self.m.validate_allowed_ips(bad)

    def test_endpoint_host(self):
        for raw, expected in (("", ""), (None, ""), ("203.0.113.5", "203.0.113.5"), ("VPN.Example.com.", "vpn.example.com"),
                              ("nxtalk.freemyip.com", "nxtalk.freemyip.com")):  # fmt: skip
            self.assertEqual(self.m.validate_endpoint_host(raw), expected, raw)
        for bad in ("localhost", "vpn_1.example.com", "-a.example.com", "1.2.3", "a..b", "x" * 64 + ".com",
                    "host.123", "vpn.example.com:51820", "2001:db8::1"):  # fmt: skip
            with self.assertRaises(ValueError, msg=bad):
                self.m.validate_endpoint_host(bad)


class UpgradeTests(unittest.TestCase):
    def test_clients_from_before_24_stay_ipv4_only_and_unflagged(self):
        first = build_manager()
        server = first.create_wireguard_server({"name": "home", "port": 51940, "subnet": "10.40.0.0/24", "auto_start": False})
        client, _ = first.add_wireguard_client(server["id"], "phone")
        # The device holds what 2.3 issued: AllowedIPs = 0.0.0.0/0, the detected IP.
        as_issued_by_23 = {**client, "allowed_ips": "0.0.0.0/0"}
        self.assertIn("AllowedIPs = 0.0.0.0/0\n", first.generate_wireguard_client_config(server, as_issued_by_23, False))
        data = json.loads(Path(first.config_file).read_text(encoding="utf-8"))
        data["servers"][0].pop("endpoint_host")  # neither field existed in 2.3
        stored = data["servers"][0]["clients"][0]
        stored.pop("allowed_ips")
        stored["config_issued_fingerprint"] = first.config_fingerprint(server, as_issued_by_23)
        Path(first.config_file).write_text(json.dumps(data), encoding="utf-8")

        manager = build_manager(config_dir=first.config_dir, wireguard_config_dir=first.wireguard_config_dir,
                                config_file=first.config_file)  # fmt: skip
        loaded = manager.get_client(client["id"])
        self.assertEqual(loaded["allowed_ips"], "0.0.0.0/0")
        self.assertEqual(manager.get_server(server["id"])["endpoint_host"], "")
        self.assertFalse(manager.is_config_outdated(manager.get_server(server["id"]), loaded))


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.http = self.app.test_client()
        self.home = self._create("home", "10.40.0.0/24", 51940)
        self.office = self._create("office", "10.41.0.0/24", 51941)
        self.phone = self._add(self.home, "phone")
        self.laptop = self._add(self.home, "laptop")
        self.desk = self._add(self.office, "desk")
        for client in (self.phone, self.laptop, self.desk):
            self.http.post(self._client_url(client, "issued"), json={})

    def _create(self, name, subnet, port, **extra):
        body = {"name": name, "protocol": "AWG 2.0", "subnet": subnet, "port": port, "auto_start": False, **extra}
        response = self.http.post("/api/servers", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def _add(self, server, name, **extra):
        return self.http.post(f"/api/servers/{server['id']}/clients", json={"name": name, **extra}).get_json()["client"]

    @staticmethod
    def _client_url(client, *parts):
        return "/".join(("/api/servers", client["server_id"], "clients", client["id"], *parts))

    def outdated(self):
        return {c["name"] for s in self.http.get("/api/servers").get_json() for c in s["clients"] if c["config_outdated"]}

    def config(self, client):
        return self.http.get(self._client_url(client, "config-both")).get_json()["clean_config"]

    def test_new_clients_route_ipv4_and_can_split_tunnel(self):
        self.assertIn("AllowedIPs = 0.0.0.0/0\n", self.config(self.phone))
        split = self._add(self.home, "split", allowed_ips="192.168.1.0/24")
        self.assertIn("AllowedIPs = 192.168.1.0/24\n", self.config(split))
        response = self.http.post(f"/api/servers/{self.home['id']}/clients", json={"name": "bad", "allowed_ips": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_editing_allowed_ips_flags_only_that_client(self):
        url = self._client_url(self.phone, "client-params")
        body = {"client_params": {"Jc": 5}, "allowed_ips": "0.0.0.0/0, ::/0"}
        self.assertEqual(self.http.post(url, json={**body, "allowed_ips": "x"}).status_code, 400)
        self.assertEqual(self.outdated(), set())
        self.assertEqual(self.http.post(url, json=body).status_code, 200)
        self.assertEqual(self.outdated(), {"phone"})
        self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0\n", self.config(self.phone))

    def test_allowed_ips_are_never_a_warning(self):
        def warnings(allowed):
            body = {"server_id": self.home["id"], "client_params": {"Jc": 5}, "allowed_ips": allowed}
            return self.http.post("/api/validate", json=body).get_json()["warnings"]

        self.assertEqual(warnings("0.0.0.0/0"), [])
        self.assertEqual(warnings("0.0.0.0/0, ::/0"), [])
        self.assertEqual(warnings("192.168.1.0/24"), [])

    def test_an_endpoint_host_flags_the_servers_clients_and_then_outlives_ip_changes(self):
        url = f"/api/servers/{self.home['id']}/endpoint-host"
        self.assertEqual(self.http.post(url, json={"endpoint_host": "no_such host"}).status_code, 400)
        # The drawer's preview counts it before saving.
        preview = self.http.post("/api/validate", json={
            "server_id": self.home["id"], "protocol": "AWG 2.0", "endpoint_host": "vpn.example.com",
            "transport_params": self.manager.get_server(self.home["id"])["transport_params"]}).get_json()  # fmt: skip
        self.assertEqual((preview["errors"], preview["configs_changed"], preview["outdated_after"]), ([], 2, 2))

        self.assertEqual(
            self.http.post(url, json={"endpoint_host": "VPN.example.com"}).get_json()["endpoint_host"], "vpn.example.com"
        )
        self.assertEqual(self.outdated(), {"phone", "laptop"})
        self.assertIn("Endpoint = vpn.example.com:51940\n", self.config(self.phone))
        for client in (self.phone, self.laptop):
            self.http.post(self._client_url(client, "issued"), json={})

        with mock.patch.object(self.manager, "detect_public_ip", return_value="198.51.100.20"), \
             mock.patch.object(self.manager, "lookup_geoip", return_value=(None, None)):  # fmt: skip
            self.assertEqual(self.http.post("/api/system/refresh-ip", json={}).status_code, 200)
        # A new WAN address reaches only the server that still dials it.
        self.assertEqual(self.outdated(), {"desk"})

    def test_a_new_server_can_start_with_an_endpoint_host(self):
        body = {"server": {"name": "x", "port": 51950, "subnet": "10.50.0.0/24", "endpoint_host": "bad host"}}
        self.assertEqual(len(self.http.post("/api/validate", json=body).get_json()["errors"]), 1)
        server = self._create("dyn", "10.50.0.0/24", 51950, endpoint_host="203.0.113.77")
        self.assertEqual(server["endpoint_host"], "203.0.113.77")
        self.assertIn("Endpoint = 203.0.113.77:51950\n", self.config(self._add(server, "c")))


if __name__ == "__main__":
    unittest.main()
