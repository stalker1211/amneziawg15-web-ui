"""Tests for services/netinfo.py: the public IP, the egress probe and GeoIP.

NetInfo as the manager builds it (build_real_manager): its commands go through the
manager's run_command to a FakeSubprocess, and `https_get` is replaced, so the URLs
asked, the source address bound and what each failure leaves behind are pinned down.
The manager's side (the last known IP, a probe recorded on its server) is in
tests/test_system_edges.py.
"""

import json
import unittest
from unittest import mock

# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access,wrong-import-order
from tests.support import SystemPaths, build_manager, build_real_manager

MODULE = "services.netinfo"


def _Response(status=200, text="", data=None, content_type="application/json"):
    """What https_get returns."""
    from core.helpers import HttpsResponse

    return HttpsResponse(status, content_type, json.dumps(data) if data is not None else text)


class _Base(unittest.TestCase):
    def setUp(self):
        self.paths = SystemPaths().start(self)
        self.manager, self.fake = build_real_manager(self)
        self.net = self.manager.netinfo


class PublicIpTests(_Base):
    def _detect(self):
        return self.net.detect_public_ip()

    def test_first_valid_answer_wins(self):
        answers = {
            "https://api.ipify.org": _Response(500),
            "https://ident.me": _Response(text="<html>not an ip</html>"),
            "https://icanhazip.com": _Response(text="198.51.100.7\n"),
        }
        with (
            mock.patch(f"{MODULE}.https_get", side_effect=lambda url, timeout: answers[url]) as get,
            self.assertLogs(MODULE, "INFO"),
        ):
            self.assertEqual(self._detect(), "198.51.100.7")
        self.assertTrue(all(call.args[0].startswith("https://") for call in get.call_args_list))

    def test_none_when_nothing_answers_and_no_local_guess(self):
        # The old fallbacks (the route source address, "YOUR_SERVER_IP") would have
        # gone into every client config's Endpoint.
        with mock.patch(f"{MODULE}.https_get", side_effect=OSError("offline")), self.assertLogs(MODULE, "WARNING"):
            self.assertIsNone(self._detect())
        self.assertEqual(self.fake.calls, [])


class GeoIpTests(_Base):
    def _lookup(self, ip, response=None, side_effect=None):
        with mock.patch(f"{MODULE}.https_get", return_value=response, side_effect=side_effect) as get:
            return self.net.lookup_geoip(ip), get

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
            self.net._geoip_cache.clear()
            first, _ = self._lookup(ip, **kwargs)
            second, get = self._lookup(ip, response=_Response(data={"country": "FR"}))
            self.assertEqual(second, first)
            get.assert_not_called()

    def test_a_failure_is_retried_after_ten_minutes_an_answer_after_a_day(self):
        with mock.patch(f"{MODULE}.time.time", return_value=1_000_000):
            self._lookup("9.9.9.9", side_effect=OSError("down"))
            self._lookup("1.0.0.1", _Response(data={"country": "NL"}))
        with mock.patch(f"{MODULE}.time.time", return_value=1_000_000 + 11 * 60):
            retried, get = self._lookup("9.9.9.9", _Response(data={"country": "FR"}))
            self.assertEqual(retried, ("FR", "FR"))
            kept, get = self._lookup("1.0.0.1", _Response(data={"country": "DE"}))
            self.assertEqual(kept, ("NL", "NL"))
            get.assert_not_called()

    def test_the_cached_lookup_never_waits_and_resolves_in_the_background(self):
        # The traffic loop reads the cache only; a miss is looked up in a background task.
        tasks = []
        self.manager.start_background_task = lambda target: tasks.append(target)  # reaches netinfo
        with mock.patch(f"{MODULE}.https_get") as get:
            self.assertEqual(self.net.lookup_geoip_cached("8.8.8.8"), (None, None))
            self.assertEqual(self.net.lookup_geoip_cached("8.8.8.8"), (None, None))
            self.assertEqual(self.net.lookup_geoip_cached("10.0.0.1"), (None, None))
            get.assert_not_called()
        self.assertEqual(len(tasks), 1)  # one lookup per address, however often it is asked
        with mock.patch(f"{MODULE}.https_get", return_value=_Response(data={"country": "US"})):
            tasks[0]()
        self.assertEqual(self.net.lookup_geoip_cached("8.8.8.8"), ("US", "US"))
        self.assertEqual(self.net._geoip_pending, set())

    def test_disabled(self):
        self.net.enable_geoip = False
        result, get = self._lookup("8.8.8.8", _Response(data={"country": "US"}))
        self.assertEqual(result, (None, None))
        get.assert_not_called()


class EgressProbeTests(_Base):
    def test_source_and_service_are_validated(self):
        with self.assertRaises(ValueError):
            self.net.detect_public_ip_from_source("not-an-ip", "https://api.ipify.org")
        with self.assertRaises(ValueError):
            self.net.detect_public_ip_from_source("10.0.0.1", "https://evil.example")

    def test_request_is_bound_to_the_source_address(self):
        with mock.patch(f"{MODULE}.https_get", return_value=_Response(text="198.51.100.9\n")) as get:
            result = self.net.detect_public_ip_from_source("10.0.0.1", "https://ident.me")
        self.assertEqual(result, ("198.51.100.9", "https://ident.me"))
        self.assertEqual(get.call_args.kwargs["source_ip"], "10.0.0.1")

    def test_bad_answers_raise_with_the_service_named_once(self):
        cases = (
            ({"return_value": _Response(503)}, r"^https://ident\.me: HTTP 503$"),
            ({"return_value": _Response(text="nope")}, r"^https://ident\.me: invalid IP response 'nope'$"),
            ({"side_effect": OSError("x")}, r"^https://ident\.me: x$"),
        )
        for kwargs, message in cases:
            with mock.patch(f"{MODULE}.https_get", **kwargs), self.assertRaisesRegex(RuntimeError, message):
                self.net.detect_public_ip_from_source("10.0.0.1", "https://ident.me")

    def test_service_rotation(self):
        services = list(self.net.EGRESS_PROBE_SERVICES)
        self.assertEqual(self.net.get_next_egress_probe_service({}), services[0])
        for n, previous in enumerate(services):
            server = {"egress_probe": {"service": previous}}
            self.assertEqual(self.net.get_next_egress_probe_service(server), services[(n + 1) % len(services)])
        self.assertEqual(self.net.get_next_egress_probe_service({"egress_probe": {"service": "gone"}}), services[0])

    def test_service_name(self):
        self.assertEqual(self.net.format_probe_service_name("https://api.ipify.org"), "api.ipify.org")
        self.assertEqual(self.net.format_probe_service_name("ident.me"), "ident.me")
        self.assertIsNone(self.net.format_probe_service_name(""))
        self.assertIsNone(self.net.format_probe_service_name(None))

    def test_route_lookup(self):
        self.fake.respond(["ip", "route", "get", "1.1.1.1", "from", "10.0.0.1"],
                          "1.1.1.1 from 10.0.0.1 via 192.168.1.1 dev eth0 src 10.0.0.1 uid 0\n    cache")  # fmt: skip
        route = self.net.get_route_for_source_ip("10.0.0.1")
        self.assertEqual((route["dev"], route["via"], route["src"]), ("eth0", "192.168.1.1", "10.0.0.1"))
        self.assertNotIn("cache", route["raw"])

        self.fake.calls.clear()
        self.assertIsNone(self.net.get_route_for_source_ip("bogus")["dev"])
        self.assertEqual(self.fake.calls, [])

        self.fake.respond(["ip", "route"], 2)
        self.assertTrue(self.net.get_route_for_source_ip("10.0.0.1")["raw"].startswith("route lookup failed"))


class GeoipCacheTests(unittest.TestCase):
    def setUp(self):
        self.net = build_manager().netinfo

    def test_cache_is_bounded(self):
        limit = self.net.GEOIP_CACHE_MAX_ENTRIES
        for n in range(limit + 50):
            self.net._cache_geoip(f"198.51.{n // 256}.{n % 256}", 1000.0, None, None, {})
        self.assertLessEqual(len(self.net._geoip_cache), limit)

    def test_expired_entries_are_evicted_first(self):
        limit = self.net.GEOIP_CACHE_MAX_ENTRIES
        ttl = self.net.GEOIP_CACHE_TTL_SECONDS
        now = 100000.0
        # Fill with stale entries, then add one fresh entry past the limit.
        for n in range(limit):
            self.net._cache_geoip(f"203.0.{n // 256}.{n % 256}", now - ttl - 1, None, None, {})
        self.net._cache_geoip("192.0.2.7", now, "Somewhere", "SE", {})
        self.assertIn("192.0.2.7", self.net._geoip_cache)
        self.assertLess(len(self.net._geoip_cache), limit)

    def test_oldest_evicted_when_all_entries_are_fresh(self):
        limit = self.net.GEOIP_CACHE_MAX_ENTRIES
        for n in range(limit):
            self.net._cache_geoip(f"203.1.{n // 256}.{n % 256}", 1000.0 + n, None, None, {})
        oldest = "203.1.0.0"
        self.assertIn(oldest, self.net._geoip_cache)
        self.net._cache_geoip("192.0.2.9", 99999.0, None, None, {})
        self.assertNotIn(oldest, self.net._geoip_cache)
        self.assertIn("192.0.2.9", self.net._geoip_cache)


if __name__ == "__main__":
    unittest.main()
