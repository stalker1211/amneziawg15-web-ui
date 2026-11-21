"""The panel's view of the network: the public IP, the egress probe and GeoIP.

The three outbound calls, all HTTPS through core.helpers.https_get: the public IP
(api.ipify.org and two fallbacks), the egress probe bound to a tunnel's address (where
that tunnel's traffic exits), and the country and city of an address, cached: from
ipapi.co, or first from a GeoIP provider in its shape (the geoip_provider setting). ⚙
shows how that source has done (geoip_status) and can ask it once (check_geoip_source).
Nothing here reads or writes web_config.json: the manager keeps the public IP it hands
to clients, and records each probe on its server (probe_server_egress_ip).
"""

import ipaddress
import json
import re
import threading
import time
from urllib.parse import urlparse

from core.helpers import https_get, is_valid_ip
from core.logging_setup import get_logger

logger = get_logger(__name__)


class NetInfo:
    """Public IP, egress probe and GeoIP. `run_command` (argv -> stdout or None) and
    `start_background_task` (runs a callable off the request thread) come from the
    manager, which owns both."""

    # GeoIP lookups are cached to avoid rate limits; bounded so the dict cannot grow
    # without limit as new client endpoints appear.
    GEOIP_CACHE_TTL_SECONDS = 24 * 3600
    # A failed lookup (rate limit, timeout) is retried after this, not after a day; so
    # is ipapi.co's answer for a failed provider, which is asked again once it is back.
    GEOIP_FAILURE_TTL_SECONDS = 10 * 60
    GEOIP_CACHE_MAX_ENTRIES = 512
    # ipapi.co: the default, and the fallback when a GeoIP provider is set and fails.
    GEOIP_DEFAULT_URL = "https://ipapi.co"
    # A lookup never waits longer than this, the provider and the fallback together
    # (a wall-clock limit on each request: _within).
    GEOIP_TIMEOUT_SECONDS = 2.0
    GEOIP_PROVIDER_TIMEOUT_SECONDS = 1.0
    # Less than this left after the provider: no time to ask ipapi.co.
    GEOIP_MIN_TIMEOUT_SECONDS = 0.2
    # ⚙'s Test waits longer than a lookup, so a slow source is told from a dead one, and
    # asks about this address when the panel has no public one of its own.
    GEOIP_TEST_TIMEOUT_SECONDS = 5.0
    GEOIP_TEST_ADDRESS = "8.8.8.8"

    EGRESS_PROBE_SERVICES = (
        "https://api.ipify.org",
        "https://ident.me",
        "https://icanhazip.com",
    )
    PUBLIC_IP_SERVICES = EGRESS_PROBE_SERVICES

    def __init__(self, *, run_command, start_background_task, enable_geoip=True):
        self.run_command = run_command
        self.start_background_task = start_background_task
        self.enable_geoip = enable_geoip
        # A base URL answering in ipapi.co's shape, asked first; "" asks only ipapi.co.
        self.geoip_provider = ""
        # Cache GeoIP lookups to avoid rate limits and latency
        # { ip: {"ts": epoch_seconds, "ttl": seconds, "label": str, "country_code": str} }
        self._geoip_cache = {}
        # Request threads and background lookups write the cache, and a provider
        # change clears it: each under this lock.
        self._geoip_lock = threading.Lock()
        # Addresses a background task is looking up now (lookup_geoip_cached).
        self._geoip_pending = set()
        # How the first source asked (the provider, or ipapi.co without one) has done
        # since the panel started or the provider changed: ⚙'s line under the setting
        # (geoip_status). Memory only, like the cache; under the same lock.
        self._geoip_record = _new_record()

    def detect_public_ip(self):
        """The host's public IPv4 address, or None when no service answered.

        None rather than a guess: the answer goes into every client config's Endpoint.
        The old fallbacks -- the `ip route get` source (the LAN address on macvlan) or
        "YOUR_SERVER_IP" -- sent every device to the wrong place after a refresh
        during an outage. HTTPS only, so nobody on the path can supply the answer.
        """
        for service in self.PUBLIC_IP_SERVICES:
            try:
                response = https_get(service, timeout=5)
            except Exception:  # pylint: disable=broad-exception-caught  -- try the next one
                continue
            ip = response.text.strip() if response.status == 200 else ""
            if is_valid_ip(ip):
                logger.info("Detected public IP: %s", ip)
                return ip
        logger.warning("Could not detect the public IP: none of %s answered", ", ".join(self.PUBLIC_IP_SERVICES))
        return None

    def detect_public_ip_from_source(self, source_ip, service):
        """Detect external IP for traffic originating from a specific source IP."""
        if not is_valid_ip(source_ip):
            raise ValueError(f"Invalid source IP: {source_ip}")

        if service not in self.EGRESS_PROBE_SERVICES:
            raise ValueError(f"Unsupported egress probe service: {service}")

        # Bound to the tunnel's address, so the answer is where that tunnel's traffic exits.
        try:
            response = https_get(service, timeout=8, source_ip=source_ip)
        except Exception as e:
            raise RuntimeError(f"{service}: {e}") from e
        if response.status != 200:
            raise RuntimeError(f"{service}: HTTP {response.status}")
        body = response.text.strip()
        if body and is_valid_ip(body):
            return body, service
        raise RuntimeError(f"{service}: invalid IP response '{body[:120]}'")

    def get_next_egress_probe_service(self, server):
        """Rotate egress probe services for a server across refreshes."""
        previous_service = None
        probe = server.get("egress_probe") if isinstance(server, dict) else None
        if isinstance(probe, dict):
            previous_service = probe.get("service")

        services = list(self.EGRESS_PROBE_SERVICES)
        if previous_service in services:
            previous_index = services.index(previous_service)
            return services[(previous_index + 1) % len(services)]

        return services[0]

    def format_probe_service_name(self, service):
        """Return a short host label for a probe service URL."""
        if not service or not isinstance(service, str):
            return None

        try:
            parsed = urlparse(service)
            host = (parsed.hostname or "").strip().lower()
            return host or service.strip()
        except Exception:
            return service.strip()

    def get_route_for_source_ip(self, source_ip, destination="1.1.1.1"):
        """Return Linux route decision for destination when source IP is forced."""
        result = {
            "destination": destination,
            "source_ip": source_ip,
            "raw": "",
            "dev": None,
            "via": None,
            "src": None,
        }

        if not is_valid_ip(source_ip):
            return result

        output = self.run_command(["ip", "route", "get", destination, "from", source_ip])
        if output is None:
            result["raw"] = "route lookup failed"
            return result

        line = (output.splitlines() or [""])[0]
        result["raw"] = line
        for key in ("dev", "via", "src"):
            match = re.search(rf"\b{key}\s+(\S+)", line)
            result[key] = match.group(1) if match else None
        return result

    def _cache_geoip(self, provider, ip, now, place, ttl):
        """Keep `place` (label, country code) for `ttl` seconds, evicting expired and
        then oldest entries. `provider` is the one the lookup started with: when the
        setting changed since, the cache was cleared for the new source, and nothing
        is stored.

        Unbounded growth was slow but real: one entry per distinct client endpoint IP,
        never removed.
        """
        label, country_code = place
        with self._geoip_lock:
            if provider != self.geoip_provider:
                return
            self._geoip_cache[ip] = {"ts": now, "ttl": ttl, "label": label, "country_code": country_code}

            if len(self._geoip_cache) <= self.GEOIP_CACHE_MAX_ENTRIES:
                return

            for key, entry in list(self._geoip_cache.items()):
                if (now - entry["ts"]) >= entry["ttl"]:
                    del self._geoip_cache[key]

            # Still over budget (many fresh entries): drop the oldest.
            if len(self._geoip_cache) > self.GEOIP_CACHE_MAX_ENTRIES:
                for key, _ in sorted(self._geoip_cache.items(), key=lambda kv: kv[1]["ts"]):
                    if len(self._geoip_cache) <= self.GEOIP_CACHE_MAX_ENTRIES:
                        break
                    del self._geoip_cache[key]

    def _geoip_candidate(self, ip):
        """The stripped address when GeoIP is on and it is public, else None."""
        if not self.enable_geoip or not ip or not isinstance(ip, str):
            return None
        ip = ip.strip()
        return ip if _is_public(ip) else None

    def _geoip_fresh(self, ip, now):
        """The cached (label, country code), or None when absent or expired."""
        cached = self._geoip_cache.get(ip)
        if not isinstance(cached, dict) or (now - cached["ts"]) >= cached["ttl"]:
            return None
        return (cached.get("label"), cached.get("country_code"))

    def lookup_geoip_cached(self, ip):
        """(label, country code) from the cache only; a miss is looked up in the background.

        For the traffic loop, which must not wait on GeoIP (up to 2 s per new
        client endpoint): the label appears on the next tick.
        """
        ip = self._geoip_candidate(ip)
        if not ip:
            return (None, None)
        fresh = self._geoip_fresh(ip, time.time())
        if fresh is not None:
            return fresh
        if ip not in self._geoip_pending:
            self._geoip_pending.add(ip)

            def resolve():
                try:
                    self.lookup_geoip(ip)
                finally:
                    self._geoip_pending.discard(ip)

            self.start_background_task(resolve)
        return (None, None)

    def set_geoip_provider(self, url):
        """Ask `url` (an https base URL in ipapi.co's shape, as the setting normalizes
        it) first, or only ipapi.co when "". A change forgets the cached places, so the
        new source names them, and starts its record over (geoip_status)."""
        with self._geoip_lock:
            if url != self.geoip_provider:
                self.geoip_provider = url
                self._geoip_cache.clear()
                self._geoip_record = _new_record()

    def _record_geoip(self, provider, error=None):
        """Count an answer (`error` None) or a failure and its reason from the first
        source asked, unless the provider changed while it was asked (as _cache_geoip)."""
        now = time.time()
        with self._geoip_lock:
            if provider != self.geoip_provider:
                return
            record = self._geoip_record
            if error is None:
                record.update(answers=record["answers"] + 1, failing=0, last_answer=now)
            else:
                record.update(failures=record["failures"] + 1, failing=record["failing"] + 1)
                record.update(last_failure=now, last_error=error[:200])

    def geoip_status(self):
        """For ⚙: the first source asked (the provider's host, never its URL, or
        ipapi.co), whether it is a provider, and its record since `since` (the panel's
        start or the provider's last change): answers and failures, `failing` (the
        failures since the last answer), when each last happened (epoch seconds) and
        the latest failure's reason. Lookups the cache answers are not counted."""
        with self._geoip_lock:
            provider, record = self.geoip_provider, dict(self._geoip_record)
        return {"source": _host(provider or self.GEOIP_DEFAULT_URL), "provider": bool(provider), **record}

    def check_geoip_source(self, url, public_ip):
        """Ask `url` (a provider as the setting normalizes it, "" for ipapi.co) where
        `public_ip` is, once and now, for ⚙'s Test button: no cache, no fallback, and
        nothing counted or kept. GEOIP_TEST_ADDRESS stands in for an address that is
        not public (none detected yet).

        It waits GEOIP_TEST_TIMEOUT_SECONDS, longer than a lookup gives the source
        (`budget`), so a slow source is told from a dead one: `slow` is an answer
        after the budget, which a lookup would not have waited for.
        """
        own = isinstance(public_ip, str) and _is_public(public_ip)
        ip = public_ip.strip() if own else self.GEOIP_TEST_ADDRESS
        base = url or self.GEOIP_DEFAULT_URL
        host = _host(base)
        budget = self.GEOIP_PROVIDER_TIMEOUT_SECONDS if url else self.GEOIP_TIMEOUT_SECONDS
        label = country_code = error = None
        started = time.monotonic()
        try:
            label, country_code = self._place_at(base, ip, self.GEOIP_TEST_TIMEOUT_SECONDS)
        except Exception as exc:  # pylint: disable=broad-exception-caught  -- reported, not raised
            # Its host, never the URL: the path may carry a token.
            error = str(exc).replace(base, host)[:200]
        seconds = round(time.monotonic() - started, 3)
        return {
            "source": host,
            "provider": bool(url),
            "ip": ip,
            "is_public_ip": own,
            "ok": error is None,
            "label": label,
            "country_code": country_code,
            "error": error,
            "seconds": seconds,
            "budget": budget,
            "slow": error is None and seconds > budget,
        }

    @staticmethod
    def _ask_geoip(base_url, ip, timeout):
        """`<base_url>/<ip>/json/` as a dict, or ValueError/OSError naming what went wrong:
        not a 200, not JSON, an `"error": true` body (ipapi.co's and lan-dash's), or no
        country or place in it."""
        response = _within(timeout, lambda: https_get(f"{base_url}/{ip}/json/", timeout=timeout))
        try:
            data = json.loads(response.text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {}
        if response.status != 200 or data.get("error"):
            reason = data.get("reason") or data.get("message")
            raise ValueError(f"HTTP {response.status}" + (f": {str(reason)[:120]}" if reason else ""))
        if not response.content_type.startswith("application/json"):
            raise ValueError(f"not JSON ({response.content_type or 'no content type'})")
        if not data:
            raise ValueError("an empty or invalid JSON body")
        return data

    def _place_at(self, base_url, ip, timeout):
        """(label, country code) from one service; raises when it has no place."""
        data = self._ask_geoip(base_url, ip, timeout)
        label, country_code = _geo_label(data), _country_code(data)
        if not label and not country_code:
            raise ValueError("no country or place in the answer")
        return (label, country_code)

    def lookup_geoip(self, ip):
        """Return (geo label, country code) for a public IP with caching.

        Asks the GeoIP provider when one is set, then ipapi.co if it fails in any way
        (an error, a timeout, an answer without a place). The two share 2 s: the
        provider gets at most half, ipapi.co what is left. ipapi.co's answer for a
        failed provider is kept only as long as a failure, so the provider names the
        address once it is back.
        """
        ip = self._geoip_candidate(ip)
        if not ip:
            return (None, None)

        now = time.time()
        fresh = self._geoip_fresh(ip, now)
        if fresh is not None:
            return fresh

        provider = self.geoip_provider
        timeout = self.GEOIP_TIMEOUT_SECONDS
        if provider:
            started = time.monotonic()
            try:
                place = self._place_at(provider, ip, self.GEOIP_PROVIDER_TIMEOUT_SECONDS)
            except Exception as exc:  # pylint: disable=broad-exception-caught  -- any failure falls back
                # Its host, never the URL: the path may carry a token.
                host = _host(provider)
                reason = str(exc).replace(provider, host)
                logger.info("GeoIP: %s failed for %s (%s); asking ipapi.co", host, ip, reason)
                self._record_geoip(provider, reason)
            else:
                self._record_geoip(provider)
                self._cache_geoip(provider, ip, now, place, self.GEOIP_CACHE_TTL_SECONDS)
                return place
            timeout -= time.monotonic() - started
        place, ttl = (None, None), self.GEOIP_FAILURE_TTL_SECONDS
        if timeout >= self.GEOIP_MIN_TIMEOUT_SECONDS:
            try:
                place = self._place_at(self.GEOIP_DEFAULT_URL, ip, timeout)
            except Exception as exc:  # pylint: disable=broad-exception-caught  -- cached as a failure below
                if not provider:  # ipapi.co was the first source: its record
                    self._record_geoip(provider, str(exc))
            else:
                if not provider:
                    ttl = self.GEOIP_CACHE_TTL_SECONDS
                    self._record_geoip(provider)
        self._cache_geoip(provider, ip, now, place, ttl)
        return place


def _new_record():
    """A GeoIP source's record, empty, counting from now (NetInfo.geoip_status)."""
    return {
        "since": time.time(),
        "answers": 0,
        "failures": 0,
        "failing": 0,
        "last_answer": None,
        "last_failure": None,
        "last_error": None,
    }


def _host(url):
    """A source's host name, for the log and ⚙: never the URL, whose path may carry a token."""
    return urlparse(url).hostname or "the provider"


def _is_public(ip):
    """Whether GeoIP can place `ip`: an address that is not private, loopback,
    link-local, multicast, reserved or unspecified."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _within(timeout, call):
    """call() on a daemon thread, waited for at most `timeout` seconds in all.

    http.client's timeout bounds each socket operation (each address's connect, the TLS
    handshake, each read), not the request, and the name lookup not at all. A request
    still running when the wait ends finishes on its own, and its answer is dropped.
    """
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except Exception as exc:  # pylint: disable=broad-exception-caught  -- raised in the caller
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="geoip", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"no answer within {timeout:.1f} s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _geo_label(raw):
    """ "Country / City, Region" from an ipapi.co-shaped answer (or ip-api.com's keys)."""
    country = raw.get("country_name") or raw.get("country") or raw.get("countryCode")
    loc = ", ".join(str(p) for p in (raw.get("city"), raw.get("region") or raw.get("regionName")) if p).strip()
    if country and loc:
        return f"{country} / {loc}"
    if country:
        return str(country)
    return loc or None


def _country_code(raw):
    """The two-letter country code, upper-cased, or None."""
    cc = raw.get("country_code") or raw.get("countryCode") or raw.get("country")
    if isinstance(cc, str):
        cc = cc.strip().upper()
        if re.fullmatch(r"[A-Z]{2}", cc):
            return cc
    return None
