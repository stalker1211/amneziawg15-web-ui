"""Tests for POST /api/validate, the dry run the forms call as you type.

The validation rules live only in Python; this endpoint runs them without changing
anything. Pinned here: every body shape, one message per failing validator, the
conflict messages, the warnings ported from the old JS, the counts the server
settings warning shows (including a revert), and that nothing is ever written.
"""

import os
import unittest

from tests.support import build_app

VALID_TRANSPORT = {"S1": 70, "S2": 80, "S3": 30, "S4": 25, "H1": "5000", "H2": "6000", "H3": "7000", "H4": "8000"}


class ValidateTestCase(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.http = self.app.test_client()
        self.home = self.manager.create_wireguard_server(
            {"name": "home", "protocol": "AWG 2.0", "subnet": "10.40.0.0/24", "port": 51940, "auto_start": False}
        )

    def validate(self, body, status=200):
        response = self.http.post("/api/validate", json=body)
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()


class NewServerTests(ValidateTestCase):
    def test_a_name_alone_is_valid_because_everything_else_has_defaults(self):
        self.assertEqual(self.validate({"server": {"name": "office", "port": 51941, "subnet": "10.41.0.0/24"}}),
                         {"errors": [], "warnings": []})  # fmt: skip

    def test_every_invalid_field_is_reported_at_once(self):
        body = {"server": {"name": " ", "port": 70000, "subnet": "10.0.0.0/31", "mtu": 9000, "dns": "1.1.1.1, nope"}}
        self.assertEqual(
            self.validate(body)["errors"],
            [
                "A server name is required",
                "MTU must be between 1280 and 1440, got 9000",
                "Port must be between 1 and 65535, got 70000",
                "Subnet '10.0.0.0/31' is too small: use /30 or larger",
                "Invalid DNS server IP: nope",
            ],
        )

    def test_non_numeric_values_are_messages_not_crashes(self):
        errors = self.validate({"server": {"name": "x", "port": "abc", "mtu": "big", "subnet": "10.41.0.0/24"}})["errors"]
        self.assertEqual(errors, ["MTU must be an integer, got 'big'", "Port must be an integer, got 'abc'"])

    def test_conflicts_with_an_existing_server(self):
        port_taken = {"server": {"name": "x", "port": 51940, "subnet": "10.41.0.0/24"}}
        self.assertEqual(self.validate(port_taken)["errors"], ["Port 51940 is already used by server 'home'"])
        overlap = {"server": {"name": "x", "port": 51941, "subnet": "10.40.0.128/25"}}
        self.assertEqual(self.validate(overlap)["errors"], ["Subnet 10.40.0.128/25 overlaps 'home' (10.40.0.0/24)"])

    def test_no_conflict_check_while_the_port_is_invalid(self):
        body = {"server": {"name": "x", "port": 0, "subnet": "10.40.0.0/24"}}
        self.assertEqual(self.validate(body)["errors"], ["Port must be between 1 and 65535, got 0"])

    def test_transport_params_and_client_defaults_are_checked(self):
        body = {"server": {"name": "x", "port": 51941, "subnet": "10.41.0.0/24", "protocol": "AWG 2.0",
                           "transport_params": {**VALID_TRANSPORT, "S2": 126},
                           "client_defaults": {"Jc": 8, "Jmin": 90, "Jmax": 80}}}  # fmt: skip
        self.assertEqual(
            self.validate(body)["errors"],
            ["S1 + 56 must not equal S2", "Jmin must be less than or equal to Jmax, got Jmin=90, Jmax=80"],
        )

    def test_warnings_use_the_forms_mtu(self):
        body = {"server": {"name": "x", "port": 51941, "subnet": "10.41.0.0/24", "protocol": "AWG 2.0", "mtu": 1280,
                           "transport_params": {**VALID_TRANSPORT, "S1": 140, "S2": 1200},
                           "client_defaults": {"Jc": 3, "Jmin": 8, "Jmax": 1300}}}  # fmt: skip
        self.assertEqual(
            self.validate(body)["warnings"],
            [
                "S2 (1200) is outside the common 15-150 range.",
                "S2 (1200) is above the rule-of-thumb bound MTU - 92 (1188).",
                "Jc (3) is outside the recommended range 4-12.",
                "Jmax (1300) is at or above MTU (1280) and may fragment junk packets.",
            ],
        )

    def test_nothing_is_created(self):
        self.validate({"server": {"name": "office", "port": 51941, "subnet": "10.41.0.0/24"}})
        self.assertEqual([s["name"] for s in self.manager.config["servers"]], ["home"])


class TransportWarningTests(ValidateTestCase):
    """The rules of the old getTransportParamWarningsJS, now in Python."""

    def warnings(self, protocol, **overrides):
        transport = self.manager.validate_transport_params(protocol, {**VALID_TRANSPORT, **overrides})
        return self.manager.transport_param_warnings(protocol, transport, 1420)

    def test_within_the_usual_ranges_there_is_nothing_to_say(self):
        self.assertEqual(self.warnings("AWG 2.0"), [])

    def test_each_rule(self):
        self.assertEqual(self.warnings("AWG 2.0", S1=10), ["S1 (10) is outside the common 15-150 range."])
        self.assertEqual(self.warnings("AWG 2.0", S3=151), ["S3 (151) is outside the common 15-150 range."])
        self.assertIn("S4 (33) is above a conservative 0-32", self.warnings("AWG 2.0", S4=33)[0])
        self.assertIn("MTU - 148 (1272)", self.warnings("AWG 2.0", S1=1273)[1])

    def test_bounds_are_exact(self):
        # MTU 1420: S1 may reach 1272 and S2 1328 before the MTU rules speak up.
        self.assertEqual(len(self.warnings("AWG 2.0", S1=1272)), 1)
        self.assertEqual(len(self.warnings("AWG 2.0", S1=1273)), 2)
        self.assertEqual(len(self.warnings("AWG 2.0", S2=1328)), 1)
        self.assertEqual(len(self.warnings("AWG 2.0", S2=1329)), 2)
        self.assertEqual(self.warnings("AWG 2.0", S1=15, S2=150, S3=15, S4=32), [])
        quiet = {"Jc": 4, "Jmin": 8, "Jmax": 1419}
        self.assertEqual(self.manager.client_param_warnings(quiet, 1420), [])
        self.assertEqual(self.manager.client_param_warnings({**quiet, "Jc": 12}, 1420), [])
        self.assertEqual(len(self.manager.client_param_warnings({**quiet, "Jc": 13, "Jmax": 1420}, 1420)), 2)

    def test_s3_and_s4_are_ignored_where_the_protocol_has_none(self):
        self.assertEqual(self.warnings("AWG 1.5", S3=500, S4=500), [])


class ServerSettingsTests(ValidateTestCase):
    def setUp(self):
        super().setUp()
        self.clients = [self.manager.add_wireguard_client(self.home["id"], name)[0] for name in ("a", "b", "c")]
        for client in self.clients[:2]:
            self.manager.mark_config_issued(self.home, client)

    def settings(self, transport, protocol="AWG 2.0"):
        return self.validate({"server_id": self.home["id"], "protocol": protocol, "transport_params": transport})

    def test_unchanged_values_change_nothing(self):
        current = dict(self.manager.get_server(self.home["id"])["transport_params"])
        self.assertEqual(
            self.settings(current),
            {"errors": [], "warnings": [], "configs_changed": 0, "outdated_now": 0, "outdated_after": 0},
        )

    def test_a_change_counts_every_config_but_only_issued_clients_need_to_re_import(self):
        result = self.settings(VALID_TRANSPORT)
        self.assertEqual((result["configs_changed"], result["outdated_now"], result["outdated_after"]), (3, 0, 2))
        self.assertEqual(result["errors"], [])

    def test_a_revert_brings_every_device_back_in_line(self):
        original = dict(self.manager.get_server(self.home["id"])["transport_params"])
        self.manager.update_server_transport_params(self.home["id"], {"protocol": "AWG 2.0", **VALID_TRANSPORT})
        result = self.settings(original)
        self.assertEqual((result["configs_changed"], result["outdated_now"], result["outdated_after"]), (3, 2, 0))

    def test_errors_and_warnings(self):
        result = self.settings({**VALID_TRANSPORT, "S4": 40, "H2": "5000"})
        self.assertEqual(result["errors"], ["H1-H4 ranges must not intersect for AWG 2.0"])
        self.assertEqual(result["configs_changed"], 0)

        result = self.settings({**VALID_TRANSPORT, "S4": 40})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1)

    def test_awg3_nonce_floor_is_reported(self):
        key = "aGVhZGVyUFJPVEVDVElPTmtleTAwMDAwMDAwMDAwMDA="
        result = self.settings({**VALID_TRANSPORT, "S4": 5, "HeaderProtectionKey": key}, protocol="AWG 3.0")
        self.assertEqual(result["errors"], ["S4 must be at least 12 when HeaderProtectionKey is set"])

    def test_nothing_is_saved(self):
        os.utime(self.manager.config_file, ns=(0, 0))
        before = dict(self.manager.get_server(self.home["id"])["transport_params"])
        self.settings(VALID_TRANSPORT)
        self.assertEqual(self.manager.get_server(self.home["id"])["transport_params"], before)
        self.assertEqual(os.stat(self.manager.config_file).st_mtime_ns, 0)


class ClientParamsTests(ValidateTestCase):
    def client(self, params):
        return self.validate({"server_id": self.home["id"], "client_params": params})

    def test_valid_and_quiet(self):
        self.assertEqual(self.client({"Jc": 8, "Jmin": 8, "Jmax": 80}), {"errors": [], "warnings": []})

    def test_errors(self):
        self.assertEqual(self.client({"Jc": -1})["errors"], ["Jc must not be negative, got -1"])
        self.assertEqual(
            self.client({"Jc": 0}), {"errors": [], "warnings": ["Jc is 0: no junk packets are sent before the handshake."]}
        )
        self.assertEqual(self.client({"Jc": 8, "RekeyTimeout": "9-3"})["errors"],
                         ["RekeyTimeout range '9-3' is inverted: start must be <= end"])  # fmt: skip

    def test_warnings_use_the_servers_mtu(self):
        self.assertEqual(
            self.client({"Jc": 20, "Jmin": 8, "Jmax": 1420})["warnings"],
            ["Jc (20) is outside the recommended range 4-12.",
             "Jmax (1420) is at or above MTU (1420) and may fragment junk packets."],
        )  # fmt: skip


class MalformedRequestTests(ValidateTestCase):
    def test_bodies_that_match_no_form_are_a_400(self):
        for body in ({}, {"server": "x"}, {"transport_params": {}}, {"client_params": {}}):
            self.assertIn("error", self.validate(body, status=400), body)

    def test_a_server_id_without_params_is_a_400(self):
        self.assertIn("error", self.validate({"server_id": self.home["id"]}, status=400))

    def test_an_unknown_server_is_a_404(self):
        self.assertEqual(self.validate({"server_id": "nope", "client_params": {}}, status=404), {"error": "Server not found"})

    def test_it_is_behind_the_json_only_guard(self):
        response = self.http.post("/api/validate", data="server=x", content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()
