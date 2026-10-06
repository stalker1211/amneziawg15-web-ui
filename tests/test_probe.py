"""Tests for services/probe.py: when to capture, the capture itself, and the Old config /
Maybe blocked verdicts (DEVELOPMENT.md §10, 2.8, part 2).

Every I/O is a fake: `conntrack` answers what a test sets, the capture returns the
datagrams tests/fixtures/handshakes/capture.jsonl has from that source to that port
(the real daemon's, see test_initiation.py for the five devices), background tasks run
inline unless a test holds them, and the GeoIP lookup always says US. Ticks are 7 s
apart, as in the traffic monitor.
"""

import json
import socket
import unittest
from pathlib import Path
from unittest import mock

# Tests exercise internals on purpose and use self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,wrong-import-order
import tests.support  # noqa: F401 -- puts web-ui on the path

from services import probe  # isort: skip -- after tests.support
from services.probe import Probe, parse_conntrack  # isort: skip

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "handshakes"
KEYS = json.loads((FIXTURE / "keys.json").read_text(encoding="utf-8"))
RECORDS = [json.loads(line) for line in (FIXTURE / "capture.jsonl").read_text(encoding="utf-8").splitlines()]
CONNTRACK_TXT = (FIXTURE / "conntrack.txt").read_text(encoding="utf-8")
K1 = KEYS["servers"]["51820"]["hpk"]
PUB = KEYS["clients"]
# Each device's flow, (server port, address, source port), as the fixture has them.
A = (51820, "192.168.97.3", 50980)  # old S/H, trailers on
B = (51821, "192.168.97.4", 37071)  # old S/H, no header protection
C = (51820, "192.168.97.5", 59901)  # an old HeaderProtectionKey: nobody
D = (51820, "192.168.97.6", 42244)  # another server's key: nobody
E = (51820, "192.168.97.7", 43006)  # correct

# run.sh's current parameters (test_initiation.py's CUR0, CUR1), and the old ones A and
# B were left on: a server on those reads their initiations.
CUR0 = {"S1": 40, "S2": 30, "S3": 50, "S4": 20, "H1": "100000-100999", "H2": "200000-200999",
        "H3": "300000-300999", "H4": "400000-400999", "HeaderProtectionKey": K1, "RandomTrailers": False}  # fmt: skip
CUR1 = {"S1": 55, "S2": 66, "S3": 40, "S4": 20, "H1": "1100000-1100999", "H2": "1200000-1200999",
        "H3": "1300000-1300999", "H4": "1400000-1400999"}  # fmt: skip
OLD_A = {**CUR0, "S1": 31, "H1": "500000-500999", "RandomTrailers": True}
OLD_B_TRAILERS = {**CUR1, "S1": 33, "H1": "1500000-1500999", "RandomTrailers": True}

DIAGNOSIS_KEYS = {"verdict", "since", "last_attempt", "attempts", "endpoint", "country", "device", "server",
                  "mismatch", "last_handshake", "params_changed_at"}  # fmt: skip


def flow_line(source, timeout, unreplied=True):
    port, src, sport = source
    flag = " [UNREPLIED]" if unreplied else ""
    assured = "" if unreplied else " [ASSURED]"
    return (
        f"udp      17 {timeout} src={src} dst=192.168.97.2 sport={sport} dport={port}{flag} "
        f"src=192.168.97.2 dst={src} sport={port} dport={sport}{assured} mark=0 use=1"
    )


def sent(source):
    port, src, sport = source
    return [bytes.fromhex(r["hex"]) for r in RECORDS if (r["dport"], r["src"], r["sport"]) == (port, src, sport)]


def endpoint(source):
    return f"{source[1]}:{source[2]}"


class Harness:
    """Two servers as stored (s0 on 51820 with clients a, c, e; s1 on 51821 with b),
    both running, every peer in the dump, never connected."""

    def __init__(self, params0=None, params1=None):
        self.servers = [
            {"id": "s0", "interface": "awg0", "port": 51820, "server_private_key": KEYS["servers"]["51820"]["priv"],
             "transport_params": dict(params0 or CUR0),
             "clients": [self.client(name) for name in "ace"]},
            {"id": "s1", "interface": "awg1", "port": 51821, "server_private_key": KEYS["servers"]["51821"]["priv"],
             "transport_params": dict(params1 or CUR1), "clients": [self.client("b")]},
        ]  # fmt: skip
        self.peers = {
            "awg0": {PUB[name]: self.peer() for name in "ACE"},
            "awg1": {PUB["B"]: self.peer()},
        }
        self.conntrack = ""
        self.run_calls, self.captures, self.events, self.held = [], [], [], []
        self.sends = {}  # source -> datagrams the capture returns in place of the fixture's
        self.capture_error = None
        self.inline = True
        self.probe = Probe(
            run_command=self.run,
            start_background_task=self.background,
            lookup_geoip=lambda ip: ("Somewhere, US", "US"),
            on_event=lambda event, server, client, detail: self.events.append((event, client["id"], detail)),
            capture=self.capture,
        )

    @staticmethod
    def client(name):
        return {"id": name, "name": f"u_{name}", "client_public_key": PUB[name.upper()], "suspended": False}

    @staticmethod
    def peer(endpoint_=None, handshake_at=None, rx=0):
        return {"endpoint": endpoint_, "handshake_at": handshake_at, "rx": rx, "tx": 0}

    def run(self, args):
        self.run_calls.append(args)
        return self.conntrack

    def capture(self, port, src, sport, seconds, limit):
        self.captures.append((port, src, sport))
        self.limits = (seconds, limit)
        if self.capture_error:
            raise self.capture_error
        return self.sends.get((port, src, sport), sent((port, src, sport)))[:limit]

    def background(self, target):
        if self.inline:
            target()
        else:
            self.held.append(target)

    def tick(self, at, read=True):
        self.probe.tick(at, self.servers, {"at": at, "interfaces": self.peers, "read": read})

    def flows(self, *lines):
        self.conntrack = "\n".join(lines)

    def set_peer(self, name, **values):
        interface = "awg1" if name == "b" else "awg0"
        self.peers[interface][PUB[name.upper()]].update(values)

    def server(self, server_id):
        return next(server for server in self.servers if server["id"] == server_id)


class ParseConntrackTests(unittest.TestCase):
    def test_the_fixture(self):
        flows = parse_conntrack(CONNTRACK_TXT)
        self.assertEqual(len(flows), 6)
        unreplied = {(f["dport"], f["src"], f["sport"]) for f in flows if f["unreplied"]}
        self.assertEqual(unreplied, {A, B, C, D})
        replied = {(f["dport"], f["src"], f["sport"]): f["timeout"] for f in flows if not f["unreplied"]}
        self.assertEqual(replied, {E: 119, (53, "127.0.0.1", 42276): 16})
        self.assertEqual(next(f["timeout"] for f in flows if (f["dport"], f["src"], f["sport"]) == B), 25)

    def test_skips_what_it_cannot_read(self):
        lines = (
            "",
            "conntrack v1.4.8 (conntrack-tools): 6 flow entries have been shown.",
            "tcp      6 431999 ESTABLISHED src=1.2.3.4 dst=5.6.7.8 sport=1 dport=2 mark=0 use=1",
            "udp      17 29 src=1.2.3.4 dst=5.6.7.8 [UNREPLIED]",
            "udp      17 x src=1.2.3.4 dst=5.6.7.8 sport=1 dport=2",
        )
        text = "\n".join(lines)
        self.assertEqual(parse_conntrack(text), [])
        self.assertEqual(parse_conntrack(None), [])

    def test_ipv6(self):
        line = "udp      17 30 src=2001:db8::5 dst=2001:db8::1 sport=40000 dport=51820 [UNREPLIED] src=2001:db8::1 dst=2001:db8::5 sport=51820 dport=40000 mark=0 use=1"
        self.assertEqual(
            parse_conntrack(line), [{"src": "2001:db8::5", "sport": 40000, "dport": 51820, "timeout": 30, "unreplied": True}]
        )


class FakeRawSocket:
    """socket.socket for capture(): recvfrom answers from `packets`, then times out."""

    def __init__(self, packets):
        self.packets = list(packets)
        self.opened = []

    def __call__(self, family, kind, proto):
        self.opened.append((family, kind, proto))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, seconds):
        self.timeout = seconds

    def recvfrom(self, size):
        if not self.packets:
            raise TimeoutError
        return self.packets.pop(0)


def udp(sport, dport, payload):
    return sport.to_bytes(2, "big") + dport.to_bytes(2, "big") + (8 + len(payload)).to_bytes(2, "big") + b"\0\0" + payload


def ipv4(src, sport, dport, payload, options=b""):
    ihl = (20 + len(options)) // 4
    header = bytes([0x40 | ihl]) + bytes(11) + socket.inet_aton(src) + socket.inet_aton("192.168.97.2") + options
    return header + udp(sport, dport, payload), (src, 0)


class CaptureTests(unittest.TestCase):
    def capture(self, packets, src="192.168.97.3", limit=64):
        raw = FakeRawSocket(packets)
        with mock.patch.object(probe.socket, "socket", raw):
            return probe.capture(51820, src, 50980, 10, limit), raw

    def test_keeps_only_that_source_to_that_port(self):
        packets = [
            ipv4("192.168.97.3", 50980, 51820, b"one"),
            ipv4("192.168.97.9", 50980, 51820, b"other address"),
            ipv4("192.168.97.3", 50981, 51820, b"other source port"),
            ipv4("192.168.97.3", 50980, 51821, b"other server"),
            ipv4("192.168.97.3", 50980, 51820, b"two", options=bytes(4)),
        ]
        payloads, raw = self.capture(packets)
        self.assertEqual(payloads, [b"one", b"two"])
        self.assertEqual(raw.opened, [(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)])
        self.assertLessEqual(raw.timeout, 10)

    def test_stops_at_the_limit(self):
        packets = [ipv4("192.168.97.3", 50980, 51820, bytes([n])) for n in range(5)]
        payloads, _ = self.capture(packets, limit=3)
        self.assertEqual(payloads, [b"\0", b"\1", b"\2"])

    def test_ipv6_comes_without_an_ip_header(self):
        packets = [(udp(50980, 51820, b"v6"), ("2001:db8::5", 0, 0, 0)), (udp(50980, 51820, b"no"), ("2001:db8::6", 0, 0, 0))]
        payloads, raw = self.capture(packets, src="2001:db8:0::5")
        self.assertEqual(payloads, [b"v6"])
        self.assertEqual(raw.opened[0][0], socket.AF_INET6)

    def test_no_raw_socket_raises(self):
        denied = PermissionError(1, "Operation not permitted")
        with mock.patch.object(probe.socket, "socket", side_effect=denied), self.assertRaises(PermissionError):
            probe.capture(51820, "192.168.97.3", 50980, 10, 64)


class OldConfigTests(unittest.TestCase):
    """T1: a flow left unanswered, refreshed on two ticks running."""

    def test_names_the_client_and_what_differs(self):
        h = Harness()
        h.flows(flow_line(A, 29))
        h.tick(0)
        self.assertEqual(h.captures, [])
        h.flows(flow_line(A, 28))
        h.tick(7)
        self.assertEqual(h.captures, [A])
        self.assertEqual(h.limits, (10, 64))
        self.assertEqual(h.events, [])  # judged on the next tick
        h.flows(flow_line(A, 27))
        h.tick(14)
        expected = {
            "verdict": "old_config",
            "since": 0,
            "last_attempt": 14,
            "attempts": 5,
            "endpoint": "192.168.97.3:50980",
            "country": "US",
            "device": {"S1": 31, "H1": 500284, "trailers": True, "key": "current"},
            "server": {"S1": 40, "H1": "100000-100999", "trailers": False, "key": True},
            "mismatch": ["S1", "H1", "RandomTrailers"],
            "last_handshake": None,
            "params_changed_at": None,
        }
        self.assertEqual(h.probe.diagnosis("a"), expected)
        detail = {key: value for key, value in expected.items() if key != "since"}
        self.assertEqual(h.events, [("client.old_config", "a", detail)])
        self.assertEqual(h.run_calls, [["conntrack", "-L", "-p", "udp"]] * 3)

    def test_a_scanners_single_probe_is_never_captured(self):
        h = Harness()
        for at, timeout in ((0, 29), (7, 22), (14, 16), (21, 8), (28, 1)):  # ageing, never refreshed
            h.flows(flow_line(D, timeout))
            h.tick(at)
        self.assertEqual(h.captures, [])

    def test_two_ticks_running(self):
        h = Harness()
        for at, timeout in ((0, 29), (7, 23), (14, 29)):  # refreshed, not, refreshed
            h.flows(flow_line(A, timeout))
            h.tick(at)
        self.assertEqual(h.captures, [])
        h.flows(flow_line(A, 27))
        h.tick(21)
        self.assertEqual(h.captures, [A])

    def test_replied_flows_and_other_ports_are_not_watched(self):
        h = Harness()
        other = (51822, "192.168.97.8", 40000)
        for at in (0, 7, 14):
            h.flows(flow_line(E, 119, unreplied=False), flow_line(other, 29), flow_line((53, "127.0.0.1", 42276), 29))
            h.tick(at)
        self.assertEqual(h.captures, [])

    def test_the_fixture_one_capture_per_tick(self):
        """conntrack.txt on every tick: A-D refreshed each time, captured one at a time;
        A and B named, C and D (nobody) resting."""
        h = Harness()
        h.conntrack = CONNTRACK_TXT
        for at in range(0, 50, 7):
            h.tick(at)
        self.assertEqual(h.captures, [C, B, A, D])
        self.assertEqual(
            [(event, client) for event, client, _ in h.events], [("client.old_config", "b"), ("client.old_config", "a")]
        )
        b = h.probe.diagnosis("b")
        self.assertEqual(b["mismatch"], ["S1", "H1"])
        self.assertEqual(b["device"], {"S1": 33, "H1": b["device"]["H1"], "trailers": False, "key": "none"})
        self.assertTrue(1500000 <= b["device"]["H1"] <= 1500999)
        self.assertEqual(b["server"], {"S1": 55, "H1": "1100000-1100999", "trailers": False, "key": False})
        self.assertIsNone(h.probe.diagnosis("c"))
        self.assertIsNone(h.probe.diagnosis("e"))


class RestTests(unittest.TestCase):
    def test_a_stranger_rests_five_minutes(self):
        h = Harness()
        timeout = 29
        at = 0
        while at <= 7 + 7 + 300 + 14:
            h.flows(flow_line(D, timeout))  # refreshed on every tick
            h.tick(at)
            at += 7
        # Captured at 7, judged (nobody) at 14, resting until 314; then two ticks
        # running again (counted through the rest) and captured at 315.
        self.assertEqual(h.captures, [D, D])
        self.assertEqual(h.events, [])

    def test_a_suspended_client_is_nobody(self):
        h = Harness()
        h.server("s0")["clients"][0]["suspended"] = True
        for at in (0, 7, 14):
            h.flows(flow_line(A, 29))
            h.tick(at)
        self.assertEqual(h.captures, [A])
        self.assertEqual(h.events, [])
        self.assertIsNone(h.probe.diagnosis("a"))
        for at in (21, 28, 35):
            h.flows(flow_line(A, 29))
            h.tick(at)
        self.assertEqual(h.captures, [A])  # resting

    def test_no_capture_while_its_client_has_a_verdict(self):
        h = Harness()
        for at in range(0, 64, 7):
            h.flows(flow_line(A, 29))
            h.tick(at)
        self.assertEqual(h.captures, [A])
        self.assertEqual(h.probe.diagnosis("a")["last_attempt"], 63)  # still trying
        self.assertEqual(len(h.events), 1)
        h.flows()  # it gave up
        h.tick(70)
        self.assertEqual(h.probe.diagnosis("a")["last_attempt"], 63)

    def test_the_same_device_from_a_new_port_moves_its_verdict(self):
        h = Harness()
        for at in (0, 7, 14):
            h.flows(flow_line(A, 29))
            h.tick(at)
        moved = (51820, "192.168.97.3", 50999)
        h.sends[moved] = sent(A)
        for at in (21, 28, 35):
            h.flows(flow_line(moved, 29))
            h.tick(at)
        self.assertEqual(h.captures, [A, moved])
        self.assertEqual(len(h.events), 1)
        self.assertEqual(h.probe.diagnosis("a")["endpoint"], "192.168.97.3:50999")
        self.assertEqual(h.probe.diagnosis("a")["since"], 0)
        for at in (42, 49):
            h.flows(flow_line(moved, 29))
            h.tick(at)
        self.assertEqual(len(h.captures), 2)


def attempt(h, growth, ticks=3, start=0, name="a", source=A):
    """`ticks` ticks of client `name` (handshake at 1000, never moving) whose rx grows by
    `growth` after each: the first tick is its baseline, each next one grew."""
    h.set_peer(name, endpoint=endpoint(source), handshake_at=1000, rx=5000)
    rx = 5000
    for n in range(ticks):
        h.tick(start + 7 * n)
        rx += growth
        h.set_peer(name, rx=rx)


class MaybeBlockedTests(unittest.TestCase):
    """T2: the server reads the device's initiations (rx grows by 148 each) and none
    completes. The server here runs A's old parameters, so A's capture fits it."""

    def test_named_after_its_handshake_still_has_not_moved(self):
        h = Harness(params0=OLD_A)
        attempt(h, 148 * 2)  # rx 5000 at 0, 5296 at 7, 5592 at 14
        self.assertEqual(h.captures, [A])
        self.assertEqual(h.events, [])
        h.set_peer("a", rx=5592 + 148)
        h.tick(21)
        expected = {
            "verdict": "maybe_blocked",
            "since": 7,
            "last_attempt": 21,
            "attempts": 5,  # (5740 - 5000) / 148
            "endpoint": "192.168.97.3:50980",
            "country": "US",
            "device": {"S1": 31, "H1": 500284, "trailers": True, "key": "current"},
            "server": {"S1": 31, "H1": "500000-500999", "trailers": True, "key": True},
            "mismatch": [],
            "last_handshake": 1000,
            "params_changed_at": None,
        }
        self.assertEqual(h.probe.diagnosis("a"), expected)
        self.assertEqual(h.events, [("client.maybe_blocked", "a", {k: v for k, v in expected.items() if k != "since"})])
        h.set_peer("a", rx=5740 + 296)
        h.tick(28)
        self.assertEqual(h.probe.diagnosis("a")["attempts"], 7)
        self.assertEqual(h.probe.diagnosis("a")["last_attempt"], 28)
        h.tick(35)  # nothing this tick
        self.assertEqual(h.probe.diagnosis("a")["last_attempt"], 28)

    def test_no_verdict_when_it_connects_meanwhile(self):
        h = Harness(params0=OLD_A)
        attempt(h, 148)
        self.assertEqual(h.captures, [A])
        h.set_peer("a", handshake_at=1021)
        h.tick(21)
        self.assertEqual(h.events, [])
        self.assertIsNone(h.probe.diagnosis("a"))

    def test_silent_for_a_busy_session_a_sleeping_phone_and_light_data(self):
        for label, growth in (
            ("busy", 60000),
            ("asleep", 0),
            ("light data", 400),
            ("keepalives", 32),
            ("one data packet", 592 + 32),
        ):
            with self.subTest(label):
                h = Harness(params0=OLD_A)
                attempt(h, growth, ticks=6)
                self.assertEqual(h.captures, [])

    def test_attempt_sized_growth_triggers(self):
        for growth in (148, 444, 148 * 13):
            with self.subTest(growth=growth):
                h = Harness(params0=OLD_A)
                attempt(h, growth)
                self.assertEqual(h.captures, [A])

    def test_one_attempt_tick_is_not_enough(self):
        h = Harness(params0=OLD_A)
        h.set_peer("a", endpoint=endpoint(A), handshake_at=1000, rx=5000)
        for at, rx in ((0, 5000), (7, 5148), (14, 5148), (21, 5296), (28, 5296)):
            h.set_peer("a", rx=rx)
            h.tick(at)
        self.assertEqual(h.captures, [])

    def test_trailers_on_the_server_and_off_on_the_device_is_old_config(self):
        """F5: the server reads B's initiations (its trailers are optional), B drops the
        longer responses: it looks blocked, the capture shows no trailers on 5."""
        h = Harness(params1=OLD_B_TRAILERS)
        attempt(h, 148, name="b", source=B)
        h.tick(21)
        diagnosis = h.probe.diagnosis("b")
        self.assertEqual(diagnosis["verdict"], "old_config")
        self.assertEqual(diagnosis["mismatch"], ["RandomTrailers"])
        self.assertEqual((diagnosis["device"]["trailers"], diagnosis["server"]["trailers"]), (False, True))
        self.assertEqual(diagnosis["attempts"], 5)
        self.assertEqual(diagnosis["since"], 7)

    def test_the_key_decides_who_is_named(self):
        h = Harness(params0=OLD_A)
        h.sends[E] = sent(A)  # E's endpoint, A's initiations (a shared NAT address, say)
        h.set_peer("a", handshake_at=1000, rx=9000)
        attempt(h, 148, name="e", source=E)
        h.tick(21)
        self.assertEqual(h.captures, [E])
        self.assertIsNone(h.probe.diagnosis("e"))
        self.assertEqual(h.probe.diagnosis("a")["verdict"], "maybe_blocked")
        self.assertEqual([client for _, client, _ in h.events], ["a"])

    def test_a_suspended_client_is_not_watched(self):
        h = Harness(params0=OLD_A)
        h.server("s0")["clients"][0]["suspended"] = True
        attempt(h, 148)
        self.assertEqual(h.captures, [])

    def test_params_changed_after_the_last_handshake(self):
        for changed_at, expected in ((2000, 2000), (900, None), (None, None)):
            with self.subTest(changed_at=changed_at):
                h = Harness(params0=OLD_A)
                h.server("s0")["transport_changed_at"] = changed_at
                attempt(h, 148)
                h.tick(21)
                self.assertEqual(h.probe.diagnosis("a")["params_changed_at"], expected)

    def test_never_connected_and_params_changed(self):
        h = Harness()
        h.server("s0")["transport_changed_at"] = 2000
        for at in (0, 7, 14):
            h.flows(flow_line(A, 29))
            h.tick(at)
        self.assertEqual(h.probe.diagnosis("a")["params_changed_at"], 2000)


class EndTests(unittest.TestCase):
    def old_config(self):
        h = Harness()
        for at in (0, 7, 14):
            h.flows(flow_line(A, 29))
            h.tick(at)
        self.assertIsNotNone(h.probe.diagnosis("a"))
        h.events.clear()
        return h

    def test_a_moved_handshake_recovers(self):
        h = self.old_config()
        h.set_peer("a", handshake_at=1021)
        h.tick(21)
        self.assertIsNone(h.probe.diagnosis("a"))
        self.assertEqual(h.events, [("client.recovered", "a", {"verdict": "old_config", "duration_s": 21})])

    def test_a_peer_missing_from_the_dump_is_not_a_recovery(self):
        h = self.old_config()
        del h.peers["awg0"][PUB["A"]]
        h.tick(21)
        self.assertIsNotNone(h.probe.diagnosis("a"))
        self.assertEqual(h.events, [])

    def test_ends_silently(self):
        def suspend(h):
            h.server("s0")["clients"][0]["suspended"] = True

        def delete(h):
            del h.server("s0")["clients"][0]

        def stop(h):
            del h.peers["awg0"]

        for change in (suspend, delete, stop):
            with self.subTest(change.__name__):
                h = self.old_config()
                change(h)
                h.tick(21)
                self.assertIsNone(h.probe.diagnosis("a"))
                self.assertEqual(h.events, [])

    def test_a_capture_whose_server_stopped_judges_nothing(self):
        h = Harness()
        h.inline = False
        for at in (0, 7):
            h.flows(flow_line(A, 29))
            h.tick(at)
        h.held.pop()()
        del h.peers["awg0"]
        h.tick(14)
        self.assertEqual(h.events, [])
        self.assertIsNone(h.probe.diagnosis("a"))


class CaptureFlowTests(unittest.TestCase):
    def test_one_capture_at_a_time(self):
        h = Harness()
        h.inline = False
        for at in (0, 7):
            h.flows(flow_line(A, 29), flow_line(C, 29))
            h.tick(at)
        self.assertEqual((h.captures, len(h.held)), ([], 1))
        h.flows(flow_line(A, 29), flow_line(C, 29))
        h.tick(14)  # the first still running
        self.assertEqual(len(h.held), 1)
        h.held.pop()()
        self.assertEqual(h.captures, [A])
        h.flows(flow_line(A, 29), flow_line(C, 29))
        h.tick(21)  # judges A, starts C
        self.assertEqual(h.probe.diagnosis("a")["verdict"], "old_config")
        self.assertEqual(len(h.held), 1)
        h.held.pop()()
        self.assertEqual(h.captures, [A, C])

    def test_without_conntrack_t1_is_off_and_t2_still_works(self):
        h = Harness(params0=OLD_A)
        h.conntrack = None
        with self.assertLogs("services.probe", "WARNING") as logs:
            attempt(h, 148)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(h.run_calls), 1)
        self.assertEqual(h.captures, [A])

    def test_no_conntrack_call_without_a_running_server(self):
        h = Harness()
        h.peers = {}
        h.tick(0)
        self.assertEqual(h.run_calls, [])

    def test_without_a_raw_socket_the_probe_is_off(self):
        h = Harness()
        h.capture_error = PermissionError(1, "Operation not permitted")
        with self.assertLogs("services.probe", "WARNING") as logs:
            for at in (0, 7, 14, 21):
                h.flows(flow_line(A, 29))
                h.tick(at)
        self.assertEqual(len(logs.records), 1)
        self.assertFalse(h.probe.enabled)
        self.assertEqual(h.captures, [A])
        self.assertEqual(len(h.run_calls), 2)  # nothing after the capture failed
        self.assertEqual(h.events, [])

    def test_a_failed_capture_is_logged_and_rests(self):
        h = Harness()
        h.capture_error = OSError("boom")
        with self.assertLogs("services.probe", "WARNING"):
            for at in (0, 7, 14, 21, 28):
                h.flows(flow_line(A, 29))
                h.tick(at)
        self.assertTrue(h.probe.enabled)
        self.assertEqual(h.captures, [A])

    def test_a_failed_read_is_not_a_tick(self):
        h = Harness()
        h.flows(flow_line(A, 29))
        for at in (0, 7, 14):
            h.tick(at, read=False)
        self.assertEqual((h.run_calls, h.captures), ([], []))

    def test_diagnosis_is_a_copy_in_the_contract_shape(self):
        h = Harness()
        for at in (0, 7, 14):
            h.flows(flow_line(A, 29))
            h.tick(at)
        diagnosis = h.probe.diagnosis("a")
        assert diagnosis is not None
        self.assertEqual(set(diagnosis), DIAGNOSIS_KEYS)
        diagnosis["verdict"] = "changed"
        self.assertEqual(h.probe.diagnosis("a")["verdict"], "old_config")
        self.assertIsNone(h.probe.diagnosis("nobody"))


if __name__ == "__main__":
    unittest.main()
