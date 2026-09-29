"""Tests for the auth model: the persisted session secret, the /socket.io/ cookie, and nginx.

These run the production code in core/guards.py and core/runtime.py. nginx is the
first gate (Basic Auth on / and /api/), Flask records that in a session cookie, and
the Socket.IO connect handler admits only that cookie -- so all three are checked.
"""

import os
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import PUBLIC_IP, build_app

NGINX_CONF = Path(__file__).resolve().parent.parent / "config" / "nginx.conf"


class SecretKeyTests(unittest.TestCase):
    def setUp(self):
        from core.guards import load_or_create_secret_key

        self.load = load_or_create_secret_key
        self.path = os.path.join(tempfile.mkdtemp(prefix="awg-key-"), "sub", ".flask_secret_key")

    def test_created_owner_only_in_a_missing_directory(self):
        key = self.load(self.path)
        self.assertEqual(len(key), 24)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), key)

    def test_existing_key_is_reused_across_restarts(self):
        first = self.load(self.path)
        self.assertEqual(self.load(self.path), first)

    def test_empty_file_is_replaced_with_a_fresh_key(self):
        os.makedirs(os.path.dirname(self.path))
        Path(self.path).write_bytes(b"")
        key = self.load(self.path)
        self.assertEqual(len(key), 24)
        self.assertEqual(Path(self.path).read_bytes(), key)


class SessionCookieTests(unittest.TestCase):
    """mark_authenticated(): any request that reached Flask passed nginx's Basic Auth."""

    def setUp(self):
        self.app, _ = build_app()
        self.client = self.app.test_client()

    def test_api_request_sets_a_long_lived_http_only_cookie(self):
        self.assertEqual(self.client.get("/api/servers").status_code, 200)
        cookie = self.client.get_cookie("session")
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie.http_only)
        # Permanent (a year), so a browser restart does not drop the WebSocket auth.
        self.assertIsNotNone(cookie.expires)
        self.assertGreater(cookie.expires.timestamp() - time.time(), 360 * 24 * 3600)

    def test_secret_key_is_the_persisted_one(self):
        key_path = os.path.join(tempfile.mkdtemp(prefix="awg-key-"), ".flask_secret_key")
        app, _ = build_app(secret_key_path=key_path)
        self.assertEqual(app.secret_key, Path(key_path).read_bytes())


class SocketAuthTests(unittest.TestCase):
    """handle_connect(): /socket.io/ has no Basic Auth in nginx, so the cookie is the gate."""

    def setUp(self):
        self.key_path = os.path.join(tempfile.mkdtemp(prefix="awg-key-"), ".flask_secret_key")
        self.app, self.socketio, self.client = self._build(self.key_path)

    @staticmethod
    def _build(key_path):
        from core.runtime import create_socketio, register_socket_handlers

        app, manager = build_app(secret_key_path=key_path)
        socketio = create_socketio(app, [])
        register_socket_handlers(socketio, manager, "80")
        return app, socketio, app.test_client()

    def _session_cookie_header(self):
        self.client.get("/api/servers")
        return {"Cookie": f"session={self.client.get_cookie('session').value}"}

    def test_server_runs_in_threading_mode(self):
        # No eventlet/greenlet: handlers and background tasks are plain threads.
        self.assertEqual(self.socketio.server.async_mode, "threading")

    def test_connect_without_cookie_is_rejected(self):
        ws = self.socketio.test_client(self.app)
        self.assertFalse(ws.is_connected())

    def test_forged_cookie_is_rejected(self):
        # Right shape, not signed with this app's key.
        forged = {"Cookie": "session=eyJuZ2lueF9hdXRoZW50aWNhdGVkIjp0cnVlfQ.Z0000A.c2lnbmF0dXJl"}
        ws = self.socketio.test_client(self.app, headers=forged)
        self.assertFalse(ws.is_connected())

    def test_connect_after_an_authenticated_request_is_accepted(self):
        self.client.get("/api/servers")
        ws = self.socketio.test_client(self.app, flask_test_client=self.client)
        self.assertTrue(ws.is_connected())
        status = [event for event in ws.get_received() if event["name"] == "status"]
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["args"][0]["public_ip"], PUBLIC_IP)

    def test_status_goes_only_to_the_tab_that_connected(self):
        self.client.get("/api/servers")
        first = self.socketio.test_client(self.app, flask_test_client=self.client)
        first.get_received()
        self.socketio.test_client(self.app, flask_test_client=self.client)
        self.assertEqual(first.get_received(), [])

    def test_cookie_survives_a_restart_with_the_persisted_key(self):
        headers = self._session_cookie_header()
        app, socketio, _ = self._build(self.key_path)
        self.assertTrue(socketio.test_client(app, headers=headers).is_connected())

    def test_cookie_from_another_key_is_rejected(self):
        headers = self._session_cookie_header()
        other_key = os.path.join(tempfile.mkdtemp(prefix="awg-key-"), ".flask_secret_key")
        app, socketio, _ = self._build(other_key)
        self.assertFalse(socketio.test_client(app, headers=headers).is_connected())


def _nginx_locations():
    """Map each `location <path>` to its directives, comments stripped."""
    text = "\n".join(line.split("#", 1)[0] for line in NGINX_CONF.read_text(encoding="utf-8").splitlines())
    return {m.group(1): m.group(2) for m in re.finditer(r"location\s+(\S+)\s*\{([^{}]*)\}", text)}


class NginxAuthConfigTests(unittest.TestCase):
    """The first gate: Flask trusts that anything reaching it passed Basic Auth."""

    def setUp(self):
        self.locations = _nginx_locations()

    def test_expected_locations_exist(self):
        self.assertEqual(set(self.locations), {"/", "/api/", "/socket.io/", "/static/", "/status"})

    def test_ui_and_api_require_basic_auth(self):
        for path in ("/", "/api/"):
            self.assertRegex(self.locations[path], r"auth_basic\s+\"", path)
            self.assertIn("auth_basic_user_file /etc/nginx/.htpasswd;", self.locations[path], path)

    def test_socket_io_is_the_only_ungated_proxy_to_flask(self):
        # /socket.io/ relies on the session cookie instead; /status is localhost-only.
        for path, body in self.locations.items():
            if "proxy_pass" not in body or path in ("/socket.io/", "/status"):
                continue
            self.assertRegex(body, r"auth_basic\s+\"", path)
        self.assertNotIn("auth_basic", self.locations["/socket.io/"])

    def test_status_is_localhost_only(self):
        body = self.locations["/status"]
        self.assertIn("allow 127.0.0.1;", body)
        self.assertIn("deny all;", body)

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
