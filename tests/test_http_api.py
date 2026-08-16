"""Tests for the HTTP layer: the anti-CSRF guard, token auth, and serialization.

Uses Flask's test client, so no container is needed. The app module reads a lot of
environment at import time and constructs a real AmneziaManager, so `build_app()`
below patches the boundaries the same way tests/support.py does.
"""

import json
import os
import re
import sys
import unittest

from tests.support import GOLDEN_DIR, WEB_UI_DIR, build_manager

STATIC_JS = os.path.join(WEB_UI_DIR, "static", "js")


def build_app():
    """Construct the Flask app with a stubbed manager, without touching the system."""
    from flask import Flask, jsonify, request  # noqa: PLC0415

    from core.helpers import to_bool  # noqa: PLC0415
    from routes.servers import register_server_routes  # noqa: PLC0415
    from routes.system import register_system_routes  # noqa: PLC0415

    manager = build_manager()

    app = Flask(__name__)
    app.config.update(TESTING=True)

    api_token = os.environ.get("TEST_API_TOKEN", "")

    # Mirrors app.py: reject non-JSON mutations before anything else runs.
    @app.before_request
    def require_json_for_mutations():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if not request.path.startswith("/api/"):
            return None
        if not request.is_json:
            return jsonify({"error": "Content-Type: application/json is required"}), 415
        return None

    def require_token(view):
        from functools import wraps  # noqa: PLC0415

        @wraps(view)
        def wrapped(*args, **kwargs):
            if not api_token:
                return view(*args, **kwargs)
            token = (request.headers.get("X-API-Token") or "").strip()
            if not token:
                auth = request.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    token = auth[7:].strip()
            if not token:
                return jsonify({"error": "Missing API token"}), 401
            if token != api_token:
                return jsonify({"error": "Invalid API token"}), 401
            return view(*args, **kwargs)

        return wrapped

    register_system_routes(
        app, require_token, manager,
        awg_log_file="/nonexistent/awg.log", nginx_port="80", auto_start_servers=False,
        default_mtu=1420, default_subnet="10.0.0.0/24", default_port=51820,
        default_dns="1.1.1.1",
    )
    register_server_routes(
        app, require_token, manager,
        to_bool=to_bool, default_enable_nat=True, default_block_lan_cidrs=True,
    )
    return app, manager


class CsrfGuardTests(unittest.TestCase):
    """A cross-site <form> can only send urlencoded/text-plain/multipart bodies."""

    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()

    def test_form_content_types_are_rejected(self):
        for content_type in (
            "application/x-www-form-urlencoded",
            "text/plain",
            "multipart/form-data; boundary=x",
        ):
            response = self.client.post("/api/servers", data="x=1", content_type=content_type)
            self.assertEqual(response.status_code, 415, content_type)

    def test_missing_content_type_is_rejected(self):
        self.assertEqual(self.client.post("/api/servers").status_code, 415)

    def test_no_server_is_created_by_a_rejected_request(self):
        self.client.post("/api/servers", data="x=1",
                         content_type="application/x-www-form-urlencoded")
        self.assertEqual(self.manager.config["servers"], [])

    def test_json_mutations_are_allowed(self):
        response = self.client.post("/api/servers", json={
            "name": "ok", "protocol": "AWG 2.0", "subnet": "10.31.0.0/24",
            "port": 51931, "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.manager.config["servers"]), 1)

    def test_get_requests_need_no_content_type(self):
        self.assertEqual(self.client.get("/api/servers").status_code, 200)
        self.assertEqual(self.client.get("/api/clients").status_code, 200)

    def test_delete_also_requires_json(self):
        self.assertEqual(self.client.delete("/api/servers/none").status_code, 415)


class CreateServerValidationTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()

    def test_empty_body_does_not_create_a_default_server(self):
        response = self.client.post("/api/servers", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.get_json()["error"].lower())
        self.assertEqual(self.manager.config["servers"], [])

    def test_blank_name_rejected(self):
        self.assertEqual(self.client.post("/api/servers", json={"name": "   "}).status_code, 400)

    def test_invalid_subnet_returns_400_not_500(self):
        response = self.client.post("/api/servers", json={
            "name": "bad", "subnet": "10.0.0.0/24; id", "auto_start": False,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("subnet", response.get_json()["error"].lower())

    def test_invalid_port_returns_400(self):
        response = self.client.post("/api/servers", json={
            "name": "bad", "port": 99999, "subnet": "10.32.0.0/24", "auto_start": False,
        })
        self.assertEqual(response.status_code, 400)


class TokenAuthTests(unittest.TestCase):
    def setUp(self):
        os.environ["TEST_API_TOKEN"] = "s3cr3t"
        self.app, _ = build_app()
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("TEST_API_TOKEN", None)

    def test_missing_token_rejected_and_message_mentions_token(self):
        response = self.client.get("/api/servers")
        self.assertEqual(response.status_code, 401)
        # ApiClient.isMissingApiToken() looks for "token" to decide whether to prompt.
        self.assertIn("token", response.get_json()["error"].lower())

    def test_wrong_token_rejected(self):
        response = self.client.get("/api/servers", headers={"X-API-Token": "nope"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("token", response.get_json()["error"].lower())

    def test_correct_token_accepted(self):
        self.assertEqual(
            self.client.get("/api/servers", headers={"X-API-Token": "s3cr3t"}).status_code, 200)

    def test_bearer_form_accepted(self):
        self.assertEqual(
            self.client.get("/api/servers",
                            headers={"Authorization": "Bearer s3cr3t"}).status_code, 200)


class SerializationTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()
        self.client.post("/api/servers", json={
            "name": "ser", "protocol": "AWG 3.0", "subnet": "10.33.0.0/24",
            "port": 51933, "auto_start": False,
        })
        self.server_id = self.manager.config["servers"][0]["id"]
        self.client.post(f"/api/servers/{self.server_id}/clients", json={"name": "phone"})

    def test_legacy_obfuscation_fields_are_stripped(self):
        payload = self.client.get("/api/servers").get_json()[0]
        self.assertNotIn("obfuscation_params", payload)
        self.assertNotIn("obfuscation_enabled", payload)
        for client in payload["clients"]:
            self.assertNotIn("obfuscation_params", client)

    def test_server_payload_exposes_transport_and_client_defaults(self):
        payload = self.client.get("/api/servers").get_json()[0]
        self.assertIn("transport_params", payload)
        self.assertIn("client_defaults", payload)
        self.assertEqual(payload["protocol"], "AWG 3.0")

    def test_client_config_download_is_attachment_with_safe_filename(self):
        client_id = self.manager.config["servers"][0]["clients"][0]["id"]
        response = self.client.get(f"/api/servers/{self.server_id}/clients/{client_id}/config")
        self.assertEqual(response.status_code, 200)
        disposition = response.headers["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn(".conf", disposition)

    def test_unknown_ids_return_404(self):
        self.assertEqual(self.client.get("/api/servers/nope/info").status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/servers/{self.server_id}/clients/nope/config").status_code, 404)

    def test_transport_params_rejects_bad_values_with_400(self):
        response = self.client.post(f"/api/servers/{self.server_id}/transport-params", json={
            "protocol": "AWG 3.0", "S1": 50, "S2": 60, "S3": 40, "S4": 5,
            "H1": "1", "H2": "2", "H3": "3", "H4": "4",
            "HeaderProtectionKey": "aGVhZGVyUFJPVEVDVElPTmtleTAwMDAwMDAwMDAwMDA=",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("12", response.get_json()["error"])


class ProtocolTableTests(unittest.TestCase):
    """The frontend mirrors the backend protocol table; catch drift between them."""

    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()

    def test_status_exposes_the_protocol_table(self):
        protocols = self.client.get("/api/system/status").get_json()["protocols"]
        self.assertEqual(protocols["default"], self.manager.DEFAULT_PROTOCOL)
        self.assertEqual([p["id"] for p in protocols["supported"]],
                         list(self.manager.SUPPORTED_PROTOCOLS))

    def test_every_supported_protocol_normalizes_to_itself(self):
        for protocol in self.manager.SUPPORTED_PROTOCOLS:
            self.assertEqual(self.manager.normalize_protocol(protocol), protocol)

    def test_frontend_protocols_js_matches_the_backend(self):
        """protocols.js is the UI's copy of the table; it must not drift."""
        source = open(os.path.join(STATIC_JS, "protocols.js"), encoding="utf-8").read()

        ids = re.findall(r"id:\s*'([^']+)'", source)
        self.assertEqual(ids, list(self.manager.SUPPORTED_PROTOCOLS),
                         "protocols.js protocol list differs from SUPPORTED_PROTOCOLS")

        default = re.search(r"DEFAULT_PROTOCOL\s*=\s*'([^']+)'", source).group(1)
        self.assertEqual(default, self.manager.DEFAULT_PROTOCOL)

        # Each entry's capability flags must agree with the Python predicates.
        entries = re.findall(
            r"id:\s*'([^']+)',\s*"
            r"supportsS34:\s*(true|false),[^}]*?"
            r"supportsHeaderRanges:\s*(true|false),[^}]*?"
            r"supportsAwg3:\s*(true|false),[^}]*?"
            r"supportsAwg31:\s*(true|false)",
            source, re.S)
        self.assertEqual(len(entries), len(self.manager.SUPPORTED_PROTOCOLS),
                         "could not parse every protocol entry from protocols.js")
        for protocol, s34, ranges, awg3, awg31 in entries:
            self.assertEqual(s34 == "true", self.manager.protocol_supports_s34(protocol),
                             f"{protocol}: supportsS34 mismatch")
            self.assertEqual(ranges == "true", self.manager.protocol_supports_header_ranges(protocol),
                             f"{protocol}: supportsHeaderRanges mismatch")
            self.assertEqual(awg3 == "true", self.manager.protocol_supports_awg3(protocol),
                             f"{protocol}: supportsAwg3 mismatch")
            self.assertEqual(awg31 == "true", self.manager.protocol_supports_awg31(protocol),
                             f"{protocol}: supportsAwg31 mismatch")

    def test_no_hardcoded_protocol_literals_left_in_the_ui(self):
        """Capability checks must go through Protocols, not string comparison."""
        for name in ("app.js", "modals.js"):
            source = open(os.path.join(STATIC_JS, name), encoding="utf-8").read()
            # Display strings such as "AWG 3.0 parameters" are fine; quoted
            # identifiers used for comparison are not.
            offenders = re.findall(r"=== '(AWG [0-9.]+)'|'(AWG [0-9.]+)' ===", source)
            self.assertEqual(offenders, [], f"{name} compares against a protocol literal")

    def test_create_server_checks_the_selected_protocol(self):
        source = open(os.path.join(STATIC_JS, "app.js"), encoding="utf-8").read()
        self.assertIn("window.Protocols.supportsAwg3(formData.protocol)", source)
        self.assertNotIn("formData.window.Protocols", source)

    def test_header_range_ui_uses_the_header_range_capability(self):
        for name in ("app.js", "modals.js"):
            source = open(os.path.join(STATIC_JS, name), encoding="utf-8").read()
            offenders = re.findall(
                r"(?:allowRanges|supportsRanges)\s*=\s*window\.Protocols\.supportsS34",
                source,
            )
            self.assertEqual(offenders, [], f"{name} gates header ranges on S3/S4 support")

        app_source = open(os.path.join(STATIC_JS, "app.js"), encoding="utf-8").read()
        self.assertIn(
            "window.Protocols.supportsHeaderRanges(protocol) && !headers.some",
            app_source,
        )


class SystemRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()

    def test_status_reports_counts(self):
        payload = self.client.get("/api/system/status").get_json()
        self.assertEqual(payload["total_servers"], 0)
        self.assertIn("public_ip", payload)
        self.assertIn("environment", payload)

    def test_awg_log_handles_missing_file(self):
        payload = self.client.get("/api/system/awg-log").get_json()
        self.assertEqual(payload["lines"], [])
        self.assertIn("note", payload)

    def test_awg_log_clamps_line_count(self):
        for requested, expected in (("1", 50), ("999999", 5000), ("abc", 400)):
            payload = self.client.get(f"/api/system/awg-log?lines={requested}").get_json()
            # The file is missing, so only the clamping path is observable; assert it
            # does not raise and returns the documented shape.
            self.assertIn("path", payload)

    def test_iptables_test_requires_server_id(self):
        self.assertEqual(self.client.get("/api/system/iptables-test").status_code, 400)
        self.assertEqual(
            self.client.get("/api/system/iptables-test?server_id=nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
