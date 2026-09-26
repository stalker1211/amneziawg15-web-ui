"""Tests for the manager's edges: subprocesses, interfaces, iptables, and the network.

tests/support.py's build_manager() overrides this whole layer so the logic tests stay
hermetic. Here the production code runs instead, with only `subprocess.run` (a
FakeSubprocess), two system paths (SystemPaths) and `requests` replaced -- so the argv
each operation produces, and what it does when a command fails, are pinned down.
"""

import ast
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.support import (
    PRESHARED_KEY,
    SERVER_PRIVATE_KEY,
    SERVER_PUBLIC_KEY,
    WEB_UI_DIR,
    FakeSocketIO,
    SystemPaths,
    build_real_manager,
)

MODULE = "services.amnezia_manager"


class _Base(unittest.TestCase):
    def setUp(self):
        self.paths = SystemPaths().start(self)
        self.manager, self.fake = build_real_manager(self)

    def _server(self, name="edge", subnet="10.50.0.0/24", port=51950, **extra):
        data = {"name": name, "protocol": "AWG 2.0", "subnet": subnet, "port": port, "auto_start": False}
        data.update(extra)
        server = self.manager.create_wireguard_server(data)
        self.fake.calls.clear()
        return server

    def _saved(self):
        with open(self.manager.config_file, encoding="utf-8") as f:
            return json.load(f)


class RunCommandTests(_Base):
    def test_returns_stripped_stdout(self):
        self.fake.respond(["awg", "show"], "  interface: wg0 \n")
        self.assertEqual(self.manager.run_command(["awg", "show"]), "interface: wg0")
        call = self.fake.calls[-1]
        self.assertEqual(call["args"], ["awg", "show"])
        self.assertTrue(call["check"])
        self.assertNotIn("shell", call)

    def test_non_zero_exit_returns_none_and_logs_error(self):
        self.fake.respond(["awg", "show"], 1)
        with self.assertLogs(MODULE, "ERROR"):
            self.assertIsNone(self.manager.run_command(["awg", "show"]))

    def test_missing_binary_returns_none(self):
        self.fake.respond(["awg"], FileNotFoundError("awg"))
        with self.assertLogs(MODULE, "ERROR"):
            self.assertIsNone(self.manager.run_command(["awg", "show"]))


class KeyGenerationTests(_Base):
    def test_keypair_derives_public_key_over_stdin(self):
        keys = self.manager.generate_wireguard_keys()
        self.assertEqual(keys, {"private_key": SERVER_PRIVATE_KEY, "public_key": SERVER_PUBLIC_KEY})
        pubkey_call = next(c for c in self.fake.calls if c["args"] == ["wg", "pubkey"])
        self.assertEqual(pubkey_call["input"], SERVER_PRIVATE_KEY)
        # The private key must never appear in an argv (visible in the process list).
        for argv in self.fake.argvs():
            self.assertNotIn(SERVER_PRIVATE_KEY, argv)

    def test_failed_genkey_or_pubkey_raises_instead_of_inventing_a_pair(self):
        self.fake.respond(["wg", "genkey"], 1)
        with self.assertLogs(MODULE, "ERROR"), self.assertRaises(RuntimeError):
            self.manager.generate_wireguard_keys()

        self.fake.respond(["wg", "genkey"], SERVER_PRIVATE_KEY).respond(["wg", "pubkey"], 1)
        with self.assertLogs(MODULE, "ERROR"), self.assertRaises(RuntimeError):
            self.manager.generate_wireguard_keys()

    def test_preshared_key(self):
        self.assertEqual(self.manager.generate_preshared_key(), PRESHARED_KEY)
        self.fake.respond(["wg", "genpsk"], 1)
        with self.assertLogs(MODULE, "ERROR"), self.assertRaises(RuntimeError):
            self.manager.generate_preshared_key()

    def test_header_protection_key_falls_back_to_random_bytes(self):
        self.assertEqual(self.manager.generate_header_protection_key(), SERVER_PRIVATE_KEY)

        self.fake.respond(["wg", "genkey"], "not-a-key")
        malformed = self.manager.generate_header_protection_key()
        self.fake.respond(["wg", "genkey"], 1)
        with self.assertLogs(MODULE, "ERROR"):
            failed = self.manager.generate_header_protection_key()

        for key in (malformed, failed):
            self.assertTrue(self.manager.is_valid_wireguard_key(key), key)
        self.assertEqual(len({malformed, failed, SERVER_PRIVATE_KEY}), 3)


class StartStopTests(_Base):
    def test_start_brings_the_interface_up_then_applies_iptables(self):
        server = self._server(enable_nat=False, block_lan_cidrs=True)
        self.assertTrue(self.manager.start_server(server["id"]))

        up, setup = self.fake.calls
        self.assertEqual(up["args"], ["/usr/bin/awg-quick", "up", server["interface"]])
        self.assertEqual(setup["args"], ["/app/scripts/setup_iptables.sh", server["interface"], server["subnet"]])
        self.assertEqual((setup["env"]["ENABLE_NAT"], setup["env"]["BLOCK_LAN_CIDRS"]), ("0", "1"))

        self.assertEqual(self._saved()["servers"][0]["status"], "running")
        self.assertIn(("server_status", {"server_id": server["id"], "status": "running"}), self.manager.socketio.emitted)

    def test_failed_start_skips_iptables_and_keeps_status(self):
        server = self._server()
        self.fake.respond(["/usr/bin/awg-quick", "up"], 1)
        with self.assertLogs(MODULE, "ERROR"):
            self.assertFalse(self.manager.start_server(server["id"]))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertNotEqual(self.manager.get_server(server["id"])["status"], "running")

    def test_start_still_succeeds_when_the_iptables_script_is_missing(self):
        server = self._server()
        self.paths.scripts = False
        with self.assertLogs(MODULE, "WARNING"):
            self.assertTrue(self.manager.start_server(server["id"]))
        self.assertEqual(self.fake.argvs(), [["/usr/bin/awg-quick", "up", server["interface"]]])

    def test_stop_cleans_iptables_before_taking_the_interface_down(self):
        server = self._server(enable_nat=True, block_lan_cidrs=False)
        self.assertTrue(self.manager.stop_server(server["id"]))

        cleanup, down = self.fake.calls
        self.assertEqual(cleanup["args"], ["/app/scripts/cleanup_iptables.sh", server["interface"], server["subnet"]])
        self.assertEqual((cleanup["env"]["ENABLE_NAT"], cleanup["env"]["BLOCK_LAN_CIDRS"]), ("1", "0"))
        self.assertEqual(down["args"], ["/usr/bin/awg-quick", "down", server["interface"]])
        self.assertEqual(self._saved()["servers"][0]["status"], "stopped")

    def test_failed_stop_returns_false(self):
        server = self._server()
        self.fake.respond(["/usr/bin/awg-quick", "down"], 1)
        with self.assertLogs(MODULE, "ERROR"):
            self.assertFalse(self.manager.stop_server(server["id"]))

    def test_unknown_server(self):
        self.assertFalse(self.manager.start_server("nope"))
        self.assertFalse(self.manager.stop_server("nope"))
        self.assertEqual(self.fake.calls, [])

    def test_reapply_iptables_cleans_up_then_sets_up_with_the_servers_toggles(self):
        server = self._server(enable_nat=False, block_lan_cidrs=False)
        self.assertTrue(self.manager.reapply_iptables_for_server(server))
        self.assertEqual(
            [c["args"][0] for c in self.fake.calls], ["/app/scripts/cleanup_iptables.sh", "/app/scripts/setup_iptables.sh"]
        )
        for call in self.fake.calls:
            self.assertEqual((call["env"]["ENABLE_NAT"], call["env"]["BLOCK_LAN_CIDRS"]), ("0", "0"))
        self.assertFalse(self.manager.reapply_iptables_for_server(None))


class IptablesScriptTests(_Base):
    def test_unset_toggles_leave_the_environment_alone(self):
        with mock.patch.dict(os.environ, {"ENABLE_NAT": "from-env", "BLOCK_LAN_CIDRS": "from-env"}):
            self.assertTrue(self.manager.setup_iptables("wg-x", "10.9.0.0/24"))
        env = self.fake.calls[-1]["env"]
        self.assertEqual((env["ENABLE_NAT"], env["BLOCK_LAN_CIDRS"]), ("from-env", "from-env"))

    def test_script_failure_returns_false(self):
        self.fake.respond(["/app/scripts/cleanup_iptables.sh"], 2)
        with self.assertLogs(MODULE, "ERROR"):
            self.assertFalse(self.manager.cleanup_iptables("wg-x", "10.9.0.0/24", enable_nat=True))


class LiveConfigTests(_Base):
    def test_strip_is_synced_through_a_temp_file_that_is_removed(self):
        seen = {}

        def syncconf(args, _kwargs):
            seen["path"] = args[3]
            seen["content"] = Path(args[3]).read_text(encoding="utf-8")
            return ""

        self.fake.respond(["awg-quick", "strip", "wg-x"], "[Interface]\nListenPort = 51820")
        self.fake.respond(["awg", "syncconf", "wg-x"], syncconf)

        self.assertTrue(self.manager.apply_live_config("wg-x"))
        self.assertEqual(seen["content"], "[Interface]\nListenPort = 51820\n")
        self.assertFalse(os.path.exists(seen["path"]))

    def test_failed_strip_does_not_sync(self):
        self.fake.respond(["awg-quick", "strip"], 1)
        with self.assertLogs(MODULE, "ERROR"):
            self.assertFalse(self.manager.apply_live_config("wg-x"))
        self.assertEqual(len(self.fake.calls), 1)

    def test_failed_sync_still_removes_the_temp_file(self):
        seen = {}

        def failing(args, _kwargs):
            seen["path"] = args[3]
            return 1

        self.fake.respond(["awg", "syncconf"], failing)
        with self.assertLogs(MODULE, "ERROR"):
            self.assertFalse(self.manager.apply_live_config("wg-x"))
        self.assertFalse(os.path.exists(seen["path"]))


class ServerStatusTests(_Base):
    def test_absent_interface_is_stopped_without_spawning_anything(self):
        server = self._server()
        self.assertEqual(self.manager.get_server_status(server["id"]), "stopped")
        self.assertEqual(self.fake.calls, [])

    def test_present_interface_is_checked_with_ip_link(self):
        server = self._server()
        self.paths.interfaces.add(server["interface"])
        self.fake.respond(["ip", "link", "show"], f"5: {server['interface']}: <POINTOPOINT,UP> mtu 1420 state UNKNOWN")
        self.assertEqual(self.manager.get_server_status(server["id"]), "running")
        self.assertEqual(self.fake.argvs(), [["ip", "link", "show", server["interface"]]])

        self.fake.respond(["ip", "link", "show"], f"5: {server['interface']}: <POINTOPOINT> mtu 1420 state DOWN")
        self.assertEqual(self.manager.get_server_status(server["id"]), "stopped")

    def test_unknown_server(self):
        self.assertEqual(self.manager.get_server_status("nope"), "not_found")


class AutoStartTests(_Base):
    def test_starts_only_eligible_servers_and_survives_a_failure(self):
        wanted = self._server("wanted", "10.51.0.0/24", 51951, auto_start=False)
        opted_out = self._server("opted-out", "10.52.0.0/24", 51952)
        no_conf = self._server("no-conf", "10.53.0.0/24", 51953)
        broken = self._server("broken", "10.54.0.0/24", 51954)
        for server in (wanted, no_conf, broken):
            self.manager.get_server(server["id"])["auto_start"] = True
        self.manager.get_server(opted_out["id"])["auto_start"] = False
        os.remove(no_conf["config_path"])
        self.fake.respond(["/usr/bin/awg-quick", "up", broken["interface"]], 1)

        with self.assertLogs(MODULE, "INFO"):
            self.manager.auto_start_servers()

        brought_up = [argv[2] for argv in self.fake.argvs() if argv[:2] == ["/usr/bin/awg-quick", "up"]]
        self.assertEqual(sorted(brought_up), sorted([wanted["interface"], broken["interface"]]))
        self.assertEqual(self.manager.get_server(wanted["id"])["status"], "running")


class StopLoop(BaseException):
    """Escapes the traffic loop: it catches Exception, not BaseException."""


class BackgroundTaskTests(_Base):
    def test_traffic_loop_emits_only_for_running_servers(self):
        from services.amnezia_manager import AmneziaManager

        up = self._server("up", "10.55.0.0/24", 51955)
        self._server("down", "10.56.0.0/24", 51956)
        self.paths.interfaces.add(up["interface"])
        self.fake.respond(["ip", "link", "show"], "state UNKNOWN")

        socketio = FakeSocketIO(run_tasks=True)
        socketio.sleep = mock.Mock(side_effect=StopLoop)
        self.manager.socketio = socketio
        with (
            mock.patch.object(self.manager, "get_traffic_for_server", return_value={"clients": {}}) as traffic,
            self.assertRaises(StopLoop),
        ):
            AmneziaManager.start_traffic_monitoring(self.manager)

        traffic.assert_called_once_with(up["id"])
        self.assertEqual(socketio.emitted, [("traffic_update", {"server_id": up["id"], "traffic": {"clients": {}}})])
        socketio.sleep.assert_called_once_with(7)

    def test_status_is_emitted_after_the_delay(self):
        self.manager.emit_status_after_delay("abc", "running", delay_seconds=3)
        self.assertEqual(self.manager.socketio.slept[-1], 3)
        self.assertEqual(self.manager.socketio.emitted[-1], ("server_status", {"server_id": "abc", "status": "running"}))


class _Response:
    def __init__(self, status=200, text="", data=None, content_type="application/json"):
        self.status_code = status
        self.text = text
        self._data = data
        self.headers = {"content-type": content_type}

    def json(self):
        return self._data


class PublicIpTests(_Base):
    def _detect(self):
        from services.amnezia_manager import AmneziaManager

        return AmneziaManager.detect_public_ip(self.manager)

    def test_first_valid_answer_wins(self):
        answers = {
            "http://ifconfig.me": _Response(500),
            "https://api.ipify.org": _Response(text="<html>not an ip</html>"),
            "https://ident.me": _Response(text="198.51.100.7\n"),
        }
        with (
            mock.patch(f"{MODULE}.requests.get", side_effect=lambda url, timeout: answers[url]),
            self.assertLogs(MODULE, "INFO"),
        ):
            self.assertEqual(self._detect(), "198.51.100.7")

    def test_falls_back_to_the_routing_source_address(self):
        self.fake.respond(["ip", "route", "get"], "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.5 uid 0")
        with mock.patch(f"{MODULE}.requests.get", side_effect=OSError("offline")), self.assertLogs(MODULE, "INFO"):
            self.assertEqual(self._detect(), "192.168.1.5")

    def test_placeholder_when_nothing_works(self):
        self.fake.respond(["ip", "route", "get"], 2)
        with mock.patch(f"{MODULE}.requests.get", side_effect=OSError("offline")), self.assertLogs(MODULE, "ERROR"):
            self.assertEqual(self._detect(), "YOUR_SERVER_IP")


class GeoIpTests(_Base):
    def _lookup(self, ip, response=None, side_effect=None):
        with mock.patch(f"{MODULE}.requests.get", return_value=response, side_effect=side_effect) as get:
            return self.manager.lookup_geoip(ip), get

    def test_non_public_or_invalid_addresses_are_not_looked_up(self):
        for ip in ("10.0.0.1", "127.0.0.1", "192.168.1.1", "203.0.113.9", "not-an-ip", "", None, 42):
            result, get = self._lookup(ip)
            self.assertEqual(result, (None, None), ip)
            get.assert_not_called()

    def test_label_and_country_code_shapes(self):
        cases = [
            ({"country_name": "Netherlands", "city": "Amsterdam", "region": "North Holland", "country_code": "NL"},
             ("Netherlands / Amsterdam, North Holland", "NL")),
            ({"country": "CH"}, ("CH", "CH")),
            ({"city": "Zurich"}, ("Zurich", None)),
            ({"countryCode": "de", "regionName": "Bavaria"}, ("de / Bavaria", "DE")),
            ({"country_code": "Germany"}, (None, None)),
            ({}, (None, None)),
        ]  # fmt: skip
        for n, (data, expected) in enumerate(cases):
            result, _ = self._lookup(f"8.8.8.{n}", _Response(data=data))
            self.assertEqual(result, expected, data)

    def test_non_json_answer_has_no_label(self):
        result, _ = self._lookup("8.8.4.4", _Response(text="<html>", content_type="text/html"))
        self.assertEqual(result, (None, None))

    def test_answers_and_failures_are_cached(self):
        for kwargs in (
            {"response": _Response(429)},
            {"side_effect": OSError("down")},
            {"response": _Response(data={"country": "NL"})},
        ):
            ip = "9.9.9.9"
            self.manager._geoip_cache.clear()
            first, _ = self._lookup(ip, **kwargs)
            second, get = self._lookup(ip, response=_Response(data={"country": "FR"}))
            self.assertEqual(second, first)
            get.assert_not_called()

    def test_disabled(self):
        self.manager.enable_geoip = False
        result, get = self._lookup("8.8.8.8", _Response(data={"country": "US"}))
        self.assertEqual(result, (None, None))
        get.assert_not_called()


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.mounted = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def mount(self, prefix, adapter):
        self.mounted.append((prefix, adapter._source_ip))

    def get(self, url, timeout):
        if self.error:
            raise self.error
        return self.response


class EgressProbeTests(_Base):
    def test_source_and_service_are_validated(self):
        with self.assertRaises(ValueError):
            self.manager.detect_public_ip_from_source("not-an-ip", "https://api.ipify.org")
        with self.assertRaises(ValueError):
            self.manager.detect_public_ip_from_source("10.0.0.1", "https://evil.example")

    def test_request_is_bound_to_the_source_address(self):
        session = _Session(_Response(text="198.51.100.9\n"))
        with mock.patch(f"{MODULE}.requests.Session", return_value=session):
            result = self.manager.detect_public_ip_from_source("10.0.0.1", "https://ident.me")
        self.assertEqual(result, ("198.51.100.9", "https://ident.me"))
        self.assertEqual(session.mounted, [("http://", "10.0.0.1"), ("https://", "10.0.0.1")])

    def test_bad_answers_raise(self):
        for session in (_Session(_Response(503)), _Session(_Response(text="nope")), _Session(error=OSError("x"))):
            with mock.patch(f"{MODULE}.requests.Session", return_value=session), self.assertRaises(RuntimeError):
                self.manager.detect_public_ip_from_source("10.0.0.1", "https://ident.me")

    def test_service_rotation(self):
        services = list(self.manager.EGRESS_PROBE_SERVICES)
        self.assertEqual(self.manager.get_next_egress_probe_service({}), services[0])
        for n, previous in enumerate(services):
            server = {"egress_probe": {"service": previous}}
            self.assertEqual(self.manager.get_next_egress_probe_service(server), services[(n + 1) % len(services)])
        self.assertEqual(self.manager.get_next_egress_probe_service({"egress_probe": {"service": "gone"}}), services[0])

    def test_service_name(self):
        self.assertEqual(self.manager.format_probe_service_name("https://api.ipify.org"), "api.ipify.org")
        self.assertEqual(self.manager.format_probe_service_name("ident.me"), "ident.me")
        self.assertIsNone(self.manager.format_probe_service_name(""))
        self.assertIsNone(self.manager.format_probe_service_name(None))

    def test_route_lookup(self):
        self.fake.respond(["ip", "route", "get", "1.1.1.1", "from", "10.0.0.1"],
                          "1.1.1.1 from 10.0.0.1 via 192.168.1.1 dev eth0 src 10.0.0.1 uid 0\n    cache")  # fmt: skip
        route = self.manager.get_route_for_source_ip("10.0.0.1")
        self.assertEqual((route["dev"], route["via"], route["src"]), ("eth0", "192.168.1.1", "10.0.0.1"))
        self.assertNotIn("cache", route["raw"])

        self.fake.calls.clear()
        self.assertIsNone(self.manager.get_route_for_source_ip("bogus")["dev"])
        self.assertEqual(self.fake.calls, [])

        self.fake.respond(["ip", "route"], 2)
        self.assertTrue(self.manager.get_route_for_source_ip("10.0.0.1")["raw"].startswith("route lookup failed"))

    def test_probe_is_recorded_and_persisted(self):
        server = self._server()
        with (
            mock.patch.object(self.manager, "detect_public_ip_from_source", return_value=("198.51.100.9", "https://ident.me")),
            mock.patch.object(self.manager, "lookup_geoip", return_value=("Netherlands", "NL")),
        ):
            probe = self.manager.probe_server_egress_ip(server["id"])
        self.assertEqual((probe["external_ip"], probe["service"], probe["error"]), ("198.51.100.9", "https://ident.me", None))
        self.assertEqual(probe["external_ip_geo_country_code"], "NL")
        self.assertEqual(self._saved()["servers"][0]["egress_probe"]["external_ip"], "198.51.100.9")

    def test_failed_probe_records_the_error(self):
        server = self._server()
        with mock.patch.object(self.manager, "detect_public_ip_from_source", side_effect=RuntimeError("ident.me: timeout")):
            probe = self.manager.probe_server_egress_ip(server["id"])
        self.assertEqual((probe["external_ip"], probe["error"]), (None, "ident.me: timeout"))
        self.assertEqual(self._saved()["servers"][0]["egress_probe"]["error"], "ident.me: timeout")
        self.assertIsNone(self.manager.probe_server_egress_ip("nope"))


class NoShellTests(unittest.TestCase):
    """CLAUDE.md: no shell anywhere in the backend. Subnet, port and interface come
    from API input, so a string command line would be an injection path."""

    SUBPROCESS_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
    FORBIDDEN = frozenset({("os", "system"), ("os", "popen"), ("subprocess", "getoutput"), ("subprocess", "getstatusoutput")})

    def test_no_shell_in_the_backend(self):
        problems = []
        for path in Path(WEB_UI_DIR).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
                where = f"{path.relative_to(WEB_UI_DIR)}:{node.lineno}"
                if (owner, node.func.attr) in self.FORBIDDEN:
                    problems.append(f"{where} {owner}.{node.func.attr}")
                if any(
                    k.arg == "shell" and not (isinstance(k.value, ast.Constant) and k.value.value is False)
                    for k in node.keywords
                ):
                    problems.append(f"{where} shell=")
                string_command = node.args and isinstance(node.args[0], (ast.Constant, ast.JoinedStr, ast.BinOp))
                if owner == "subprocess" and node.func.attr in self.SUBPROCESS_CALLS and string_command:
                    problems.append(f"{where} string command")
        self.assertEqual(problems, [])

    def test_the_guard_sees_the_known_call_sites(self):
        # If this drops to zero the scan above has silently stopped matching anything.
        sites = [p for p in Path(WEB_UI_DIR).rglob("*.py") if "subprocess." in p.read_text(encoding="utf-8")]
        self.assertGreaterEqual(len(sites), 2)


if __name__ == "__main__":
    unittest.main()
