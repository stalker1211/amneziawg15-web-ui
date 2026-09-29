"""Tests for services/generator.py and POST /api/generate.

One test per trap Architect's comments record -- a range in the next zone, a bound
on the cap, S values clamped onto the nonce floor, a collision fix that steps over
its ceiling -- plus many draws through the panel's own validators, which must accept
every set without a single warning.
"""

import itertools
import os
import unittest
from unittest import mock

# tests.support puts web-ui on sys.path. Tests exercise internals on purpose and use
# self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access,wrong-import-order
from services import generator

from tests.support import build_app, build_manager

DRAWS = 300


def always_max(low, high):
    return high


def always_min(low, high):
    return low


class HeaderTests(unittest.TestCase):
    def test_ranges_stay_inside_their_own_zone_even_on_the_largest_draw(self):
        # The base used to be drawn across the whole zone, so the spread and the
        # window carried the range into the next one.
        zones = generator.header_zones()
        for pick in (always_max, always_min):
            with mock.patch.object(generator, "rnd", pick):
                for key, (low, high, _spread) in zones.items():
                    start, end = map(int, generator.header_range(key).split("-"))
                    self.assertTrue(low <= start < end <= high, (pick.__name__, key, start, end))
        self.assertEqual(len({(low, high) for low, high, _ in zones.values()}), 4)
        bounds = sorted(zones.values())
        for (_, high, _), (next_low, _, _) in itertools.pairwise(bounds):
            self.assertLess(high, next_low)

    def test_no_range_ends_on_the_cap(self):
        # 2147483647 as an upper bound is a signature, not a random number.
        with mock.patch.object(generator, "rnd", always_max):
            self.assertLess(int(generator.header_range("H4").split("-")[1]), generator.H_CAP)
        for _ in range(DRAWS):
            end = int(generator.header_range("H4").split("-")[1])
            self.assertLess(end, generator.H_CAP)

    def test_windows_are_1k_to_50k_wide_and_within_5_to_2_31(self):
        for _ in range(DRAWS):
            for key in ("H1", "H2", "H3", "H4"):
                start, end = map(int, generator.header_range(key).split("-"))
                self.assertTrue(generator.RANGE_MIN_WIDTH <= end - start <= generator.RANGE_MAX_WIDTH)
                self.assertTrue(5 <= start and end <= generator.H_CAP)

    def test_single_values_differ_and_stay_under_the_cap(self):
        # Clamped constants once put H2-H4 on the cap: three identical headers.
        for pick in (always_max, always_min):
            with mock.patch.object(generator, "rnd", pick):
                values = [generator.header_single(key) for key in ("H1", "H2", "H3", "H4")]
            self.assertEqual(len(set(values)), 4, values)
            self.assertTrue(all(5 <= v < generator.H_CAP for v in values), values)


class PaddingTests(unittest.TestCase):
    def test_a_collision_at_the_ceiling_steps_down_not_over(self):
        # S1 94 with S2 150 used to give S2 151, past the cap the draw honoured.
        self.assertEqual(generator.avoid_collision(150, 150, lambda v: v == 150), 149)
        self.assertEqual(generator.avoid_collision(80, 150, lambda v: v == 80), 81)
        self.assertEqual(generator.avoid_collision(64, 64, lambda v: v in (64, 63)), 62)
        self.assertEqual(generator.avoid_collision(40, 64, lambda v: False), 40)

    def test_sizes_under_the_floor_are_redrawn_not_clamped(self):
        # Clamping made most S4 draws exactly 12, and S3 = S4 = 12 is a signature.
        lifted = {generator.lift_above_floor(3, 12, 32) for _ in range(DRAWS)}
        self.assertTrue(lifted <= set(range(12, 33)))
        self.assertGreater(len(lifted), 10)
        self.assertEqual(generator.lift_above_floor(20, 12, 32), 20)
        self.assertEqual(generator.lift_above_floor(3, 12, 12), 12)

        s4 = [generator.padding_sizes(with_s34=True, header_protection=True, mtu=1280)["S4"] for _ in range(DRAWS)]
        self.assertTrue(all(12 <= v <= 32 for v in s4))
        self.assertLess(s4.count(12), DRAWS // 5)

    def test_no_two_message_types_come_out_the_same_length(self):
        for _ in range(DRAWS):
            sizes = generator.padding_sizes(with_s34=True, header_protection=False, mtu=1280)
            s1, s2, s3 = sizes["S1"], sizes["S2"], sizes["S3"]
            self.assertNotEqual(s2, s1 + 56, sizes)
            self.assertNotIn(s3, (s1 + 84, s2 + 28), sizes)
            self.assertTrue(generator.S_MIN <= s1 <= generator.S_MAX and s2 <= generator.S_MAX, sizes)
            self.assertTrue(s3 <= generator.S3_MAX and 1 <= sizes["S4"] <= generator.S4_MAX, sizes)

    def test_awg15_has_no_s3_s4(self):
        self.assertEqual(set(generator.padding_sizes(with_s34=False, header_protection=False, mtu=1280)), {"S1", "S2"})


class ClientDefaultsTests(unittest.TestCase):
    def test_junk_is_small(self):
        # Jmin/Jmax used to reach the MTU; junk precedes every handshake.
        for _ in range(DRAWS):
            params = generator.client_defaults(awg3=False)
            self.assertTrue(4 <= params["Jc"] <= 12, params)
            self.assertTrue(8 <= params["Jmin"] <= 40, params)
            self.assertTrue(params["Jmin"] + 64 <= params["Jmax"] <= 160, params)
            self.assertNotIn("RejectAfterTime", params)

    def test_awg3_timers_keep_the_invariants_with_a_margin(self):
        for _ in range(DRAWS):
            params = generator.client_defaults(awg3=True)
            lo = {k: int(params[k].split("-")[0]) for k in generator.timings()}
            hi = {k: int(params[k].split("-")[-1]) for k in generator.timings()}
            self.assertGreater(lo["RejectAfterTime"], lo["KeepaliveTimeout"] + lo["RekeyTimeout"] + 15, params)
            self.assertLess(hi["RekeyAfterTime"], lo["RejectAfterTime"], params)
            padding = [int(x) for x in params["ContentPaddingAddition"].split("-")]
            self.assertTrue(16 <= padding[0] <= padding[-1] <= 128, params)


class ThroughTheValidatorsTests(unittest.TestCase):
    """Every generated set must pass the panel's own checks with no warning at all."""

    def setUp(self):
        self.m = build_manager()

    def test_many_draws_per_protocol(self):
        for protocol in self.m.SUPPORTED_PROTOCOLS:
            for mtu in (1280, 1420):
                for _ in range(DRAWS // 3):
                    transport = self.m.generate_transport_params(protocol, mtu)
                    validated = self.m.validate_transport_params(protocol, transport)
                    self.assertEqual(self.m.transport_param_warnings(protocol, validated, mtu), [], transport)
                    client = self.m.validate_client_params(self.m.generate_client_defaults(protocol))
                    self.assertEqual(self.m.client_param_warnings(client, mtu), [], client)

    def test_protocol_shapes(self):
        awg15 = self.m.generate_transport_params("AWG 1.5", 1420)
        self.assertEqual(set(awg15), {"S1", "S2", "H1", "H2", "H3", "H4"})
        self.assertTrue(all("-" not in str(awg15[k]) for k in ("H1", "H2", "H3", "H4")))
        awg20 = self.m.generate_transport_params("AWG 2.0", 1420)
        self.assertEqual(set(awg20), {"S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"})
        self.assertTrue(all("-" in awg20[k] for k in ("H1", "H2", "H3", "H4")))
        for protocol in ("AWG 3.0", "AWG 3.1"):
            params = self.m.generate_transport_params(protocol, 1420)
            self.assertTrue(self.m.is_valid_wireguard_key(params["HeaderProtectionKey"]), protocol)
            self.assertIn("RejectAfterTime", self.m.generate_client_defaults(protocol))


class GenerateRouteTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()

    def test_answers_a_set_for_the_protocol_and_saves_nothing(self):
        response = self.client.post("/api/generate", json={"protocol": "AWG 3.1", "mtu": 1280})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["protocol"], "AWG 3.1")
        self.assertIn("HeaderProtectionKey", data["transport_params"])
        self.assertIn("KeepaliveTimeout", data["client_defaults"])
        self.assertFalse(os.path.exists(self.manager.config_file))

    def test_defaults_and_bad_input(self):
        data = self.client.post("/api/generate", json={}).get_json()
        self.assertEqual(data["protocol"], self.manager.DEFAULT_PROTOCOL)
        response = self.client.post("/api/generate", json={"protocol": "AWG 2.0", "mtu": 9000})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/generate").status_code, 405)


if __name__ == "__main__":
    unittest.main()
