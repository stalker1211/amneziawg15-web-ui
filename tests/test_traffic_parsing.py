"""Tests for the `awg show` output parser and derived client activity.

The sample below is real `awg show` output (captured from amneziawg-tools
v3.0.20260805) and doubles as a reference for the format the parser depends on.
Only four line prefixes matter: `peer:`, `endpoint:`, `latest handshake:` and
`transfer:` — AWG 3.0 added interface-level lines (`header protection key:` etc.)
which must be ignored.
"""

import unittest

from tests.support import CLIENT_PUBLIC_KEY, build_manager

AWG_SHOW_SAMPLE = """interface: wg-abc123
  public key: c2VydmVyUFVCTElDa2V5MDAwMDAwMDAwMDAwMDAwMDA=
  private key: (hidden)
  listening port: 51820
  s1: 28
  s2: 37
  s3: 108
  s4: 28
  h1: 97474
  h2: 145032
  h3: 273401
  h4: 390875
  header protection key: aGVhZGVyUFJPVEVDVElPTmtleTAwMDAwMDAwMDAwMDA=

peer: {pubkey}
  preshared key: (hidden)
  endpoint: 198.51.100.7:49232
  allowed ips: 10.9.0.2/32
  latest handshake: 1 minute, 2 seconds ago
  transfer: 1.39 MiB received, 6.59 MiB sent
  persistent keepalive: every 25 seconds

peer: b3RoZXJQVUJMSUNrZXkwMDAwMDAwMDAwMDAwMDAwMDAA=
  allowed ips: 10.9.0.3/32
  transfer: 0 B received, 0 B sent
"""


class HandshakeParsingTests(unittest.TestCase):
    """`parse_handshake_seconds` is nested inside get_traffic_for_server, so it is
    exercised through the public method using crafted `awg show` output."""

    def setUp(self):
        self.manager = build_manager()
        self.server = self.manager.create_wireguard_server({
            "name": "traffic", "protocol": "AWG 3.0", "subnet": "10.9.0.0/24",
            "auto_start": False,
        })
        self.client, _ = self.manager.add_wireguard_client(self.server["id"], "phone")

    def _traffic_for(self, handshake_line, transfer="transfer: 10 B received, 20 B sent"):
        pubkey = self.client["client_public_key"]
        output = f"interface: {self.server['interface']}\n\npeer: {pubkey}\n"
        output += "  endpoint: 198.51.100.7:1234\n"
        if handshake_line:
            output += f"  latest handshake: {handshake_line}\n"
        output += f"  {transfer}\n"

        self.manager.run_command = lambda args, _o=output: _o
        return self.manager.get_traffic_for_server(self.server["id"])[self.client["id"]]

    def test_seconds_only(self):
        self.assertEqual(self._traffic_for("57 seconds ago")["latest_handshake_seconds"], 57)

    def test_minutes_and_seconds_are_summed(self):
        self.assertEqual(self._traffic_for("1 minute, 2 seconds ago")["latest_handshake_seconds"], 62)

    def test_hours_and_days(self):
        self.assertEqual(self._traffic_for("2 hours, 5 minutes ago")["latest_handshake_seconds"], 7500)
        self.assertEqual(self._traffic_for("1 day ago")["latest_handshake_seconds"], 86400)

    def test_never_is_none_and_inactive(self):
        info = self._traffic_for("Never")
        self.assertIsNone(info["latest_handshake_seconds"])
        self.assertFalse(info["active"])

    def test_missing_handshake_line(self):
        info = self._traffic_for(None)
        self.assertIsNone(info["latest_handshake_seconds"])
        self.assertFalse(info["active"])

    def test_active_boundary_is_five_minutes(self):
        self.assertTrue(self._traffic_for("4 minutes, 59 seconds ago")["active"])
        self.assertTrue(self._traffic_for("5 minutes ago")["active"])
        self.assertFalse(self._traffic_for("5 minutes, 1 second ago")["active"])


class ShowOutputParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.server = self.manager.create_wireguard_server({
            "name": "traffic", "protocol": "AWG 3.0", "subnet": "10.9.0.0/24",
            "auto_start": False,
        })
        self.client, _ = self.manager.add_wireguard_client(self.server["id"], "phone")
        sample = AWG_SHOW_SAMPLE.format(pubkey=self.client["client_public_key"])
        self.manager.run_command = lambda args, _o=sample: _o
        self.traffic = self.manager.get_traffic_for_server(self.server["id"])

    def test_parses_transfer_endpoint_and_handshake(self):
        info = self.traffic[self.client["id"]]
        self.assertEqual(info["received"], "1.39 MiB received")
        self.assertEqual(info["sent"], "6.59 MiB sent")
        self.assertEqual(info["endpoint"], "198.51.100.7:49232")
        self.assertEqual(info["latest_handshake"], "1 minute, 2 seconds ago")
        self.assertEqual(info["latest_handshake_seconds"], 62)
        self.assertTrue(info["active"])

    def test_awg3_interface_lines_do_not_confuse_the_parser(self):
        """The 'header protection key:' line must not be read as peer data."""
        self.assertEqual(len(self.traffic), 1)

    def test_unknown_peers_are_ignored(self):
        """The sample has a second peer that is not a known client."""
        self.assertNotIn("b3RoZXJQVUJMSUNrZXkwMDAwMDAwMDAwMDAwMDAwMDAA=", self.traffic)

    def test_client_status_is_derived(self):
        self.assertEqual(self.manager.get_client(self.client["id"])["status"], "active")


class MissingDataTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.server = self.manager.create_wireguard_server({
            "name": "traffic", "protocol": "AWG 1.5", "subnet": "10.9.0.0/24",
            "auto_start": False,
        })
        self.client, _ = self.manager.add_wireguard_client(self.server["id"], "phone")

    def test_peer_absent_from_output_reports_zeroes(self):
        self.manager.run_command = lambda args: f"interface: {self.server['interface']}\n"
        info = self.manager.get_traffic_for_server(self.server["id"])[self.client["id"]]
        self.assertEqual(info["received"], "0 B")
        self.assertEqual(info["sent"], "0 B")
        self.assertIsNone(info["endpoint"])
        self.assertFalse(info["active"])

    def test_no_output_returns_none(self):
        self.manager.run_command = lambda args: ""
        self.assertIsNone(self.manager.get_traffic_for_server(self.server["id"]))

    def test_unknown_server_returns_none(self):
        self.assertIsNone(self.manager.get_traffic_for_server("nope"))

    def test_endpoint_none_literal(self):
        pubkey = self.client["client_public_key"]
        output = (f"interface: {self.server['interface']}\n\npeer: {pubkey}\n"
                  "  endpoint: (none)\n  transfer: 0 B received, 0 B sent\n")
        self.manager.run_command = lambda args, _o=output: _o
        info = self.manager.get_traffic_for_server(self.server["id"])[self.client["id"]]
        self.assertEqual(info["endpoint"], "(none)")
        self.assertIsNone(info["geo"])


if __name__ == "__main__":
    unittest.main()
