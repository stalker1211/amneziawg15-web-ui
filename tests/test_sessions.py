"""Tests for the *session* Activity events (services/sessions.py, DEVELOPMENT.md §10
2.7 part 2): client.online / client.offline from the monitor's snapshots.

Each tick is an `awg show all dump` in the layout amneziawg-tools v3.1 prints, the
recorded sample of tests/test_traffic_parsing.py, run through the manager's own
read_telemetry and record_sessions at a fixed clock.
"""

import unittest
from unittest import mock

from tests.support import build_manager
from tests.test_traffic_parsing import interface_line, peer_line

# Tests exercise internals on purpose and use self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access

T0 = 1_790_718_400
ENDPOINT = "198.51.100.7:60848"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.server = self.manager.create_wireguard_server(
            {"name": "home", "protocol": "AWG 3.1", "subnet": "10.9.0.0/24", "auto_start": False}
        )
        # build_manager hands every client the same key; the dump needs one peer each.
        self.phone, self.laptop = (
            self.manager.get_client(self.manager.add_wireguard_client(self.server["id"], name)[0]["id"])
            for name in ("phone", "laptop")
        )
        self.phone["client_public_key"], self.laptop["client_public_key"] = "UEhPTkU=", "TEFQVE9Q"

    def tick(self, at, peers=None, *, read=True):
        """One monitor tick at `at`: `peers` maps a client to its peer_line fields (the
        server stopped when None). Returns the session events it recorded."""
        iface = self.server["interface"]
        lines = []
        if peers is not None:
            lines.append(interface_line(iface))
            lines += [peer_line(iface, client["client_public_key"], **fields) for client, fields in peers]
        dump = "\n".join(lines) if read else None
        self.manager.run_command = lambda args, _o=dump: _o
        before = self.manager.activity.payload()["events"]
        with mock.patch("services.amnezia_manager.time.time", return_value=at):
            self.manager.read_telemetry()
            self.manager.record_sessions()
        events = self.manager.activity.payload()["events"]
        return [e for e in reversed(events[: len(events) - len(before)]) if e["kind"] == "session"]

    def peer(self, client, handshake, rx, tx):
        return (client, {"endpoint": ENDPOINT, "handshake": handshake, "rx": rx, "tx": tx})

    def test_online_then_offline_with_duration_and_bytes(self):
        phone = self.phone
        self.assertEqual(self.tick(T0, [(phone, {"handshake": 0, "rx": 0, "tx": 0, "endpoint": "(none)"})]), [])
        with mock.patch.object(self.manager.netinfo, "lookup_geoip", return_value=("Netherlands", "NL")) as geo:
            (online,) = self.tick(T0 + 7, [self.peer(phone, T0 + 5, rx=1_000, tx=2_000)])
        geo.assert_called_once_with("198.51.100.7")
        self.assertEqual(
            {k: online[k] for k in ("kind", "event", "server_id", "server", "client_id", "client", "detail")},
            {"kind": "session", "event": "client.online", "server_id": self.server["id"], "server": "home",
             "client_id": phone["id"], "client": "phone", "detail": {"endpoint": ENDPOINT, "country": "NL"}},
        )  # fmt: skip
        # Keepalives until T0 + 607; then silence, and offline once the handshake is 300 s old.
        self.assertEqual(self.tick(T0 + 300, [self.peer(phone, T0 + 125, rx=50_000, tx=900_000)]), [])
        self.assertEqual(self.tick(T0 + 607, [self.peer(phone, T0 + 485, rx=80_000, tx=1_500_000)]), [])
        self.assertEqual(self.tick(T0 + 700, [self.peer(phone, T0 + 485, rx=80_000, tx=1_500_000)]), [])
        (offline,) = self.tick(T0 + 786, [self.peer(phone, T0 + 485, rx=80_000, tx=1_500_000)])
        self.assertEqual(offline["event"], "client.offline")
        # From the online tick to the last tick the device sent anything, not to the offline tick.
        self.assertEqual(offline["detail"], {"duration_s": 600, "received_bytes": 80_000, "sent_bytes": 1_500_000})

    def test_the_first_tick_is_a_baseline(self):
        online = [self.peer(c, T0 - 30, rx=10_000, tx=20_000) for c in (self.phone, self.laptop)]
        self.assertEqual(self.tick(T0, online), [])  # no burst of "online" at boot
        later = [self.peer(self.phone, T0 - 30, rx=15_000, tx=26_000)]  # the laptop's peer is gone
        (offline,) = self.tick(T0 + 7, later)
        self.assertEqual((offline["client"], offline["event"]), ("laptop", "client.offline"))
        # Counted from the baseline: the panel did not see the start.
        self.assertEqual(offline["detail"], {"duration_s": 0, "received_bytes": 0, "sent_bytes": 0})

    def test_a_failed_read_ends_nothing_and_a_stop_ends_all(self):
        self.tick(T0, [])
        with mock.patch.object(self.manager.netinfo, "lookup_geoip", return_value=(None, None)):
            events = self.tick(T0 + 7, [self.peer(c, T0 + 6, rx=500, tx=700) for c in (self.phone, self.laptop)])
        self.assertEqual([(e["client"], e["detail"]) for e in events],
                         [("phone", {"endpoint": ENDPOINT, "country": None}), ("laptop", {"endpoint": ENDPOINT, "country": None})])  # fmt: skip
        self.assertEqual(self.tick(T0 + 14, read=False), [])
        # The server stopped: every session ends at once, with what the last ticks counted.
        events = self.tick(T0 + 21, None)
        self.assertEqual([(e["event"], e["detail"]["received_bytes"]) for e in events], [("client.offline", 500)] * 2)

    def test_a_restarted_interface_counts_from_zero(self):
        self.tick(T0, [])
        with mock.patch.object(self.manager.netinfo, "lookup_geoip", return_value=(None, None)):
            self.tick(T0 + 7, [self.peer(self.phone, T0 + 6, rx=9_000, tx=9_000)])
        self.tick(T0 + 14, [self.peer(self.phone, T0 + 12, rx=1_000, tx=2_000)])  # counters reset
        (offline,) = self.tick(T0 + 21, [])
        self.assertEqual((offline["detail"]["received_bytes"], offline["detail"]["sent_bytes"]), (10_000, 11_000))

    def test_suspend_ends_a_session_and_a_deleted_client_is_dropped(self):
        both = [self.peer(c, T0 - 5, rx=1, tx=1) for c in (self.phone, self.laptop)]
        self.tick(T0, both)
        self.manager.toggle_client_suspend(self.server["id"], self.phone["id"])
        self.manager.delete_client(self.server["id"], self.laptop["id"])
        (offline,) = self.tick(T0 + 7, [])
        self.assertEqual((offline["client"], offline["event"]), ("phone", "client.offline"))


class ThresholdTests(unittest.TestCase):
    """Online is a handshake within 300 s, or twice an AWG 3.x client's RekeyAfterTime
    top when that is longer: such a device handshakes only that often."""

    def setUp(self):
        self.manager = build_manager()

    def threshold(self, protocol, rekey_after):
        server = {"protocol": protocol}
        client = {"client_params": {"RekeyAfterTime": rekey_after} if rekey_after else {}}
        return self.manager.active_within(server, client)

    def test_the_threshold(self):
        self.assertEqual(self.threshold("AWG 3.1", None), 300)  # unset: WireGuard's 120
        self.assertEqual(self.threshold("AWG 3.1", "100-125"), 300)  # what Generate draws
        self.assertEqual(self.threshold("AWG 3.0", "200-240"), 480)
        self.assertEqual(self.threshold("AWG 3.1", "400"), 800)
        # Below 3.0 the timers are not rendered, so the device keeps WireGuard's.
        self.assertEqual(self.threshold("AWG 2.0", "400"), 300)

    def test_sessions_and_the_page_agree_on_it(self):
        server = self.manager.create_wireguard_server(
            {"name": "slow", "protocol": "AWG 3.1", "subnet": "10.8.0.0/24", "auto_start": False}
        )
        client, _ = self.manager.add_wireguard_client(server["id"], "tablet")
        stored = self.manager.get_client(client["id"])
        stored["client_params"]["RekeyAfterTime"] = "200-240"
        iface = server["interface"]
        dump = "\n".join([interface_line(iface), peer_line(iface, client["client_public_key"], handshake=T0 - 450)])
        self.manager.run_command = lambda args: dump
        with mock.patch("services.amnezia_manager.time.time", return_value=T0):
            self.manager.read_telemetry()
        self.assertTrue(self.manager.get_traffic_for_server(server["id"])[client["id"]]["active"])
        self.assertEqual(self.manager.client_status({**stored, "server_id": server["id"]}), "active")


if __name__ == "__main__":
    unittest.main()
