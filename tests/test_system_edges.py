"""Tests for the manager's edges: subprocesses, interfaces, iptables, and the network.

tests/support.py's build_manager() overrides this whole layer so the logic tests stay
hermetic. Here the production code runs instead, with only `subprocess.run` (a
FakeSubprocess), two system paths (SystemPaths) and `https_get` replaced -- so the argv
each operation produces, and what it does when a command fails, are pinned down.
"""

import ast
import json
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.support import (
    PRESHARED_KEY,
    PUBLIC_IP,
    SERVER_PRIVATE_KEY,
    SERVER_PUBLIC_KEY,
    WEB_UI_DIR,
    FakeEvents,
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

        up, setup, dump = self.fake.calls
        self.assertEqual(up["args"], ["/usr/bin/awg-quick", "up", server["interface"]])
        # The status push after a start reads telemetry first, so the reload sees it.
        self.assertEqual(dump["args"], ["/usr/bin/awg", "show", "all", "dump"])
        self.assertEqual(setup["args"], ["/app/scripts/setup_iptables.sh", server["interface"], server["subnet"]])
        self.assertEqual((setup["env"]["ENABLE_NAT"], setup["env"]["BLOCK_LAN_CIDRS"]), ("0", "1"))

        self.assertEqual(self._saved()["servers"][0]["status"], "running")
        self.assertIn(("server_status", {"server_id": server["id"], "status": "running"}), self.manager.events.published)

    def test_the_panels_log_level_never_reaches_the_daemon(self):
        # With any LOG_LEVEL the daemon keeps run_command's stdout pipe open and
        # `awg-quick up` never returns.
        server = self._server()
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "DEBUG", "WG_QUICK_USERSPACE_IMPLEMENTATION": "/x"}):
            self.assertTrue(self.manager.start_server(server["id"]))
        env = self.fake.calls[0]["env"]
        self.assertNotIn("LOG_LEVEL", env)
        self.assertNotIn("WG_QUICK_USERSPACE_IMPLEMENTATION", env)
        self.assertIn("PATH", env)

    def test_daemon_logging_goes_to_awg_quick_up_only(self):
        server = self._server()
        self.manager.awg_log_level = "debug"
        self.assertTrue(self.manager.start_server(server["id"]))
        up, setup, _dump = self.fake.calls
        self.assertEqual(up["env"]["LOG_LEVEL"], "debug")
        self.assertEqual(up["env"]["WG_QUICK_USERSPACE_IMPLEMENTATION"], "/usr/local/bin/amneziawg-go-logged")
        self.assertEqual(setup["args"][0], "/app/scripts/setup_iptables.sh")

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
        self.assertEqual(
            self.fake.argvs(), [["/usr/bin/awg-quick", "up", server["interface"]], ["/usr/bin/awg", "show", "all", "dump"]]
        )

    def test_stop_cleans_iptables_before_taking_the_interface_down(self):
        server = self._server(enable_nat=True, block_lan_cidrs=False)
        self.assertTrue(self.manager.stop_server(server["id"]))

        cleanup, down, _dump = self.fake.calls
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


class IptablesRuleTests(unittest.TestCase):
    """The scripts themselves, run with a stand-in iptables that records every call
    (and fails each -D, which ends cleanup's delete-until-gone loops)."""

    SCRIPTS = Path(WEB_UI_DIR).parent / "scripts"
    PANEL_DROP = "-A INPUT -i wg-t -p tcp --dport 8080 -m comment --comment awg:wg-t -j DROP"
    ACCEPT = "-A INPUT -i wg-t -m comment --comment awg:wg-t -j ACCEPT"

    def _run(self, script, block_lan="1", nat="1"):
        tmp = Path(tempfile.mkdtemp(prefix="awg-ipt-"))
        log = tmp / "calls"
        fake = tmp / "iptables"
        fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\ncase " $* " in *" -D "*) exit 1;; esac\n', encoding="utf-8")
        fake.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "WAN_IF": "eth0",
            "NGINX_PORT": "8080",
            "BLOCK_LAN_CIDRS": block_lan,
            "ENABLE_NAT": nat,
        }
        # Through bash: the repo keeps the scripts at 644; the image makes them executable.
        subprocess.run(["bash", str(self.SCRIPTS / script), "wg-t", "10.77.0.0/24"], env=env, check=True, capture_output=True)
        calls = log.read_text(encoding="utf-8").splitlines()
        return [c for c in calls if " -A " in f" {c} "], [c for c in calls if " -D " in f" {c} "]

    def test_block_lan_drops_the_panel_ahead_of_the_tunnels_accept(self):
        added, _ = self._run("setup_iptables.sh")
        self.assertLess(added.index(self.PANEL_DROP), added.index(self.ACCEPT))
        self.assertEqual(len(added), 4 + 4 + 1)  # what /api/system/iptables-test expects

    def test_without_block_lan_the_panel_stays_reachable_through_the_tunnel(self):
        added, _ = self._run("setup_iptables.sh", block_lan="0", nat="0")
        self.assertNotIn(self.PANEL_DROP, added)
        self.assertEqual(len(added), 4)

    def test_cleanup_deletes_each_rule_by_the_spec_setup_added(self):
        # -D matches only an identical spec: a rule spelled differently would stay.
        added, _ = self._run("setup_iptables.sh")
        _, deleted = self._run("cleanup_iptables.sh")
        self.assertEqual({rule.replace("-A ", "-D ", 1) for rule in added}, set(deleted))


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

    def test_status_is_the_sysfs_operstate(self):
        server = self._server()
        self.paths.interfaces.add(server["interface"])
        self.assertEqual(self.manager.get_server_status(server["id"]), "running")
        self.paths.states[server["interface"]] = "up"
        self.assertEqual(self.manager.get_server_status(server["id"]), "running")
        self.paths.states[server["interface"]] = "down"
        self.assertEqual(self.manager.get_server_status(server["id"]), "stopped")
        self.assertEqual(self.fake.calls, [])

    def test_unknown_server(self):
        self.assertEqual(self.manager.get_server_status("nope"), "not_found")


class AutoStartTests(_Base):
    def test_boot_restores_each_servers_last_state_and_survives_a_failure(self):
        # `status` is the last start/stop; until 2.4 a creation-time flag decided.
        was_running = self._server("was-running", "10.51.0.0/24", 51951)
        stopped = self._server("stopped-by-hand", "10.52.0.0/24", 51952)
        no_conf = self._server("no-conf", "10.53.0.0/24", 51953)
        broken = self._server("broken", "10.54.0.0/24", 51954)
        already_up = self._server("already-up", "10.57.0.0/24", 51957)
        for server in (was_running, no_conf, broken, already_up):
            self.manager.get_server(server["id"])["status"] = "running"
        self.manager.get_server(stopped["id"])["status"] = "stopped"
        os.remove(no_conf["config_path"])
        self.paths.interfaces.add(already_up["interface"])
        self.fake.respond(["/usr/bin/awg-quick", "up", broken["interface"]], 1)
        self.fake.calls.clear()

        with self.assertLogs(MODULE, "INFO"):
            self.manager.auto_start_servers()

        brought_up = [argv[2] for argv in self.fake.argvs() if argv[:2] == ["/usr/bin/awg-quick", "up"]]
        self.assertEqual(sorted(brought_up), sorted([was_running["interface"], broken["interface"]]))
        self.assertEqual(self.manager.get_server(stopped["id"])["status"], "stopped")

    def test_the_old_creation_flag_is_dropped_on_load(self):
        from services.amnezia_manager import AmneziaManager

        config = AmneziaManager.migrate_config_schema(self.manager, {"servers": [{"id": "x", "auto_start": True}]})
        self.assertNotIn("auto_start", config["servers"][0])


class StopLoop(BaseException):
    """Escapes the traffic loop: it catches Exception, not BaseException."""


class BackgroundTaskTests(_Base):
    def test_traffic_loop_reads_one_dump_and_emits_only_for_running_servers(self):
        from services.amnezia_manager import AmneziaManager

        up = self._server("up", "10.55.0.0/24", 51955)
        self._server("down", "10.56.0.0/24", 51956)
        client, _ = self.manager.add_wireguard_client(up["id"], "phone")
        self.fake.calls.clear()
        iface, key = up["interface"], client["client_public_key"]
        dump = "\t".join([iface, "priv", "pub", "51955", *["0"] * 25, "off"]) + "\n"
        dump += "\t".join([iface, key, "psk", "(none)", "10.55.0.2/32", "0", "0", "0", "off"]) + "\n"  # noqa: FLY002
        self.fake.respond(["/usr/bin/awg", "show", "all", "dump"], dump)

        events = self.manager.events = FakeEvents()
        self.manager.sleep = mock.Mock(side_effect=StopLoop)
        record = self.manager.record_history
        recorded_with = []  # what had been published when the history was recorded
        self.manager.record_history = lambda: (recorded_with.append(list(events.published)), record())
        with (
            mock.patch("services.amnezia_manager.time.time", return_value=1_790_718_000.5),
            mock.patch.object(self.manager, "save_config") as save,
            self.assertRaises(StopLoop),
        ):
            AmneziaManager.start_traffic_monitoring(self.manager)

        # One subprocess per tick, whatever the number of servers; never a config write.
        self.assertEqual(self.fake.argvs(), [["/usr/bin/awg", "show", "all", "dump"]])
        save.assert_not_called()
        self.assertEqual([(event, data["server_id"]) for event, data in events.published], [("traffic_update", up["id"])])
        self.assertEqual(events.published[0][1]["traffic"][client["id"]]["received_bytes"], 0)
        self.manager.sleep.assert_called_once_with(7)
        # The history is recorded before anything is published, and the event carries
        # the tick's time, which the page appends to its last hour.
        self.assertEqual(recorded_with, [[]])
        self.assertEqual(events.published[0][1]["at"], 1_790_718_000.5)
        self.assertEqual(self.manager.history.since, 1_790_718_000.5)

    def test_background_work_runs_on_a_daemon_thread(self):
        # The real hook (the test managers replace it): a plain thread that never
        # keeps the process alive.
        from services.amnezia_manager import AmneziaManager

        ran, seen = threading.Event(), {}

        def target():
            seen["daemon"] = threading.current_thread().daemon
            ran.set()

        AmneziaManager.start_background_task(target)
        self.assertTrue(ran.wait(2))
        self.assertTrue(seen["daemon"])

    def test_status_is_emitted_after_the_delay(self):
        self.manager.emit_status_after_delay("abc", "running", delay_seconds=3)
        self.assertEqual(self.manager.slept[-1], 3)
        self.assertEqual(self.manager.events.published[-1], ("server_status", {"server_id": "abc", "status": "running"}))


class HttpsGetTests(unittest.TestCase):
    """core/helpers.py https_get: the panel's one outbound HTTP client, on http.client."""

    def _get(self, url="https://ipapi.co/8.8.8.8/json/", response=None, error=None, **kwargs):
        from core import helpers

        connection = mock.Mock()
        if error:
            connection.request.side_effect = error
        connection.getresponse.return_value = response or mock.Mock(
            status=200, read=mock.Mock(return_value=b"ok"), getheader=mock.Mock(return_value="text/plain")
        )
        with mock.patch.object(helpers.http.client, "HTTPSConnection", return_value=connection) as cls:
            try:
                return helpers.https_get(url, timeout=3, **kwargs), cls, connection
            finally:
                self.connection = connection

    def test_the_certificate_and_host_name_are_checked(self):
        _, cls, _ = self._get()
        context = cls.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_host_port_path_and_timeout(self):
        _, cls, connection = self._get("https://ipapi.co/8.8.8.8/json/?fields=country")
        self.assertEqual(cls.call_args.args, ("ipapi.co", 443))
        self.assertEqual(cls.call_args.kwargs["timeout"], 3)
        self.assertEqual(connection.request.call_args.args, ("GET", "/8.8.8.8/json/?fields=country"))
        _, cls, connection = self._get("https://example.test:8443")
        self.assertEqual((cls.call_args.args, connection.request.call_args.args), (("example.test", 8443), ("GET", "/")))

    def test_bound_to_a_source_address_only_when_given_one(self):
        _, cls, _ = self._get(source_ip="10.0.0.1")
        self.assertEqual(cls.call_args.kwargs["source_address"], ("10.0.0.1", 0))
        _, cls, _ = self._get()
        self.assertIsNone(cls.call_args.kwargs["source_address"])

    def test_a_user_agent_always_goes_with_it(self):
        # ipapi.co answers 429 to a request without one.
        _, _, connection = self._get(headers={"Accept": "application/json"})
        self.assertEqual(
            connection.request.call_args.kwargs["headers"], {"User-Agent": "amneziawg-web-ui", "Accept": "application/json"}
        )

    def test_the_answer_capped_and_decoded(self):
        from core.helpers import MAX_BODY_BYTES

        read = mock.Mock(return_value=b"198.51.100.7\xff")
        response = mock.Mock(status=503, read=read, getheader=mock.Mock(return_value="text/plain"))
        result, _, _ = self._get(response=response)
        self.assertEqual(result, (503, "text/plain", "198.51.100.7\ufffd"))
        read.assert_called_once_with(MAX_BODY_BYTES)

    def test_the_connection_is_closed_even_when_the_request_fails(self):
        with self.assertRaises(OSError):
            self._get(error=OSError("unreachable"))
        self.connection.close.assert_called_once_with()

    def test_only_https(self):
        from core import helpers

        with mock.patch.object(helpers.http.client, "HTTPSConnection") as cls:
            for url in ("http://api.ipify.org", "api.ipify.org", "https://"):
                with self.assertRaises(ValueError, msg=url):
                    helpers.https_get(url, timeout=3)
        cls.assert_not_called()


class PublicIpTests(_Base):
    """The manager's side: what it keeps when nothing answers (tests/test_netinfo.py has the lookup)."""

    def test_boot_without_internet_keeps_the_last_known_address(self):
        self._server()
        with (
            mock.patch("services.netinfo.https_get", side_effect=OSError("offline")),
            self.assertLogs("services.netinfo", "WARNING"),
        ):
            from services.amnezia_manager import AmneziaManager

            self.assertIsNone(AmneziaManager.detect_public_ip(self.manager))
        self.assertEqual(self.manager.last_known_public_ip(), PUBLIC_IP)

    def test_a_server_cannot_be_created_while_the_public_ip_is_unknown(self):
        self.manager.public_ip = None
        with (
            mock.patch.object(self.manager, "detect_public_ip", return_value=None),
            self.assertRaisesRegex(ValueError, "public IP is unknown"),
        ):
            self._server()
        self.assertEqual(self.manager.config["servers"], [])


class EgressProbeTests(_Base):
    """The manager's side: the probe recorded on its server (tests/test_netinfo.py has the probe)."""

    def test_a_start_checks_the_egress_again_and_tells_the_page(self):
        server = self._server()
        self.paths.interfaces.add(server["interface"])  # awg-quick brought it up
        with (
            mock.patch.object(
                self.manager.netinfo, "detect_public_ip_from_source", return_value=("198.51.100.9", "https://api.ipify.org")
            ) as probe,
            mock.patch.object(self.manager.netinfo, "lookup_geoip", return_value=("Sweden", "SE")),
        ):
            self.assertTrue(self.manager.start_server(server["id"]))
        probe.assert_called_once()
        self.assertEqual(self._saved()["servers"][0]["egress_probe"]["external_ip_geo_country_code"], "SE")
        # The status push, then the probe's: each makes the page reload the servers.
        pushes = [event for event in self.manager.events.published if event[0] == "server_status"]
        self.assertEqual(pushes, [("server_status", {"server_id": server["id"], "status": "running"})] * 2)

    def test_no_egress_check_when_the_server_did_not_come_up(self):
        server = self._server()  # its interface never appears
        with mock.patch.object(self.manager.netinfo, "detect_public_ip_from_source") as probe:
            self.manager.start_server(server["id"])
        probe.assert_not_called()
        self.assertIsNone(self._saved()["servers"][0]["egress_probe"])

    def test_probe_is_recorded_and_persisted(self):
        server = self._server()
        with (
            mock.patch.object(
                self.manager.netinfo, "detect_public_ip_from_source", return_value=("198.51.100.9", "https://ident.me")
            ),
            mock.patch.object(self.manager.netinfo, "lookup_geoip", return_value=("Netherlands", "NL")),
        ):
            probe = self.manager.probe_server_egress_ip(server["id"])
        self.assertEqual((probe["external_ip"], probe["service"], probe["error"]), ("198.51.100.9", "https://ident.me", None))
        self.assertEqual(probe["external_ip_geo_country_code"], "NL")
        self.assertEqual(self._saved()["servers"][0]["egress_probe"]["external_ip"], "198.51.100.9")

    def test_failed_probe_records_the_error(self):
        server = self._server()
        with mock.patch.object(
            self.manager.netinfo, "detect_public_ip_from_source", side_effect=RuntimeError("ident.me: timeout")
        ):
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
