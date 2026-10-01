"""Tests for scripts/api_status.py, the status viewer that check_awg.sh runs.

It is a standalone uv script, not part of the app, so nothing else notices when the
API changes under it. These run it against a recorded /api/servers payload
(tests/fixtures/api_servers_2.5.json, recorded from tests/demo_server.py) with
urlopen replaced.
"""

import ast
import base64
import contextlib
import copy
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "api_status.py"
PAYLOAD = json.loads((REPO / "tests" / "fixtures" / "api_servers_2.5.json").read_text(encoding="utf-8"))
PROBE = {
    "external_ip": "198.51.100.99",
    "external_ip_geo": "Sweden / Stockholm",
    "external_ip_geo_country_code": "SE",
    "service": "https://ident.me",
    "error": None,
}


def _load_script():
    spec = importlib.util.spec_from_file_location("api_status", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApiStatusTests(unittest.TestCase):
    def setUp(self):
        self.script = _load_script()
        self.requests = []

    def run_script(self, *args, payload=PAYLOAD, error=None):
        """main() with these arguments against the payload: (exit code, stdout, stderr)."""

        def urlopen(request, timeout):
            self.requests.append(request)
            if error:
                raise error
            body = PROBE if request.get_method() == "POST" else payload
            return io.BytesIO(json.dumps(body).encode())

        out, err = io.StringIO(), io.StringIO()
        argv = ["api_status.py", "--base-url", "http://panel.test:8080/", "--user", "admin", "--password", "pw", *args]
        with (
            mock.patch.object(urllib.request, "urlopen", side_effect=urlopen),
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.script.main()
        return code, out.getvalue(), err.getvalue()

    def test_servers_and_clients_from_a_recorded_payload(self):
        code, out, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("Home NL (93f3e4) : running\n\tServer IP = 203.0.113.24  port = ", out)
        self.assertIn("\tEgress IP = 198.51.100.17", out)
        self.assertRegex(
            out,
            r"\tiPhone \(913064\) : active\n"
            r"\t  ip = 10\.10\.0\.2  endpoint = 198\.51\.100\.40:53412 \(NL / [^)]+\)\n"
            r"\t  last handshake: 33 s ago  rx = 1\.39 GiB  tx = ",
        )

    def test_no_telemetry_is_a_dash_and_a_real_zero_stays_zero(self):
        _, out, _ = self.run_script()
        # The stopped server's client has no telemetry at all.
        self.assertIn(
            "\ttest-peer (d5c62e) : inactive\n\t  ip = 10.30.0.2  endpoint = -\n\t  last handshake: -  rx = -  tx = -", out
        )
        # This one has telemetry: it never connected.
        self.assertIn(
            "\tRouter (2e112b) : inactive\n\t  ip = 10.10.0.5  endpoint = -\n\t  last handshake: never  rx = 0 B  tx = 0 B", out
        )

    def test_a_panel_older_than_2_4_says_so(self):
        # The 2.4 script printed 0 B for every client against a 2.3 panel.
        payload = copy.deepcopy(PAYLOAD)
        for server in payload:
            del server["traffic"]
        _, out, _ = self.run_script(payload=payload)
        self.assertEqual(out.count("<no traffic: this panel is older than 2.4>"), 3)
        self.assertNotIn("0 B", out)

    def test_basic_auth_goes_with_the_request(self):
        self.run_script()
        request = self.requests[0]
        self.assertEqual(request.full_url, "http://panel.test:8080/api/servers")
        self.assertEqual(request.get_header("Authorization"), "Basic " + base64.b64encode(b"admin:pw").decode())

    def test_refresh_egress_posts_json(self):
        # Without a JSON content type the panel answers a POST with 415 (anti-CSRF).
        _, out, _ = self.run_script("--refresh-egress")
        posts = [r for r in self.requests if r.get_method() == "POST"]
        self.assertEqual([r.full_url.rsplit("/", 2)[-2] for r in posts], [s["id"] for s in PAYLOAD])
        for request in posts:
            self.assertEqual((request.get_header("Content-type"), request.data), ("application/json", b"{}"))
        self.assertEqual(out.count("Egress IP = 198.51.100.99 (SE / Sweden / Stockholm)"), 3)

    def test_errors_are_reported(self):
        cases = (
            (urllib.error.HTTPError("http://panel.test:8080/api/servers", 401, "Unauthorized", {}, None), "HTTP 401"),
            (urllib.error.URLError("refused"), "invalid response: <urlopen error refused>"),
        )
        for error, message in cases:
            code, out, err = self.run_script(error=error)
            self.assertEqual((code, out, err), (2, "", f"Failed to fetch servers: {message}\n"))

    def test_it_needs_no_dependency(self):
        # The uv shebang stays, with nothing to install.
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("# dependencies = []\n", source)
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - sys.stdlib_module_names, set())


if __name__ == "__main__":
    unittest.main()
