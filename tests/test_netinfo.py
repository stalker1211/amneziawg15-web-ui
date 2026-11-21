"""Tests for services/netinfo.py: the public IP, the egress probe and GeoIP.

NetInfo as the manager builds it (build_real_manager): its commands go through the
manager's run_command to a FakeSubprocess, and `https_get` is replaced, so the URLs
asked, the source address bound and what each failure leaves behind are pinned down.
The manager's side (the last known IP, a probe recorded on its server) is in
tests/test_system_edges.py.
"""

import json
import threading
import time
import unittest
from typing import Any, ClassVar
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


class GeoIpProviderTests(_Base):
    """A GeoIP provider in ipapi.co's shape, asked first; ipapi.co on any failure."""

    PROVIDER = "https://lan.example/api/geo"
    # lan-dash 1.3's answer: ipapi.co's shape.
    PLACE: ClassVar[dict] = {"ip": "8.8.8.8", "country_code": "US", "country_name": "United States", "city": "Mountain View",
                             "region": "California", "org": "GOOGLE", "asn": "AS15169"}  # fmt: skip

    def setUp(self):
        super().setUp()
        self.net.set_geoip_provider(self.PROVIDER)

    def _lookup(self, provider_answer, ipapi_answer=None, ip="8.8.8.8"):
        """provider_answer / ipapi_answer: a response, or an exception to raise."""
        answers = {f"{self.PROVIDER}/{ip}/json/": provider_answer, f"https://ipapi.co/{ip}/json/": ipapi_answer}

        def get(url, timeout):
            answer = answers[url]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch(f"{MODULE}.https_get", side_effect=get) as https_get:
            result = self.net.lookup_geoip(ip)
        return result, [(c.args[0], c.kwargs["timeout"]) for c in https_get.call_args_list]

    def test_the_provider_answers(self):
        result, calls = self._lookup(_Response(data=self.PLACE))
        self.assertEqual(result, ("United States / Mountain View, California", "US"))
        self.assertEqual(calls, [(f"{self.PROVIDER}/8.8.8.8/json/", 1.0)])  # ipapi.co never asked

    def test_any_failure_falls_back_to_ipapi(self):
        ipapi = _Response(data={"country_name": "Netherlands", "country_code": "NL"})
        failures = {
            "lan-dash, no files": _Response(404, data={"error": True, "reason": "No GeoLite2 files"}),
            "lan-dash, not found": _Response(404, data={"error": True, "reason": "Not in GeoLite2"}),
            "lan-dash, invalid": _Response(400, data={"error": True, "reason": "Invalid IP Address"}),
            "nginx's 401 page": _Response(401, text="<html><title>401 Authorization Required</title>", content_type="text/html"),
            "an HTML 200": _Response(text="<html>", content_type="text/html"),
            "bad JSON": _Response(text="{not json"),
            "a JSON list": _Response(text="[1, 2]"),
            "an error body with 200": _Response(data={"error": True, "reason": "RateLimited"}),
            "no place in it": _Response(data={"ip": "8.8.8.8"}),
            "a timeout": TimeoutError("timed out"),
            "a bad certificate": OSError("CERTIFICATE_VERIFY_FAILED"),
        }  # fmt: skip
        for n, (name, answer) in enumerate(failures.items()):
            with self.subTest(name), self.assertLogs(MODULE, "INFO") as logs:
                result, calls = self._lookup(answer, ipapi, ip=f"8.8.8.{n}")
            self.assertEqual(result, ("Netherlands", "NL"))
            self.assertEqual(
                [url for url, _ in calls], [f"{self.PROVIDER}/8.8.8.{n}/json/", f"https://ipapi.co/8.8.8.{n}/json/"]
            )
            self.assertIn("asking ipapi.co", logs.output[0])
            if name == "lan-dash, no files":
                self.assertIn("(HTTP 404: No GeoLite2 files)", logs.output[0])  # the reason, for the log

    def test_both_failing_is_cached_as_a_failure(self):
        with self.assertLogs(MODULE, "INFO"):
            result, calls = self._lookup(OSError("down"), _Response(429))
        self.assertEqual((result, len(calls)), ((None, None), 2))
        entry = self.net._geoip_cache["8.8.8.8"]
        self.assertEqual(entry["ttl"], self.net.GEOIP_FAILURE_TTL_SECONDS)  # retried in 10 minutes, not a day
        self.assertEqual(sorted(entry), ["country_code", "label", "ts", "ttl"])  # never the whole answer

    def test_ipapi_stands_in_only_until_the_provider_is_back(self):
        # The provider is down for a minute: ipapi.co's answer is kept 10 minutes, not
        # a day, so the provider names the address once it answers again.
        with self.assertLogs(MODULE, "INFO"):
            result, _ = self._lookup(OSError("down"), _Response(data={"country": "NL"}))
        self.assertEqual(result, ("NL", "NL"))
        self.assertEqual(self.net._geoip_cache["8.8.8.8"]["ttl"], self.net.GEOIP_FAILURE_TTL_SECONDS)
        _, calls = self._lookup(None)  # within the 10 minutes: the cache answers
        self.assertEqual(calls, [])
        later = time.time() + self.net.GEOIP_FAILURE_TTL_SECONDS + 1
        with mock.patch(f"{MODULE}.time.time", return_value=later):
            result, calls = self._lookup(_Response(data=self.PLACE))
        self.assertEqual((result[1], [url for url, _ in calls]), ("US", [f"{self.PROVIDER}/8.8.8.8/json/"]))
        self.assertEqual(self.net._geoip_cache["8.8.8.8"]["ttl"], self.net.GEOIP_CACHE_TTL_SECONDS)

    def test_a_slow_provider_is_cut_off_by_the_clock(self):
        # http.client's timeout is per socket operation and DNS has none: a provider
        # that keeps the request open past its second is left behind, and ipapi.co asked.
        release = threading.Event()
        self.addCleanup(release.set)

        def get(url, timeout):
            if url.startswith(self.PROVIDER):
                release.wait(5)
                return _Response(data=self.PLACE)
            return _Response(data={"country": "NL"})

        started = time.monotonic()
        with (
            mock.patch.object(self.net, "GEOIP_PROVIDER_TIMEOUT_SECONDS", 0.1),
            mock.patch(f"{MODULE}.https_get", side_effect=get),
            self.assertLogs(MODULE, "INFO") as logs,
        ):
            result = self.net.lookup_geoip("8.8.8.8")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result, ("NL", "NL"))
        self.assertIn("no answer within 0.1 s", logs.output[0])

    def test_a_lookup_from_before_a_provider_change_is_not_kept(self):
        # The setting changes while the old provider is being asked: the cache was
        # cleared for the new source, and the old answer must not refill it.
        def answer_after_a_change(url, timeout):
            self.net.set_geoip_provider("https://other.example")
            return _Response(data=self.PLACE)

        with mock.patch(f"{MODULE}.https_get", side_effect=answer_after_a_change):
            self.assertEqual(self.net.lookup_geoip("8.8.8.8")[1], "US")  # the caller still gets it
        self.assertEqual(self.net._geoip_cache, {})

    def test_the_log_names_the_providers_host_not_its_url(self):
        # A token can only go in the path (a user, password or query is refused).
        self.net.set_geoip_provider("https://geo.example/s3cret-token")
        answers = {"https://geo.example/s3cret-token/8.8.8.8/json/": OSError("down"),
                   "https://ipapi.co/8.8.8.8/json/": _Response(data={"country": "NL"})}  # fmt: skip

        def get(url, timeout):
            answer = answers[url]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch(f"{MODULE}.https_get", side_effect=get), self.assertLogs(MODULE, "INFO") as logs:
            self.net.lookup_geoip("8.8.8.8")
        self.assertIn("GeoIP: geo.example failed for 8.8.8.8 (down)", logs.output[0])
        self.assertNotIn("s3cret", "\n".join(logs.output))

    def test_the_two_never_wait_longer_than_two_seconds(self):
        # The provider timed out after its second: ipapi.co gets what is left of the two.
        clock = iter([100.0, 101.25])
        with mock.patch(f"{MODULE}.time.monotonic", side_effect=lambda: next(clock)), self.assertLogs(MODULE, "INFO"):
            result, calls = self._lookup(TimeoutError("timed out"), _Response(data={"country": "NL"}))
        self.assertEqual(result, ("NL", "NL"))
        self.assertEqual(calls[1], ("https://ipapi.co/8.8.8.8/json/", 0.75))
        # Nothing left: ipapi.co is not asked, and the failure is cached.
        clock = iter([100.0, 101.9])
        with mock.patch(f"{MODULE}.time.monotonic", side_effect=lambda: next(clock)), self.assertLogs(MODULE, "INFO"):
            result, calls = self._lookup(TimeoutError("timed out"), ip="8.8.4.4")
        self.assertEqual((result, len(calls)), ((None, None), 1))

    def test_https_only(self):
        # The setting refuses anything else (tests/test_settings.py); should an http://
        # URL reach NetInfo anyway, https_get refuses to send it and ipapi.co answers.
        from core.helpers import https_get

        self.net.set_geoip_provider("http://lan.example/api/geo")
        ipapi = _Response(data={"country": "NL"})
        with (
            mock.patch(
                f"{MODULE}.https_get", side_effect=lambda url, timeout: ipapi if "ipapi.co" in url else https_get(url, timeout)
            ),
            self.assertLogs(MODULE, "INFO") as logs,
        ):
            self.assertEqual(self.net.lookup_geoip("8.8.8.8"), ("NL", "NL"))
        self.assertIn("Not an https URL", logs.output[0])
        self.assertNotIn("/api/geo", logs.output[0])  # https_get's error names the URL: the host stays

    def test_without_a_provider_only_ipapi_is_asked(self):
        self.net.set_geoip_provider("")
        with mock.patch(f"{MODULE}.https_get", return_value=_Response(data={"country": "NL"})) as get:
            self.assertEqual(self.net.lookup_geoip("8.8.8.8"), ("NL", "NL"))
        self.assertEqual([c.args[0] for c in get.call_args_list], ["https://ipapi.co/8.8.8.8/json/"])
        self.assertEqual(get.call_args.kwargs["timeout"], 2.0)


class GeoIpRecordTests(_Base):
    """⚙'s line under the setting (geoip_status): how the first source asked, the
    provider or ipapi.co without one, has done since the start or the last change."""

    PROVIDER = GeoIpProviderTests.PROVIDER
    PLACE = GeoIpProviderTests.PLACE

    def setUp(self):
        super().setUp()
        self.net.set_geoip_provider(self.PROVIDER)

    def _lookup(self, ip, provider_answer, ipapi_answer=None):
        """One lookup; each answer a response, or an exception to raise. Returns the URLs asked."""

        def get(url, timeout):
            answer = ipapi_answer if url.startswith("https://ipapi.co/") else provider_answer
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch(f"{MODULE}.https_get", side_effect=get) as https_get:
            self.net.lookup_geoip(ip)
        return [c.args[0] for c in https_get.call_args_list]

    def _counts(self):
        status = self.net.geoip_status()
        return (status["answers"], status["failures"], status["failing"])

    def test_answers_and_failures_are_counted_with_the_latest_reason(self):
        status = self.net.geoip_status()
        self.assertEqual(
            {k: status[k] for k in ("source", "provider", "last_answer", "last_failure", "last_error")},
            {"source": "lan.example", "provider": True, "last_answer": None, "last_failure": None, "last_error": None},
        )
        self.assertEqual(self._counts(), (0, 0, 0))
        ipapi = _Response(data={"country": "NL"})
        self._lookup("8.8.8.1", _Response(data=self.PLACE))
        with self.assertLogs(MODULE, "INFO"):
            self._lookup("8.8.8.2", _Response(404, data={"error": True, "reason": "Not in GeoLite2"}), ipapi)
            self._lookup("8.8.8.3", TimeoutError("timed out"), ipapi)
        # ipapi.co placed both, but the record is the provider's: two failures in a row.
        self.assertEqual(self._counts(), (1, 2, 2))
        status = self.net.geoip_status()
        self.assertEqual(status["last_error"], "timed out")
        self.assertGreaterEqual(status["last_failure"], status["last_answer"])
        self._lookup("8.8.8.4", _Response(data=self.PLACE))
        self.assertEqual(self._counts(), (2, 2, 0))  # answering again

    def test_a_place_from_the_cache_is_not_an_ask(self):
        self._lookup("8.8.8.8", _Response(data=self.PLACE))
        self.assertEqual(self._lookup("8.8.8.8", OSError("not asked")), [])
        self.assertEqual(self._counts(), (1, 0, 0))

    def test_a_provider_change_starts_the_record_over(self):
        self._lookup("8.8.8.8", _Response(data=self.PLACE))
        self.net.set_geoip_provider(self.PROVIDER)  # the same one: kept
        self.assertEqual(self._counts(), (1, 0, 0))
        since = self.net.geoip_status()["since"]
        with mock.patch(f"{MODULE}.time.time", return_value=since + 60):
            self.net.set_geoip_provider("https://other.example/geo")
        status = self.net.geoip_status()
        self.assertEqual((status["source"], status["since"], self._counts()), ("other.example", since + 60, (0, 0, 0)))

    def test_without_a_provider_ipapi_has_the_record(self):
        self.net.set_geoip_provider("")
        self._lookup("8.8.8.8", None, _Response(data={"country": "NL"}))
        self._lookup("8.8.4.4", None, _Response(429, data={"error": True, "reason": "RateLimited"}))
        status = self.net.geoip_status()
        self.assertEqual(
            (status["source"], status["provider"], status["last_error"]), ("ipapi.co", False, "HTTP 429: RateLimited")
        )
        self.assertEqual(self._counts(), (1, 1, 1))

    def test_a_lookup_from_before_a_provider_change_is_not_counted(self):
        def answer_after_a_change(url, timeout):
            self.net.set_geoip_provider("https://other.example")
            return _Response(data=self.PLACE)

        with mock.patch(f"{MODULE}.https_get", side_effect=answer_after_a_change):
            self.net.lookup_geoip("8.8.8.8")
        self.assertEqual(self._counts(), (0, 0, 0))

    def test_the_record_names_the_host_never_the_url(self):
        url = "https://geo.example/s3cret-token"
        self.net.set_geoip_provider(url)
        with self.assertLogs(MODULE, "INFO"):
            self._lookup("8.8.8.8", OSError(f"refused: {url}/8.8.8.8/json/"), _Response(data={"country": "NL"}))
        status = self.net.geoip_status()
        self.assertEqual((status["source"], status["last_error"]), ("geo.example", "refused: geo.example/8.8.8.8/json/"))
        self.assertNotIn("s3cret", json.dumps(status))


class GeoIpCheckTests(_Base):
    """⚙'s Test (check_geoip_source): one source, asked once and now; nothing kept."""

    PROVIDER = GeoIpProviderTests.PROVIDER
    PLACE = GeoIpProviderTests.PLACE

    def _check(self, url, answer, public_ip="8.8.4.4"):
        def get(asked, timeout):
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch(f"{MODULE}.https_get", side_effect=get) as https_get:
            result = self.net.check_geoip_source(url, public_ip)
        return result, [(c.args[0], c.kwargs["timeout"]) for c in https_get.call_args_list]

    def test_only_the_source_is_asked_and_nothing_is_kept(self):
        self.net.enable_geoip = False  # a click, not a lookup: asked all the same
        self.net.set_geoip_provider("https://saved.example")  # the URL as typed is asked, not the saved one
        before = self.net.geoip_status()
        result, calls = self._check(self.PROVIDER, _Response(data=self.PLACE))
        self.assertEqual(calls, [(f"{self.PROVIDER}/8.8.4.4/json/", 5.0)])  # longer than a lookup waits
        self.assertEqual(
            {k: v for k, v in result.items() if k != "seconds"},
            {"source": "lan.example", "provider": True, "ip": "8.8.4.4", "is_public_ip": True, "ok": True,
             "label": "United States / Mountain View, California", "country_code": "US", "error": None,
             "budget": 1.0, "slow": False},
        )  # fmt: skip
        self.assertEqual((self.net._geoip_cache, self.net.geoip_status()), ({}, before))

    def test_a_failure_is_said_and_ipapi_is_not_asked(self):
        failures = {
            "lan-dash, not found": (_Response(404, data={"error": True, "reason": "Not in GeoLite2"}), "HTTP 404: Not in GeoLite2"),
            "an HTML page": (_Response(text="<html>", content_type="text/html"), "not JSON (text/html)"),
            "no place in it": (_Response(data={"ip": "8.8.4.4"}), "no country or place in the answer"),
            "down": (OSError("Connection refused"), "Connection refused"),
        }  # fmt: skip
        for name, (answer, error) in failures.items():
            with self.subTest(name):
                result, calls = self._check(self.PROVIDER, answer)
                self.assertEqual((result["ok"], result["error"], result["label"], result["slow"]), (False, error, None, False))
                self.assertEqual(len(calls), 1)

    def test_a_slow_answer_is_told_from_a_dead_one(self):
        clock = iter([100.0, 101.5])
        with mock.patch(f"{MODULE}.time.monotonic", side_effect=lambda: next(clock)):
            result, _ = self._check(self.PROVIDER, _Response(data=self.PLACE))
        self.assertEqual((result["ok"], result["seconds"], result["budget"], result["slow"]), (True, 1.5, 1.0, True))
        # ipapi.co, as the only source, has a lookup's two seconds.
        clock = iter([100.0, 101.5])
        with mock.patch(f"{MODULE}.time.monotonic", side_effect=lambda: next(clock)):
            result, calls = self._check("", _Response(data={"country": "NL"}))
        self.assertEqual(calls[0][0], "https://ipapi.co/8.8.4.4/json/")
        self.assertEqual(
            (result["source"], result["provider"], result["budget"], result["slow"]), ("ipapi.co", False, 2.0, False)
        )

    def test_without_a_public_ip_it_asks_about_a_stand_in(self):
        for public_ip in (None, "", "192.168.1.2", "203.0.113.9"):
            with self.subTest(public_ip):
                result, calls = self._check(self.PROVIDER, _Response(data=self.PLACE), public_ip=public_ip)
                self.assertEqual((result["ip"], result["is_public_ip"]), ("8.8.8.8", False))
                self.assertEqual(calls[0][0], f"{self.PROVIDER}/8.8.8.8/json/")

    def test_the_error_names_the_host_never_the_url(self):
        url = "https://geo.example/s3cret-token"
        result, _ = self._check(url, OSError(f"refused: {url}/8.8.4.4/json/"))
        self.assertEqual((result["source"], result["error"]), ("geo.example", "refused: geo.example/8.8.4.4/json/"))
        self.assertNotIn("s3cret", json.dumps(result))


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
        cases: tuple[tuple[dict[str, Any], str], ...] = (
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

    def _put(self, ip, ts, place=(None, None)):
        self.net._cache_geoip(self.net.geoip_provider, ip, ts, place, self.net.GEOIP_CACHE_TTL_SECONDS)

    def test_cache_is_bounded(self):
        limit = self.net.GEOIP_CACHE_MAX_ENTRIES
        for n in range(limit + 50):
            self._put(f"198.51.{n // 256}.{n % 256}", 1000.0)
        self.assertLessEqual(len(self.net._geoip_cache), limit)

    def test_expired_entries_are_evicted_first(self):
        limit = self.net.GEOIP_CACHE_MAX_ENTRIES
        ttl = self.net.GEOIP_CACHE_TTL_SECONDS
        now = 100000.0
        # Fill with stale entries, then add one fresh entry past the limit.
        for n in range(limit):
            self._put(f"203.0.{n // 256}.{n % 256}", now - ttl - 1)
        self._put("192.0.2.7", now, ("Somewhere", "SE"))
        self.assertIn("192.0.2.7", self.net._geoip_cache)
        self.assertLess(len(self.net._geoip_cache), limit)

    def test_oldest_evicted_when_all_entries_are_fresh(self):
        limit = self.net.GEOIP_CACHE_MAX_ENTRIES
        for n in range(limit):
            self._put(f"203.1.{n // 256}.{n % 256}", 1000.0 + n)
        oldest = "203.1.0.0"
        self.assertIn(oldest, self.net._geoip_cache)
        self._put("192.0.2.9", 99999.0)
        self.assertNotIn(oldest, self.net._geoip_cache)
        self.assertIn("192.0.2.9", self.net._geoip_cache)


if __name__ == "__main__":
    unittest.main()
