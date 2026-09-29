"""Tests for the telemetry read: `awg show all dump`, parsed, and what is derived from it.

The sample is the layout amneziawg-tools v3.1 prints (show.c dump_print), captured
from a live AWG 3.1 server with keys and addresses replaced: per interface, one
~30-column line of its own (keys, listen port, J/S/H, I1-I5, the 3.x fields), then
one 9-column line per peer. Bytes and handshakes are plain integers -- the reason
for the switch from parsing "1.39 MiB received" and "1 minute, 2 seconds ago".
"""

import unittest
from unittest import mock

from tests.support import build_manager

# Tests exercise internals on purpose and use self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access

NOW = 1_790_718_400


def interface_line(iface):
    return "\t".join(  # noqa: FLY002 -- one column per dump field
        [iface, "cHJpdmF0ZQ==", "c2VydmVy", "51823", "0", "0", "0", "118", "90", "61", "22",
         "716817930-716834495", "1087412890-1087443650", "1531382075-1531413411", "1889269886-1889303874",
         "(null)", "(null)", "(null)", "(null)", "(null)", "aGVhZGVy", "0", "0", "0", "0", "0", "0", "off", "off", "off"]
    )  # fmt: skip


def peer_line(iface, key, endpoint="198.51.100.7:60848", handshake=NOW - 62, rx=1_457_520, tx=6_909_870):
    return "\t".join([iface, key, "cHNr", endpoint, "10.64.0.2/32", str(handshake), str(rx), str(tx), "off"])


class DumpParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()

    def test_interface_lines_are_skipped_and_peers_read(self):
        dump = "\n".join(
            [interface_line("wg-a"), peer_line("wg-a", "k1"), peer_line("wg-a", "k2", "(none)", 0, 0, 0),
             interface_line("wg-b")]
        )  # fmt: skip
        self.assertEqual(
            self.manager.parse_dump(dump),
            {
                "wg-a": {
                    "k1": {"endpoint": "198.51.100.7:60848", "handshake_at": NOW - 62, "rx": 1_457_520, "tx": 6_909_870},
                    "k2": {"endpoint": None, "handshake_at": None, "rx": 0, "tx": 0},
                },
                "wg-b": {},
            },
        )

    def test_odd_lines_are_skipped_not_fatal(self):
        dump = "\n".join([interface_line("wg-a"), "wg-a\tshort", peer_line("wg-a", "k1", rx="x"), "", peer_line("wg-a", "k2")])
        self.assertEqual(list(self.manager.parse_dump(dump)["wg-a"]), ["k2"])
        self.assertEqual(self.manager.parse_dump(""), {})

    def test_endpoint_ip(self):
        self.assertEqual(self.manager.endpoint_ip("198.51.100.7:49232"), "198.51.100.7")
        self.assertEqual(self.manager.endpoint_ip("[2001:db8::1]:51820"), "2001:db8::1")
        self.assertIsNone(self.manager.endpoint_ip(None))
        self.assertIsNone(self.manager.endpoint_ip("garbage"))


class OperstateTests(unittest.TestCase):
    def test_operstate_is_read_from_sysfs(self):
        from services.amnezia_manager import AmneziaManager  # pylint: disable=import-outside-toplevel

        with mock.patch("builtins.open", mock.mock_open(read_data="unknown\n")) as opened:
            self.assertEqual(AmneziaManager.interface_state("wg-abc"), "unknown")
        opened.assert_called_once_with("/sys/class/net/wg-abc/operstate", encoding="ascii")
        self.assertIsNone(AmneziaManager.interface_state("wg-surely-absent-9"))


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.server = self.manager.create_wireguard_server(
            {"name": "traffic", "protocol": "AWG 3.1", "subnet": "10.9.0.0/24", "auto_start": False}
        )
        self.client, _ = self.manager.add_wireguard_client(self.server["id"], "phone")

    def read(self, *peers):
        iface = self.server["interface"]
        dump = "\n".join([interface_line(iface), *(peer_line(iface, self.client["client_public_key"], **p) for p in peers)])
        self.manager.run_command = lambda args, _o=dump: _o
        with mock.patch("services.amnezia_manager.time.time", return_value=NOW):
            self.manager.read_telemetry()
        return (self.manager.get_traffic_for_server(self.server["id"]) or {}).get(self.client["id"])

    def test_bytes_endpoint_and_handshake(self):
        with mock.patch.object(self.manager, "lookup_geoip_cached", return_value=("Somewhere", "NL")) as geo:
            info = self.read({})
        geo.assert_called_with("198.51.100.7")
        self.assertEqual(
            info,
            {"received_bytes": 1_457_520, "sent_bytes": 6_909_870, "endpoint": "198.51.100.7:60848", "geo": "Somewhere",
             "geo_country_code": "NL", "latest_handshake_at": NOW - 62, "latest_handshake_seconds": 62, "active": True},
        )  # fmt: skip

    def test_active_boundary_is_five_minutes(self):
        self.assertTrue(self.read({"handshake": NOW - 300})["active"])
        self.assertFalse(self.read({"handshake": NOW - 301})["active"])

    def test_never_and_absent_peers(self):
        never = self.read({"endpoint": "(none)", "handshake": 0, "rx": 0, "tx": 0})
        self.assertEqual((never["endpoint"], never["latest_handshake_seconds"], never["active"]), (None, None, False))
        absent = self.read()
        self.assertEqual((absent["received_bytes"], absent["sent_bytes"], absent["active"]), (0, 0, False))

    def test_a_server_missing_from_the_dump_is_not_running(self):
        self.manager.run_command = lambda args: interface_line("wg-other")
        self.manager.read_telemetry()
        self.assertIsNone(self.manager.get_traffic_for_server(self.server["id"]))
        self.assertIsNone(self.manager.get_traffic_for_server("nope"))

    def test_a_failed_read_empties_the_snapshot(self):
        self.read({})
        self.manager.run_command = lambda args: None
        self.manager.read_telemetry()
        self.assertIsNone(self.manager.get_traffic_for_server(self.server["id"]))

    def test_client_status_is_derived_and_never_stored(self):
        self.read({})
        self.assertEqual(self.manager.client_status(self.client), "active")
        self.assertNotIn("status", self.manager.get_client(self.client["id"]))
        self.read({"handshake": NOW - 600})
        self.assertEqual(self.manager.client_status(self.client), "inactive")
        # The monitor reads telemetry every 7 s; it must never write the config.
        with mock.patch.object(self.manager, "save_config") as save:
            self.read({})
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
