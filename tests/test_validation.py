"""Tests for the pure validation / parsing helpers.

These encode the protocol rules that are easy to get wrong and expensive to debug
at runtime, so they double as an executable spec:

  * S1 + 56 != S2                     (AWG 1.5+)
  * H1-H4 ranges must not overlap     (AWG 2.0+)
  * each of S1-S4 >= 12 with a key    (AWG 3.0 header protection)
  * subnet/port must be well-formed   (they reach config files and iptables)
"""

import unittest

from tests.support import HEADER_PROTECTION_KEY, build_manager

VALID_TRANSPORT = {
    "S1": 50,
    "S2": 60,
    "S3": 40,
    "S4": 20,
    "H1": "1000",
    "H2": "2000",
    "H3": "3000",
    "H4": "4000",
}


class ProtocolNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_accepted_spellings(self):
        for value, expected in [
            ("AWG 1.5", "AWG 1.5"),
            ("1.5", "AWG 1.5"),
            ("awg1.5", "AWG 1.5"),
            ("AWG 2.0", "AWG 2.0"),
            ("2.0", "AWG 2.0"),
            ("AWG_2.0", "AWG 2.0"),
            ("AWG 3.0", "AWG 3.0"),
            ("3.0", "AWG 3.0"),
            ("awg3.0", "AWG 3.0"),
            ("AWG 3.1", "AWG 3.1"),
            ("3.1", "AWG 3.1"),
            ("awg3.1", "AWG 3.1"),
        ]:
            self.assertEqual(self.m.normalize_protocol(value), expected, value)

    def test_unknown_falls_back_to_default(self):
        for value in ("garbage", "", None, 42, "AWG 9.9"):
            self.assertEqual(self.m.normalize_protocol(value), "AWG 1.5")

    def test_capability_matrix(self):
        expected = {
            "AWG 1.5": (False, False, False, False),
            "AWG 2.0": (True, True, False, False),
            "AWG 3.0": (True, True, True, False),
            "AWG 3.1": (True, True, True, True),
        }
        for protocol, (s34, ranges, awg3, awg31) in expected.items():
            self.assertEqual(self.m.protocol_supports_s34(protocol), s34, protocol)
            self.assertEqual(self.m.protocol_supports_header_ranges(protocol), ranges, protocol)
            self.assertEqual(self.m.protocol_supports_awg3(protocol), awg3, protocol)
            self.assertEqual(self.m.protocol_supports_awg31(protocol), awg31, protocol)


class UintRangeTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_valid_forms(self):
        for raw, expected in [
            ("", ""),
            (None, ""),
            ("25", "25"),
            ("22-30", "22-30"),
            (" 22 - 30 ", "22-30"),
            ("7-7", "7"),
            (25, "25"),
        ]:
            self.assertEqual(self.m.parse_uint_range(raw, "X"), expected, repr(raw))

    def test_rejects_bad_input(self):
        for raw in ("30-22", "abc", "1-2-3", "-5", "1.5", "0x10"):
            with self.assertRaises(ValueError, msg=repr(raw)):
                self.m.parse_uint_range(raw, "X")

    def test_rejects_above_uint32(self):
        with self.assertRaises(ValueError):
            self.m.parse_uint_range("4294967296", "X")


class TransportParamTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_valid_params_round_trip(self):
        result = self.m.validate_transport_params("AWG 2.0", dict(VALID_TRANSPORT))
        self.assertEqual(result["S1"], 50)
        self.assertEqual(result["H1"], "1000")

    def test_s1_plus_56_must_not_equal_s2(self):
        with self.assertRaises(ValueError) as ctx:
            self.m.validate_transport_params("AWG 1.5", {**VALID_TRANSPORT, "S1": 50, "S2": 106})
        self.assertIn("S1 + 56", str(ctx.exception))

    def test_negative_s_rejected(self):
        with self.assertRaises(ValueError):
            self.m.validate_transport_params("AWG 2.0", {**VALID_TRANSPORT, "S1": -1})

    def test_s3_s4_dropped_for_awg15(self):
        result = self.m.validate_transport_params("AWG 1.5", dict(VALID_TRANSPORT))
        self.assertNotIn("S3", result)
        self.assertNotIn("S4", result)

    def test_header_ranges_allowed_for_20_and_30_only(self):
        ranged = {**VALID_TRANSPORT, "H1": "1200-1400"}
        for protocol in ("AWG 2.0", "AWG 3.0"):
            self.assertEqual(self.m.validate_transport_params(protocol, dict(ranged))["H1"], "1200-1400")
        with self.assertRaises(ValueError):
            self.m.validate_transport_params("AWG 1.5", dict(ranged))

    def test_overlapping_header_ranges_rejected(self):
        overlapping = {**VALID_TRANSPORT, "H1": "1000-2000", "H2": "1500-2500"}
        with self.assertRaises(ValueError) as ctx:
            self.m.validate_transport_params("AWG 2.0", overlapping)
        self.assertIn("intersect", str(ctx.exception))

    def test_identical_single_headers_rejected_as_overlap(self):
        with self.assertRaises(ValueError):
            self.m.validate_transport_params("AWG 2.0", {**VALID_TRANSPORT, "H2": "1000"})

    def test_empty_header_rejected(self):
        with self.assertRaises(ValueError):
            self.m.validate_transport_params("AWG 2.0", {**VALID_TRANSPORT, "H1": ""})


class HeaderProtectionTests(unittest.TestCase):
    """AWG 3.0: each of S1-S4 must individually be >= 12 when a key is set.

    This is a per-parameter floor, NOT a subtraction (`S1 - S4`): amneziawg-go uses
    the first 12 bytes of *each* message type's padding as the cipher nonce.
    """

    def setUp(self):
        self.m = build_manager()

    def _params(self, **over):
        return {**VALID_TRANSPORT, "HeaderProtectionKey": HEADER_PROTECTION_KEY, **over}

    def test_all_s_at_or_above_floor_accepted(self):
        result = self.m.validate_transport_params("AWG 3.0", self._params(S1=12, S2=12, S3=12, S4=12))
        self.assertEqual(result["HeaderProtectionKey"], HEADER_PROTECTION_KEY)

    def test_each_s_below_floor_rejected(self):
        for key in ("S1", "S2", "S3", "S4"):
            with self.assertRaises(ValueError, msg=key) as ctx:
                self.m.validate_transport_params("AWG 3.0", self._params(**{key: 11}))
            # The message must name the offending field; the daemon's own error is
            # off by one (an S4 violation is reported as "S3").
            self.assertIn(key, str(ctx.exception))

    def test_large_spread_between_s_values_is_fine(self):
        """S1 - S4 is strongly negative here; only the per-value floor matters."""
        self.m.validate_transport_params("AWG 3.0", self._params(S1=30, S2=100, S3=100, S4=100))

    def test_floor_only_applies_when_key_is_set(self):
        result = self.m.validate_transport_params("AWG 3.0", {**VALID_TRANSPORT, "S4": 5})
        self.assertEqual(result["S4"], 5)

    def test_malformed_key_rejected(self):
        for bad in ("not-a-key", "c2hvcnQ=", "x" * 44):
            with self.assertRaises(ValueError, msg=bad):
                self.m.validate_transport_params("AWG 3.0", self._params(HeaderProtectionKey=bad))

    def test_key_ignored_on_older_protocols(self):
        for protocol in ("AWG 1.5", "AWG 2.0"):
            result = self.m.validate_transport_params(protocol, self._params())
            self.assertNotIn("HeaderProtectionKey", result, protocol)


class Awg31TransportTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_boolean_options_are_normalized(self):
        result = self.m.validate_transport_params(
            "AWG 3.1",
            {
                **VALID_TRANSPORT,
                "RandomTrailers": "on",
                "DisableCookies": False,
            },
        )
        self.assertIs(result["RandomTrailers"], True)
        self.assertIs(result["DisableCookies"], False)

    def test_boolean_options_are_ignored_by_awg30(self):
        result = self.m.validate_transport_params(
            "AWG 3.0",
            {
                **VALID_TRANSPORT,
                "RandomTrailers": True,
                "DisableCookies": True,
            },
        )
        self.assertNotIn("RandomTrailers", result)
        self.assertNotIn("DisableCookies", result)


class ClientParamTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_defaults_applied(self):
        result = self.m.validate_client_params({})
        self.assertEqual(result["Jc"], 8)
        self.assertEqual(result["I1"], "")

    def test_jmin_jmax_must_be_positive_and_jc_not_negative(self):
        for key, value in (("Jc", -1), ("Jmin", 0), ("Jmax", 0)):
            with self.assertRaises(ValueError, msg=key):
                self.m.validate_client_params({"Jc": 8, "Jmin": 40, "Jmax": 70, key: value})
        # Jc 0 sends no junk; the daemon accepts it, and the form only warns.
        self.assertEqual(self.m.validate_client_params({"Jc": 0, "Jmin": 40, "Jmax": 70})["Jc"], 0)

    def test_uint16_bounds(self):
        for key in ("Jc", "Jmin", "Jmax"):
            with self.assertRaisesRegex(ValueError, f"{key} must be at most 65535"):
                self.m.validate_client_params({"Jc": 8, "Jmin": 40, "Jmax": 70000, key: 70000})
        for key in ("S1", "S2", "S3", "S4"):
            params = {**VALID_TRANSPORT, key: 65536}
            with self.assertRaisesRegex(ValueError, f"{key} must be at most 65535"):
                self.m.validate_transport_params("AWG 2.0", params)
        self.assertEqual(self.m.validate_transport_params("AWG 2.0", {**VALID_TRANSPORT, "S3": 65535})["S3"], 65535)

    def test_header_values_are_uint32(self):
        for value in ("4294967296", "5-4294967296"):
            with self.assertRaisesRegex(ValueError, "exceeds 4294967295"):
                self.m.validate_transport_params("AWG 2.0", {**VALID_TRANSPORT, "H4": value})
        self.assertEqual(
            self.m.validate_transport_params("AWG 2.0", {**VALID_TRANSPORT, "H4": "4294967295"})["H4"], "4294967295"
        )

    def test_duplicate_headers_are_refused_on_awg_15_too(self):
        # Skipped until 2.4 by a `protocol == "AWG 1.5"` literal.
        with self.assertRaisesRegex(ValueError, "must not intersect"):
            self.m.validate_transport_params("AWG 1.5", {"S1": 50, "S2": 60, "H1": 7, "H2": 7, "H3": 9, "H4": 10})

    def test_jmin_must_not_exceed_jmax(self):
        with self.assertRaises(ValueError):
            self.m.validate_client_params({"Jc": 8, "Jmin": 90, "Jmax": 70})

    def test_awg3_ranges_normalized_and_unset_dropped(self):
        result = self.m.validate_client_params(
            {
                "Jc": 8,
                "Jmin": 40,
                "Jmax": 70,
                "KeepaliveTimeout": " 22 - 30 ",
                "RekeyAfterTime": "",
            }
        )
        self.assertEqual(result["KeepaliveTimeout"], "22-30")
        # Empty means "use the daemon default", so the key must not be written at all.
        self.assertNotIn("RekeyAfterTime", result)

    def test_i_params_are_single_line(self):
        result = self.m.validate_client_params(
            {
                "Jc": 8,
                "Jmin": 40,
                "Jmax": 70,
                "I1": "<b 0xf0>\ninjected = 1",
            }
        )
        self.assertNotIn("\n", result["I1"])


class SignaturePacketTests(unittest.TestCase):
    """I1-I5 as amneziawg-go's newObfChain parses them (device/obf.go)."""

    def setUp(self):
        self.m = build_manager()

    def packet(self, value):
        return self.m.parse_signature_packet("I1", value)

    def test_sizes_of_each_tag(self):
        self.assertEqual(self.packet("<b 0xc0ffee><t><r 10><rc 3><rd 2><dz 4>"), (3 + 4 + 10 + 3 + 2 + 4, []))
        self.assertEqual(self.packet("<b C0FFEE>")[0], 3)  # the 0x is optional, case is not
        self.assertEqual(self.packet("  <t>  <r +5>  "), (9, []))

    def test_what_the_daemon_refuses_is_an_error(self):
        for value, message in (
            ("<c>", "unknown tag <c>"),
            ("<x 5>", "unknown tag <x>"),
            ("<b 0xabc>", "even number of hex digits"),
            ("<b 0xzz>", "even number of hex digits"),
            ("<b>", "even number of hex digits"),
            ("<r>", "needs a byte count"),
            ("<r five>", "needs a byte count"),
            ("<r -5>", "must not be negative"),
            ("<r 5", "missing its closing"),
            ("<>", "empty tag"),
        ):
            with self.assertRaisesRegex(ValueError, message, msg=value):
                self.packet(value)

    def test_what_the_daemon_ignores_is_a_warning(self):
        self.assertEqual(self.packet("hello<r 5>")[1], ["I1: text outside <...> tags is ignored by the daemon."])
        self.assertEqual(self.packet("<d><ds><r 2>")[1],
                         ["I1: <d> adds nothing here: signature packets carry no payload.",
                          "I1: <ds> adds nothing here: signature packets carry no payload."])  # fmt: skip

    def test_client_params_reject_a_bad_packet_and_warn_on_a_big_one(self):
        with self.assertRaisesRegex(ValueError, "I3: unknown tag <c>"):
            self.m.validate_client_params({"I3": "<c><r 5>"})
        params = self.m.validate_client_params({"I1": "<r 1400>"})
        self.assertIn("I1 is 1400 bytes, above MTU (1280): it may be fragmented.", self.m.client_param_warnings(params, 1280))


class ParamWarningTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def transport_warnings(self, protocol="AWG 2.0", **changes):
        validated = self.m.validate_transport_params(protocol, {**VALID_TRANSPORT, **changes})
        return self.m.transport_param_warnings(protocol, validated, 1420)

    def test_s3_collisions_are_a_fingerprint(self):
        self.assertEqual(self.transport_warnings(S1=20, S2=50, S3=104),
                         ["S3 (104) = S1 + 84: cookie replies come out as long as handshake initiations."])  # fmt: skip
        self.assertEqual(self.transport_warnings(S1=20, S2=50, S3=78),
                         ["S3 (78) = S2 + 28: cookie replies come out as long as handshake responses."])  # fmt: skip
        self.assertEqual(self.transport_warnings(protocol="AWG 1.5", S1=20, S2=50, S3=78), [])

    def test_headers_1_to_4_warn_unless_header_protection_hides_them(self):
        standard = {"H1": "1", "H2": "2", "H3": "3", "H4": "4"}
        self.assertEqual(self.transport_warnings(**standard),
                         ["H1, H2, H3, H4 in 1-4: WireGuard's own message types, which DPI recognises."])  # fmt: skip
        # A range that can draw 3 sometimes sends WireGuard's own cookie type.
        self.assertEqual(len(self.transport_warnings(H2="3-900")), 1)
        self.assertEqual(self.transport_warnings(H2="5-900"), [])
        protected = {**standard, "S1": 50, "S2": 60, "S3": 40, "S4": 20, "HeaderProtectionKey": HEADER_PROTECTION_KEY}
        self.assertEqual(self.transport_warnings(protocol="AWG 3.0", **protected), [])

    def test_timer_rules(self):
        def warnings(**timings):
            return self.m.timing_warnings(self.m.validate_client_params(timings))

        self.assertEqual(warnings(), [])
        self.assertEqual(warnings(RekeyAfterTime="100-130", RejectAfterTime="170-190"), [])
        self.assertEqual(warnings(RejectAfterTime="14-20"), [
            ("RejectAfterTime starts at 14, not above KeepaliveTimeout + RekeyTimeout (10 + 5): "
             "every received packet would trigger a rekey."),
            ("RekeyAfterTime reaches 120, not below RejectAfterTime's start (14): "
             "the session can be rejected before it rekeys."),
        ])  # fmt: skip
        self.assertEqual(len(warnings(RekeyAfterTime="150-200")), 1)
        self.assertEqual(warnings(MaxHandshakeAttempts="1-3"), [])


class SubnetAndKeyTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_normalizes_host_address_to_network(self):
        self.assertEqual(self.m.validate_subnet("10.8.0.5/24"), "10.8.0.0/24")

    def test_rejects_injection_and_malformed(self):
        for bad in ("10.0.0.0/24; id", "not-a-subnet", "10.0.0.0/31", "::1/64", "", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.m.validate_subnet(bad)

    def test_wireguard_key_shape(self):
        self.assertTrue(self.m.is_valid_wireguard_key(HEADER_PROTECTION_KEY))
        for bad in ("", None, "short", "!" * 44, "a" * 43 + "="):
            self.assertFalse(self.m.is_valid_wireguard_key(bad), repr(bad))


class ServerCreationValidationTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_rejects_bad_port(self):
        for port in (0, 70000, "abc", None):
            with self.assertRaises(ValueError, msg=repr(port)):
                self.m.create_wireguard_server({"name": "x", "port": port, "auto_start": False})

    def test_rejects_bad_mtu(self):
        for mtu in (1000, 9000):
            with self.assertRaises(ValueError, msg=repr(mtu)):
                self.m.create_wireguard_server({"name": "x", "mtu": mtu, "auto_start": False})

    def test_rejects_invalid_dns(self):
        with self.assertRaises(ValueError):
            self.m.create_wireguard_server({"name": "x", "dns": "not-an-ip", "auto_start": False})

    def test_generated_awg3_params_satisfy_their_own_validator(self):
        """Random generation must never produce a config the daemon would reject."""
        for _ in range(50):
            params = self.m.generate_transport_params("AWG 3.0", 1420)
            validated = self.m.validate_transport_params("AWG 3.0", params)
            for key in ("S1", "S2", "S3", "S4"):
                self.assertGreaterEqual(validated[key], self.m.HEADER_CIPHER_NONCE_SIZE, params)


class HelperTests(unittest.TestCase):
    """core/helpers.py: parse env/API toggles and IPs the same way everywhere."""

    def test_to_bool(self):
        from core.helpers import to_bool

        for value in (True, 1, 2.5, "1", "true", "yes", "on", " ON ", "anything-else"):
            self.assertIs(to_bool(value), True, value)
        for value in (False, 0, 0.0, "0", "false", "no", "off", " Off "):
            self.assertIs(to_bool(value, default=True), False, value)
        for value in (None, "", "  ", "none", "null"):
            self.assertIs(to_bool(value, default=True), True, value)
            self.assertIs(to_bool(value), False, value)
        self.assertIs(to_bool([]), False)
        self.assertIs(to_bool(["x"]), True)

    def test_is_valid_ip(self):
        from core.helpers import is_valid_ip

        for ip in ("0.0.0.0", "10.0.0.1", "255.255.255.255"):
            self.assertTrue(is_valid_ip(ip), ip)
        # int() let "+1" and "1_0" through; a leading zero reads as octal to inet_aton.
        bad = ("1.2.3", "1.2.3.4.5", "256.1.1.1", "-1.2.3.4", "a.b.c.d", "", None, "::1", "1.2.3.4/24",
               "+1.2.3.4", "1_0.0.0.1", "01.2.3.4", " 1.2.3.4")  # fmt: skip
        for ip in bad:
            self.assertFalse(is_valid_ip(ip), ip)

    def test_daemon_log_level(self):
        from core.helpers import parse_daemon_log_level

        cases = {None: "off", "": "off", " off ": "off", "0": "off", "silent": "off", "ERROR": "error",
                 "debug": "debug", "verbose": "debug", "loud": None}  # fmt: skip
        for raw, expected in cases.items():
            self.assertEqual(parse_daemon_log_level(raw), expected, raw)


if __name__ == "__main__":
    unittest.main()
