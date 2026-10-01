"""Tests for the traffic history (services/history.py) and the manager's recording of it.

The history turns the 7 s loop's counter readings into per-client deltas: the last
hour per tick and 24 h per minute, in memory. These pin the arithmetic (deltas,
resets, the first reading, gaps), the bounds of both rings and the three ranges the
page asks for; the manager's side is the state of each client and what a stopped
server, a suspended client or a failed read records.
"""

import threading
import unittest

# Tests exercise internals on purpose and use self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access,wrong-import-order
from tests.support import build_manager
from tests.test_traffic_parsing import interface_line, peer_line

from services.history import TrafficHistory  # isort: skip -- after tests.support, which puts web-ui on the path

T0 = 1_790_718_000  # a minute boundary (divisible by 60)
MB = 1_000_000


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.history = TrafficHistory(clock=self.clock)

    def tick(self, at, **clients):
        """One reading of server "s": client id -> (state, rx, tx) or a stopped server (None)."""
        self.history.record(at, {"s": clients if clients else {}})
        self.clock.now = at

    def one_hour(self, cid="a"):
        return self.history.series("s", "1h", [cid])

    def test_deltas_become_rates_over_the_tick(self):
        self.tick(T0, a=("o", 1000, 5000))
        self.tick(T0 + 7, a=("o", 1000 + 7 * MB, 5000 + 14 * MB))
        # bit/s: 7 MB in 7 s received is 8 Mbit/s.
        self.assertEqual(self.history.rates("s"), {"a": (8 * MB, 16 * MB)})
        series = self.one_hour()
        self.assertEqual(series["t"], [T0, T0 + 7])
        self.assertEqual(series["clients"]["a"]["received_bps"], [0, 8 * MB])
        self.assertEqual(series["totals"]["a"], {"received_bytes": 7 * MB, "sent_bytes": 14 * MB})

    def test_the_first_reading_is_a_baseline(self):
        # A panel restarted under a running daemon must not read 6 GB as one tick.
        self.tick(T0, a=("o", 6_000 * MB, 800 * MB))
        self.assertEqual(self.history.rates("s"), {"a": (0, 0)})
        self.assertEqual(self.history.since, T0)

    def test_a_lower_counter_is_a_reset_never_a_negative_delta(self):
        self.tick(T0, a=("o", 50 * MB, 9 * MB))
        self.tick(T0 + 7, a=("o", 7 * MB, 0))  # the interface restarted: counting from 0
        self.assertEqual(self.history.rates("s"), {"a": (8 * MB, 0)})

    def test_a_suspended_client_moves_nothing_and_counts_from_zero_when_back(self):
        self.tick(T0, a=("o", 5 * MB, 9 * MB))
        self.tick(T0 + 7, a=("s", None, None))  # no peer in the daemon
        self.assertEqual(self.history.rates("s"), {"a": (0, 0)})
        # syncconf re-added it at 0; past its old counters, which a lower-counter rule misses.
        self.tick(T0 + 14, a=("o", 7 * MB, 7 * MB))
        self.assertEqual(self.history.rates("s"), {"a": (8 * MB, 8 * MB)})
        self.assertEqual(self.one_hour()["clients"]["a"]["state"], "oso")

    def test_a_stopped_server_leaves_a_gap(self):
        self.tick(T0, a=("o", 9 * MB, 0))
        self.history.record(T0 + 7, {"s": None})
        self.clock.now = T0 + 7
        self.assertEqual(self.history.rates("s"), {})  # not running at the last tick
        self.tick(T0 + 14, a=("o", 7 * MB, 0))  # started again: counted from 0, not a baseline
        series = self.one_hour()
        self.assertEqual(series["t"], [T0, T0 + 14])  # no point at T0 + 7: the page draws a gap
        # The interval is since the last reading of any server, so the restart's bytes
        # are not spread over the gap.
        self.assertEqual(series["clients"]["a"]["received_bps"], [0, 8 * MB])

    def test_a_server_seen_stopped_first_counts_from_zero(self):
        self.history.record(T0, {"s": None})
        self.tick(T0 + 7, a=("o", 7 * MB, 0))
        self.assertEqual(self.history.rates("s"), {"a": (8 * MB, 0)})

    def test_a_client_added_later_has_no_data_before_it(self):
        self.tick(T0, a=("o", 0, 0))
        self.tick(T0 + 7, a=("o", 0, 0), b=("-", 0, 0))
        series = self.history.series("s", "1h", ["a", "b", "never"])
        self.assertEqual(series["clients"]["b"], {"received_bps": [None, 0], "sent_bps": [None, 0], "state": " -"})
        self.assertEqual(series["clients"]["never"]["state"], "  ")
        self.assertEqual(series["totals"]["never"], {"received_bytes": 0, "sent_bytes": 0})

    def test_deleted_servers_and_clients_are_forgotten(self):
        self.history.record(T0, {"s": {"a": ("o", 0, 0)}, "gone": {"x": ("o", 0, 0)}})
        self.history.record(T0 + 7, {"s": {}})
        self.assertNotIn("gone", self.history._servers)
        self.assertEqual(self.history._servers["s"].counters, {})

    def test_a_clock_going_back_records_nothing(self):
        self.tick(T0, a=("o", 0, 0))
        self.history.record(T0 - 5, {"s": {"a": ("o", MB, 0)}})
        self.assertEqual(len(self.history._servers["s"].ticks), 1)

    def test_minute_sums_at_their_boundaries(self):
        # 59 s and 60 s fall in different minutes; each minute's rate is its bytes over
        # the seconds its ticks cover.
        self.tick(T0 + 52, a=("o", 0, 0))
        self.tick(T0 + 59, a=("o", 7 * MB, 0))
        self.tick(T0 + 60, a=("s", None, None))
        self.tick(T0 + 67, a=("o", 7 * MB, 0))
        minutes = list(self.history._servers["s"].minutes)
        self.assertEqual([m[0] for m in minutes], [T0, T0 + 60])
        self.assertEqual(minutes[0][2]["a"], [7 * MB, 0, "o"])
        self.assertEqual(minutes[1][1], 8)  # 1 s, then 7 s
        self.assertEqual(minutes[1][2]["a"], [7 * MB, 0, "o"])  # the state of its last tick

        series = self.history.series("s", "6h", ["a"])
        self.assertEqual(len(series["t"]), 360)
        self.assertEqual(series["t"][-2:], [T0, T0 + 60])
        # 7 MB over the 14 s the first minute's ticks cover (the first one counts 7).
        self.assertEqual(series["clients"]["a"]["received_bps"][-2:], [4 * MB, 7 * MB])
        self.assertEqual(series["clients"]["a"]["state"][-3:], " oo")
        self.assertIsNone(series["clients"]["a"]["received_bps"][0])

    def test_both_rings_are_bounded(self):
        at = T0
        for _ in range(25 * 3600 // 7):
            at += 7
            self.history.record(at, {"s": {"a": ("o", 0, 0)}})
        server = self.history._servers["s"]
        # Every tick of the last hour (3600 / 7 = 514.3), and 1440 minutes.
        self.assertIn(len(server.ticks), (514, 515))
        self.assertGreater(server.ticks[0][0], at - 3600)
        self.assertEqual(len(server.minutes), 1440)

    def test_the_three_ranges(self):
        for k in range(0, 2 * 3600, 7):
            self.tick(T0 + k, a=("o", k * MB, 0))
        now = self.clock.now
        one, six, day = (self.history.series("s", r, ["a"]) for r in ("1h", "6h", "24h"))
        self.assertTrue(all(now - 3600 < t <= now for t in one["t"]))
        self.assertEqual(len(one["t"]), len(one["clients"]["a"]["state"]))
        self.assertEqual((len(six["t"]), len(day["t"])), (360, 1440))
        self.assertEqual(day["t"][-1], now // 60 * 60)
        self.assertEqual(six["t"], day["t"][-360:])
        # 1 MB a second, 8 Mbit/s, wherever there is data.
        self.assertEqual({v for v in one["clients"]["a"]["received_bps"]}, {8 * MB})
        self.assertEqual({v for v in day["clients"]["a"]["received_bps"][-119:]}, {8 * MB})
        self.assertEqual(day["clients"]["a"]["state"].strip(), "o" * 120)
        self.assertEqual(day["since"], T0)
        self.assertEqual(day["totals"]["a"]["received_bytes"], (now - T0) * MB)
        with self.assertRaises(KeyError):
            self.history.series("s", "2h", ["a"])

    def test_an_unknown_server_has_an_empty_history(self):
        self.assertEqual(self.history.series("nope", "1h", ["a"])["t"], [])
        self.assertEqual(self.history.series("nope", "24h", ["a"])["clients"]["a"]["state"], " " * 1440)
        self.assertEqual(self.history.rates("nope"), {})
        self.assertIsNone(self.history.since)

    def test_reading_while_the_loop_records(self):
        stop, errors = threading.Event(), []

        def read():
            while not stop.is_set():
                try:
                    for r in TrafficHistory.RANGES:
                        series = self.history.series("s", r, ["a"])
                        self.assertEqual(len(series["t"]), len(series["clients"]["a"]["state"]))
                except Exception as e:  # reported below: a thread cannot fail the test
                    errors.append(e)

        readers = [threading.Thread(target=read) for _ in range(3)]
        for thread in readers:
            thread.start()
        for k in range(3000):
            self.history.record(T0 + k * 7, {"s": {"a": ("o", k, k)}})
        stop.set()
        for thread in readers:
            thread.join()
        self.assertEqual(errors, [])


class ManagerRecordingTests(unittest.TestCase):
    """record_history: each client's state and counters from the last dump."""

    def setUp(self):
        self.manager = build_manager()
        self.manager.history = TrafficHistory(clock=Clock(T0 + 30))
        self.server = self.manager.create_wireguard_server(
            {"name": "history", "protocol": "AWG 3.1", "subnet": "10.9.0.0/24", "auto_start": False}
        )
        self.clients = {name: self.manager.add_wireguard_client(self.server["id"], name)[0] for name in "abcd"}
        for name, client in self.clients.items():  # the test manager gives every client one key
            client["client_public_key"] = f"key-{name}"
        self.manager.toggle_client_suspend(self.server["id"], self.clients["c"]["id"])

    def read(self, at, peers, interface=True):
        iface = self.server["interface"]
        lines = [interface_line(iface)] if interface else []
        lines += [peer_line(iface, self.clients[n]["client_public_key"], **kw) for n, kw in peers.items()]
        self.manager._telemetry = {"at": at, "interfaces": self.manager.parse_dump("\n".join(lines)), "read": True}
        self.manager.record_history()

    def test_states_and_counters(self):
        # a: online; b: a stale handshake; c: suspended (no peer); d: no peer at all.
        self.read(T0, {"a": {"handshake": T0 - 10, "rx": 0, "tx": 0}, "b": {"handshake": T0 - 900}})
        self.read(T0 + 7, {"a": {"handshake": T0, "rx": 7 * MB, "tx": 0}, "b": {"handshake": T0 - 900}})
        ids = [self.clients[n]["id"] for n in "abcd"]
        series = self.manager.history.series(self.server["id"], "1h", ids)
        self.assertEqual([series["clients"][cid]["state"] for cid in ids], ["oo", "--", "ss", "--"])
        self.assertEqual(series["clients"][ids[0]]["received_bps"], [0, 8 * MB])
        traffic = self.manager.get_traffic_for_server(self.server["id"])
        self.assertEqual((traffic[ids[0]]["received_bps"], traffic[ids[0]]["sent_bps"]), (8 * MB, 0))
        self.assertEqual(traffic[ids[2]]["received_bps"], 0)

    def test_a_stopped_server_records_no_tick(self):
        self.read(T0, {}, interface=False)
        self.assertFalse(self.manager.history._servers[self.server["id"]].ticks)
        self.assertEqual(self.manager.history.since, T0)

    def test_a_failed_read_records_nothing(self):
        self.read(T0, {"a": {"rx": 0}})
        self.manager.run_command = lambda args: None
        self.manager.read_telemetry()
        self.manager.record_history()
        self.assertEqual(len(self.manager.history._servers[self.server["id"]].ticks), 1)
        # The next good read spans both intervals: 14 s, not a 7 s spike.
        self.read(T0 + 14, {"a": {"rx": 14 * MB}})
        self.assertEqual(self.manager.history.rates(self.server["id"])[self.clients["a"]["id"]][0], 8 * MB)


if __name__ == "__main__":
    unittest.main()
