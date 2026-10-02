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

    def test_equal_padding_is_one_size_for_all_four(self):
        # With random trailers, amneziawg-go#186: equal S keeps data packets out of the
        # handshake branches. The size still varies between servers.
        seen = set()
        for _ in range(DRAWS):
            sizes = generator.padding_sizes(with_s34=True, header_protection=True, mtu=1280, equal=True)
            self.assertEqual(len(set(sizes.values())), 1, sizes)
            self.assertTrue(generator.S_MIN <= sizes["S1"] <= generator.S4_MAX, sizes)
            seen.add(sizes["S1"])
        self.assertGreater(len(seen), 10)


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

    def test_many_draws_with_random_trailers_on(self):
        for mtu in (1280, 1420):
            for _ in range(DRAWS // 3):
                transport = self.m.generate_transport_params("AWG 3.1", mtu, random_trailers=True)
                validated = self.m.validate_transport_params("AWG 3.1", {**transport, "RandomTrailers": True})
                self.assertIsNone(self.m.trailer_loss("AWG 3.1", validated), transport)
                self.assertEqual(self.m.transport_param_warnings("AWG 3.1", validated, mtu), [], transport)

    def test_random_trailers_only_matter_on_31(self):
        # 2.0 and 3.0 have no trailers: their S1-S4 stay drawn apart.
        for protocol in ("AWG 2.0", "AWG 3.0"):
            unequal = sum(
                len({v for k, v in self.m.generate_transport_params(protocol, 1420, random_trailers=True).items()
                     if k in ("S1", "S2", "S3", "S4")}) > 1
                for _ in range(20)
            )  # fmt: skip
            self.assertGreater(unequal, 15, protocol)

    def test_protocol_shapes(self):
        awg15 = self.m.generate_transport_params("AWG 1.5", 1420)
        self.assertEqual(set(awg15), {"S1", "S2", "H1", "H2", "H3", "H4"})
        self.assertTrue(all("-" not in str(awg15[k]) for k in ("H1", "H2", "H3", "H4")))
        awg20 = self.m.generate_transport_params("AWG 2.0", 1420)
        self.assertEqual(set(awg20), {"S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"})
        self.assertTrue(all("-" in str(awg20[k]) for k in ("H1", "H2", "H3", "H4")))
        for protocol in ("AWG 3.0", "AWG 3.1"):
            params = self.m.generate_transport_params(protocol, 1420)
            self.assertTrue(self.m.is_valid_wireguard_key(params["HeaderProtectionKey"]), protocol)
            self.assertIn("RejectAfterTime", self.m.generate_client_defaults(protocol))


# amneziawg-go#186's ranges, from a production interface: 16.113% expected, 16.082% measured.
ISSUE_186 = {"H1": "405138553-456138212", "H2": "680931238-1038459501", "H3": "1114423399-1432068193",
             "H4": "1500000000-1500001000", "S1": 97, "S2": 41, "S3": 133, "S4": 24}  # fmt: skip


class TrailerLossTests(unittest.TestCase):
    """amneziawg-go#186: the warning for random trailers with unequal S1-S4."""

    def setUp(self):
        self.m = build_manager()

    def validated(self, protocol="AWG 3.1", trailers=True, **overrides):
        return self.m.validate_transport_params(protocol, {**ISSUE_186, "RandomTrailers": trailers, **overrides})

    def warnings(self, transport, protocol="AWG 3.1"):
        return [w for w in self.m.transport_param_warnings(protocol, transport, 1420) if "trailers" in w]

    def loss(self, transport):
        value = self.m.trailer_loss("AWG 3.1", transport)
        if value is None:
            self.fail("expected a loss estimate")
        return value

    def test_the_issues_ranges_lose_what_it_measured(self):
        transport = self.validated()
        self.assertAlmostEqual(self.loss(transport), 0.16113, places=4)
        [warning] = self.warnings(transport)
        self.assertIn("about 16%", warning)
        self.assertIn("amneziawg-go#186", warning)

    def test_a_branch_reading_at_s4_loses_nothing(self):
        # S1 = S4: the H1 branch reads the packet's own type field, an H4 value.
        only_h2_h3 = 1 - (1 - 357528264 / 2**32) * (1 - 317644795 / 2**32)
        self.assertAlmostEqual(self.loss(self.validated(S1=24)), only_h2_h3, places=6)

    def test_narrow_ranges_still_warn_but_say_how_little(self):
        narrow = self.validated(H1="1000000-1025000", H2="2000000-2025000", H3="3000000-3025000")
        [warning] = self.warnings(narrow)
        self.assertIn("under 0.01%", warning)

    def test_no_warning_with_equal_s_trailers_off_or_another_protocol(self):
        self.assertEqual(self.warnings(self.validated(S1=24, S2=24, S3=24)), [])
        self.assertEqual(self.warnings(self.validated(trailers=False)), [])
        self.assertEqual(self.warnings(self.validated("AWG 3.0"), "AWG 3.0"), [])
        self.assertIsNone(self.m.trailer_loss("AWG 3.0", self.validated("AWG 3.0")))


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

    def test_random_trailers_draw_equal_padding(self):
        body = {"protocol": "AWG 3.1", "random_trailers": True}
        for _ in range(20):
            transport = self.client.post("/api/generate", json=body).get_json()["transport_params"]
            self.assertEqual(len({transport[k] for k in ("S1", "S2", "S3", "S4")}), 1, transport)

    def test_defaults_and_bad_input(self):
        data = self.client.post("/api/generate", json={}).get_json()
        self.assertEqual(data["protocol"], self.manager.DEFAULT_PROTOCOL)
        response = self.client.post("/api/generate", json={"protocol": "AWG 2.0", "mtu": 9000})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/generate").status_code, 405)


if __name__ == "__main__":
    unittest.main()
