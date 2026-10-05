"""Tests for the auth model: nginx's Basic Auth is the only gate, on every proxied path.

Flask listens on 127.0.0.1 only (pinned in test_http_api), so it trusts that anything
reaching it passed nginx. Until 2.5 /socket.io/ was the exception, gated by a session
cookie instead of the password; live updates are now GET /api/events, an ordinary
request under /api/. So both sides are checked here: nginx asks for the password on
every location that proxies to Flask but the localhost-only /status, and Flask keeps
no session and sets no cookie.
"""

import re
import unittest
from pathlib import Path

from tests.support import build_app

NGINX_CONF = Path(__file__).resolve().parent.parent / "config" / "nginx.conf"


class NoSessionTests(unittest.TestCase):
    """Nothing but Basic Auth: no cookie to carry a credential, no key to keep."""

    def setUp(self):
        self.app, _ = build_app()

    def test_no_response_sets_a_cookie(self):
        client = self.app.test_client()
        for path in ("/api/servers", "/api/system/status"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("Set-Cookie", response.headers, path)

    def test_the_app_has_no_secret_key(self):
        self.assertIsNone(self.app.secret_key)

    def test_the_event_stream_is_under_api(self):
        # So nginx's /api/ location, Basic Auth and all, covers it.
        rules = {rule.rule for rule in self.app.url_map.iter_rules() if "events" in rule.rule}
        self.assertEqual(rules, {"/api/events"})


def _nginx_text():
    """nginx.conf with its comments stripped."""
    return "\n".join(line.split("#", 1)[0] for line in NGINX_CONF.read_text(encoding="utf-8").splitlines())


def _nginx_locations():
    """Map each `location <path>` to its directives (one nested block, such as an `if`,
    included), comments stripped."""
    pattern = r"location\s+(\S+)\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
    return {m.group(1): m.group(2) for m in re.finditer(pattern, _nginx_text())}


class NginxAuthConfigTests(unittest.TestCase):
    """The first gate: Flask trusts that anything reaching it passed Basic Auth."""

    def setUp(self):
        self.locations = _nginx_locations()

    def test_expected_locations_exist(self):
        self.assertEqual(set(self.locations), {"/", "/api/", "/static/", "/status"})

    def test_every_proxy_to_flask_requires_basic_auth(self):
        # /status alone is open, and only to localhost (the Docker healthcheck).
        proxied = [path for path, body in self.locations.items() if "proxy_pass" in body and path != "/status"]
        self.assertEqual(sorted(proxied), ["/", "/api/"])
        for path in proxied:
            self.assertRegex(self.locations[path], r"auth_basic\s+\"", path)
            self.assertIn("auth_basic_user_file /etc/amnezia/.htpasswd;", self.locations[path], path)

    def test_no_websocket_is_proxied(self):
        # The upgrade headers went with /socket.io/ (2.5).
        self.assertNotRegex(_nginx_text(), r"(?i)upgrade")

    def test_status_is_localhost_only(self):
        body = self.locations["/status"]
        self.assertIn("allow 127.0.0.1;", body)
        self.assertIn("deny all;", body)
        # allow sees the realip address, which a trusted proxy's X-Forwarded-For sets;
        # the connection's own address must be loopback too.
        self.assertRegex(body, r'if \(\$realip_remote_addr != "127\.0\.0\.1"\) \{\s*return 403;\s*\}')

    def test_per_setting_directives_come_from_the_generated_include(self):
        # core/nginx_conf.py writes them (the access_log by the log level, error_log
        # stderr, the trusted proxies); written here too, they would fight the settings.
        server = _nginx_text().split("server {", 1)[1]
        self.assertIn("include /etc/nginx/awg/settings.conf;", server)
        for directive in ("set_real_ip_from", "access_log", "error_log stderr"):
            self.assertNotIn(directive, _nginx_text(), directive)
        # The gates the include picks from, and the error log file authlog reads.
        for gate in ("$awg_log_debug", "$awg_log_warning", "$awg_log_error"):
            self.assertIn(f'map "$awg_quiet$status" {gate}', _nginx_text())
        self.assertIn("error_log /var/log/nginx/error.log warn;", server)

    def test_auth_is_never_switched_off(self):
        self.assertNotRegex(NGINX_CONF.read_text(encoding="utf-8"), r"auth_basic\s+off")

    def test_security_headers_on_every_response(self):
        conf = NGINX_CONF.read_text(encoding="utf-8")
        csp = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', conf).group(1)
        for directive in ("script-src 'self'", "object-src 'none'", "frame-ancestors 'none'"):
            self.assertIn(directive, csp)
        self.assertIn('add_header X-Content-Type-Options "nosniff" always;', conf)
        self.assertIn('add_header Referrer-Policy "no-referrer" always;', conf)
        # nginx inherits add_header only into a location that sets none of its own.
        for path, body in self.locations.items():
            self.assertNotIn("add_header", body, path)


if __name__ == "__main__":
    unittest.main()
