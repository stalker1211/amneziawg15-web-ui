"""Tests for the HTTP layer: the anti-CSRF guard, token auth, and serialization.

Uses Flask's test client, so no container is needed. `build_app()` (tests/support.py)
installs the production guards from core/guards.py and the real routes around a
stubbed manager.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

from tests.support import WEB_UI_DIR, build_app

STATIC_JS = os.path.join(WEB_UI_DIR, "static", "js")


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
        self.client.post("/api/servers", data="x=1", content_type="application/x-www-form-urlencoded")
        self.assertEqual(self.manager.config["servers"], [])

    def test_json_mutations_are_allowed(self):
        response = self.client.post(
            "/api/servers",
            json={
                "name": "ok",
                "protocol": "AWG 2.0",
                "subnet": "10.31.0.0/24",
                "port": 51931,
                "auto_start": False,
            },
        )
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
        response = self.client.post(
            "/api/servers",
            json={
                "name": "bad",
                "subnet": "10.0.0.0/24; id",
                "auto_start": False,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("subnet", response.get_json()["error"].lower())

    def test_invalid_port_returns_400(self):
        response = self.client.post(
            "/api/servers",
            json={
                "name": "bad",
                "port": 99999,
                "subnet": "10.32.0.0/24",
                "auto_start": False,
            },
        )
        self.assertEqual(response.status_code, 400)


class TokenAuthTests(unittest.TestCase):
    def setUp(self):
        self.app, _ = build_app(api_token="s3cr3t")
        self.client = self.app.test_client()

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
        self.assertEqual(self.client.get("/api/servers", headers={"X-API-Token": "s3cr3t"}).status_code, 200)

    def test_bearer_form_accepted(self):
        self.assertEqual(self.client.get("/api/servers", headers={"Authorization": "Bearer s3cr3t"}).status_code, 200)


class SerializationTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()
        self.client.post(
            "/api/servers",
            json={
                "name": "ser",
                "protocol": "AWG 3.0",
                "subnet": "10.33.0.0/24",
                "port": 51933,
                "auto_start": False,
            },
        )
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
        self.assertEqual(self.client.get(f"/api/servers/{self.server_id}/clients/nope/config").status_code, 404)

    def test_transport_params_rejects_bad_values_with_400(self):
        response = self.client.post(
            f"/api/servers/{self.server_id}/transport-params",
            json={
                "protocol": "AWG 3.0",
                "S1": 50,
                "S2": 60,
                "S3": 40,
                "S4": 5,
                "H1": "1",
                "H2": "2",
                "H3": "3",
                "H4": "4",
                "HeaderProtectionKey": "aGVhZGVyUFJPVEVDVElPTmtleTAwMDAwMDAwMDAwMDA=",
            },
        )
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
        self.assertEqual([p["id"] for p in protocols["supported"]], list(self.manager.SUPPORTED_PROTOCOLS))

    def test_every_supported_protocol_normalizes_to_itself(self):
        for protocol in self.manager.SUPPORTED_PROTOCOLS:
            self.assertEqual(self.manager.normalize_protocol(protocol), protocol)

    def test_frontend_protocols_js_matches_the_backend(self):
        """protocols.js is the UI's copy of the table; it must not drift."""
        source = Path(os.path.join(STATIC_JS, "protocols.js")).read_text(encoding="utf-8")

        ids = re.findall(r"id:\s*'([^']+)'", source)
        self.assertEqual(
            ids, list(self.manager.SUPPORTED_PROTOCOLS), "protocols.js protocol list differs from SUPPORTED_PROTOCOLS"
        )

        default = re.search(r"DEFAULT_PROTOCOL\s*=\s*'([^']+)'", source).group(1)
        self.assertEqual(default, self.manager.DEFAULT_PROTOCOL)

        # Each entry's capability flags must agree with the Python predicates.
        entries = re.findall(
            r"id:\s*'([^']+)',\s*"
            r"supportsS34:\s*(true|false),[^}]*?"
            r"supportsHeaderRanges:\s*(true|false),[^}]*?"
            r"supportsAwg3:\s*(true|false),[^}]*?"
            r"supportsAwg31:\s*(true|false)",
            source,
            re.DOTALL,
        )
        self.assertEqual(
            len(entries), len(self.manager.SUPPORTED_PROTOCOLS), "could not parse every protocol entry from protocols.js"
        )
        for protocol, s34, ranges, awg3, awg31 in entries:
            self.assertEqual(s34 == "true", self.manager.protocol_supports_s34(protocol), f"{protocol}: supportsS34 mismatch")
            self.assertEqual(
                ranges == "true",
                self.manager.protocol_supports_header_ranges(protocol),
                f"{protocol}: supportsHeaderRanges mismatch",
            )
            self.assertEqual(
                awg3 == "true", self.manager.protocol_supports_awg3(protocol), f"{protocol}: supportsAwg3 mismatch"
            )
            self.assertEqual(
                awg31 == "true", self.manager.protocol_supports_awg31(protocol), f"{protocol}: supportsAwg31 mismatch"
            )

    def test_no_hardcoded_protocol_literals_left_in_the_ui(self):
        """Capability checks must go through Protocols, not string comparison."""
        for name in ("app.js", "modals.js"):
            source = Path(os.path.join(STATIC_JS, name)).read_text(encoding="utf-8")
            # Display strings such as "AWG 3.0 parameters" are fine; quoted
            # identifiers used for comparison are not.
            offenders = re.findall(r"=== '(AWG [0-9.]+)'|'(AWG [0-9.]+)' ===", source)
            self.assertEqual(offenders, [], f"{name} compares against a protocol literal")

    def test_create_server_checks_the_selected_protocol(self):
        source = Path(os.path.join(STATIC_JS, "app.js")).read_text(encoding="utf-8")
        self.assertIn("window.Protocols.supportsAwg3(formData.protocol)", source)
        self.assertNotIn("formData.window.Protocols", source)

    def test_header_range_ui_uses_the_header_range_capability(self):
        for name in ("app.js", "modals.js"):
            source = Path(os.path.join(STATIC_JS, name)).read_text(encoding="utf-8")
            offenders = re.findall(
                r"(?:allowRanges|supportsRanges)\s*=\s*window\.Protocols\.supportsS34",
                source,
            )
            self.assertEqual(offenders, [], f"{name} gates header ranges on S3/S4 support")

        app_source = Path(os.path.join(STATIC_JS, "app.js")).read_text(encoding="utf-8")
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

    def test_iptables_test_requires_server_id(self):
        self.assertEqual(self.client.get("/api/system/iptables-test").status_code, 400)
        self.assertEqual(self.client.get("/api/system/iptables-test?server_id=nope").status_code, 404)


# Two daemons sharing one log, as scripts/amneziawg-go-logged.sh writes it: a wrapper
# line, the "not required" banner each daemon prints at start, then (iface) lines.
AWG_LOG = """\
2026-09-26T10:00:00+00:00 [amneziawg-go-logged] starting: wg-aaa111
┌──────────────────────────────────────────────────────┐
│   Running amneziawg-go is not required because this   │
│   Linux kernel has first class support for AmneziaWG.  │
| https://github.com/amnezia-vpn/amneziawg-linux-kernel-module
└──────────────────────────────────────────────────────┘
INFO: (wg-aaa111) 2026/09/26 10:00:01 Starting amneziawg-go version 0.2
2026-09-26T10:00:02+00:00 [amneziawg-go-logged] starting: wg-bbb222
┌──────────────────────────────────────────────────────┐
│   Running amneziawg-go is not required because this   │
└──────────────────────────────────────────────────────┘
INFO: (wg-bbb222) 2026/09/26 10:00:03 Starting amneziawg-go version 0.2
DEBUG: (wg-aaa111) 2026/09/26 10:00:04 Received handshake initiation
*** (wg-bbb222) *** Interface closed
a general line that names no interface
"""


class AwgLogTests(unittest.TestCase):
    def setUp(self):
        self.log_path = os.path.join(tempfile.mkdtemp(prefix="awg-log-"), "amneziawg-go.log")
        Path(self.log_path).write_text(AWG_LOG, encoding="utf-8")
        self.app, _ = build_app(awg_log_file=self.log_path)
        self.client = self.app.test_client()

    def _lines(self, query=""):
        response = self.client.get(f"/api/system/awg-log{query}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["lines"]

    def test_no_filter_returns_everything(self):
        self.assertEqual(self._lines(), AWG_LOG.splitlines())

    def test_interface_filter_keeps_own_lines_own_banner_and_general_lines(self):
        lines = AWG_LOG.splitlines()
        own_banner = lines[0:6]  # wrapper line + the banner printed while wg-aaa111 started
        self.assertEqual(
            self._lines("?interface=wg-aaa111"),
            [*own_banner, lines[6], lines[12], lines[14]],
        )

    def test_interface_filter_drops_other_daemons_banner_and_lines(self):
        lines = self._lines("?interface=wg-bbb222")
        self.assertFalse([ln for ln in lines if "wg-aaa111" in ln])
        self.assertIn("*** (wg-bbb222) *** Interface closed", lines)
        self.assertEqual(sum("not required" in ln for ln in lines), 1)

    def test_line_count_is_clamped(self):
        Path(self.log_path).write_text("".join(f"line {n}\n" for n in range(6000)), encoding="utf-8")
        for requested, expected in (("1", 50), ("60", 60), ("abc", 400), ("999999", 5000)):
            lines = self._lines(f"?lines={requested}")
            self.assertEqual(len(lines), expected, requested)
            self.assertEqual(lines[-1], "line 5999")


if __name__ == "__main__":
    unittest.main()
