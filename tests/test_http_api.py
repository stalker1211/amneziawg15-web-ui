"""Tests for the HTTP layer: the anti-CSRF guard, token auth, and serialization.

Uses Flask's test client, so no container is needed. `build_app()` (tests/support.py)
installs the production guards from core/guards.py and the real routes around a
stubbed manager.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import WEB_UI_DIR, SystemPaths, build_app, build_manager, build_real_manager

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


def _create_server(client, name="s", subnet="10.61.0.0/24", port=51961, **extra):
    data = {"name": name, "protocol": "AWG 2.0", "subnet": subnet, "port": port, "auto_start": False, **extra}
    response = client.post("/api/servers", json=data)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


class _RealSystemApp(unittest.TestCase):
    """The HTTP app around a manager running its real interface/iptables code."""

    def setUp(self):
        self.paths = SystemPaths().start(self)
        manager, self.fake = build_real_manager(self)
        self.app, self.manager = build_app(manager=manager)
        self.client = self.app.test_client()
        self.server = _create_server(self.client)
        self.fake.calls.clear()


ROUTE_BODIES = {"rename": {"name": "x"}, "client-params": {"client_params": {}}}


class RouteNotFoundTests(_RealSystemApp):
    """Every route taking an id answers an unknown one with a JSON 404, never a 500."""

    def _routes(self, needs):
        for rule in self.app.url_map.iter_rules():
            if needs in rule.arguments:
                for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                    yield rule, method

    def _call(self, rule, method, server_id, client_id="nope"):
        url = rule.rule.replace("<server_id>", server_id).replace("<client_id>", client_id)
        body = ROUTE_BODIES.get(rule.rule.rsplit("/", 1)[-1], {})
        return self.client.open(url, method=method, json=None if method == "GET" else body)

    def test_unknown_server(self):
        checked = 0
        for rule, method in self._routes("server_id"):
            label = f"{method} {rule.rule}"
            response = self._call(rule, method, "nope")
            if (method, rule.rule) == ("GET", "/api/servers/<server_id>/clients"):
                # Lists by filter: an unknown server simply has no clients.
                self.assertEqual((response.status_code, response.get_json()), (200, []), label)
                continue
            self.assertEqual(response.status_code, 404, label)
            self.assertIn("error", response.get_json(), label)
            checked += 1
        self.assertGreaterEqual(checked, 18)

    def test_unknown_client_on_a_real_server(self):
        checked = 0
        for rule, method in self._routes("client_id"):
            label = f"{method} {rule.rule}"
            response = self._call(rule, method, self.server["id"])
            self.assertEqual(response.status_code, 404, label)
            self.assertIn("error", response.get_json(), label)
            checked += 1
        self.assertGreaterEqual(checked, 6)
        self.assertEqual(self.fake.calls, [])


# Keys the UI consumes. Private and preshared keys are never sent as JSON fields; the
# configs that need them are rendered server-side.
SECRET_KEYS = {"server_private_key", "client_private_key", "preshared_key"}
SERVER_KEYS = {
    "auto_start", "block_lan_cidrs", "client_defaults", "clients", "config_path", "created_at", "dns",
    "egress_probe", "enable_nat", "id", "interface", "mtu", "name", "port", "protocol", "public_ip",
    "public_ip_geo", "public_ip_geo_country_code", "server_ip", "server_public_key", "status", "subnet",
    "transport_params",
}  # fmt: skip
CLIENT_KEYS = {
    "client_ip", "client_params", "client_public_key", "created_at", "id", "name", "protocol", "server_id",
    "server_name", "status", "suspended",
}  # fmt: skip


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()
        self.server = _create_server(self.client, protocol="AWG 3.1")
        self.added = self.client.post(f"/api/servers/{self.server['id']}/clients", json={"name": "phone"}).get_json()

    def test_server_and_client_payloads(self):
        server = self.client.get("/api/servers").get_json()[0]
        self.assertEqual(set(server), SERVER_KEYS)
        payloads = {
            "GET /api/servers": server["clients"][0],
            "POST .../clients": self.added["client"],
            "GET .../clients": self.client.get(f"/api/servers/{self.server['id']}/clients").get_json()[0],
            "GET /api/clients": self.client.get("/api/clients").get_json()[0],
        }
        for label, client in payloads.items():
            self.assertEqual(set(client), CLIENT_KEYS, label)
            self.assertEqual(client["server_name"], self.server["name"], label)
        self.assertIn("[Interface]", self.added["config"])

    def test_no_json_payload_carries_a_private_key(self):
        manager_server = self.manager.get_server(self.server["id"])
        secrets = {manager_server["server_private_key"]}
        for client in manager_server["clients"]:
            secrets |= {client["client_private_key"], client["preshared_key"]}
        client_url = f"/api/servers/{self.server['id']}/clients/{self.added['client']['id']}"

        responses = {
            "POST /api/servers": self.client.post(
                "/api/servers",
                json={"name": "t", "protocol": "AWG 2.0", "subnet": "10.66.0.0/24", "port": 51966, "auto_start": False},
            ),
            "GET /api/servers": self.client.get("/api/servers"),
            "GET /api/clients": self.client.get("/api/clients"),
            "GET .../clients": self.client.get(f"/api/servers/{self.server['id']}/clients"),
            "GET .../info": self.client.get(f"/api/servers/{self.server['id']}/info"),
            "POST .../suspend": self.client.post(f"{client_url}/suspend", json={}),
            "POST .../client-params": self.client.post(f"{client_url}/client-params", json={"client_params": {"Jc": 6}}),
        }
        for label, response in responses.items():
            self.assertEqual(response.status_code, 200, label)
            body = response.get_data(as_text=True)
            for field in SECRET_KEYS:
                self.assertNotIn(f'"{field}"', body, label)
        # The key values themselves must not leak under some other name either.
        for label in ("GET /api/servers", "GET /api/clients", "GET .../clients"):
            body = responses[label].get_data(as_text=True)
            for secret in secrets:
                self.assertNotIn(secret, body, label)

    def test_configs_that_need_keys_still_have_them(self):
        client = self.manager.get_client(self.added["client"]["id"])
        both = self.client.get(f"/api/servers/{self.server['id']}/clients/{client['id']}/config-both").get_json()
        self.assertIn(client["client_private_key"], both["clean_config"])
        self.assertIn(client["preshared_key"], both["clean_config"])
        server_conf = self.client.get(f"/api/servers/{self.server['id']}/config").get_json()["config_content"]
        self.assertIn(self.manager.get_server(self.server["id"])["server_private_key"], server_conf)

    def test_info_payload(self):
        info = self.client.get(f"/api/servers/{self.server['id']}/info").get_json()
        self.assertEqual(
            set(info),
            {
                "block_lan_cidrs", "client_defaults", "clients_count", "config_path", "created_at", "dns",
                "enable_nat", "id", "interface", "mtu", "name", "port", "protocol", "public_ip", "public_key",
                "server_ip", "status", "subnet", "transport_params",
            },
        )  # fmt: skip
        self.assertEqual((info["clients_count"], info["status"], info["protocol"]), (1, "stopped", "AWG 3.1"))


class ServerListTests(unittest.TestCase):
    """GET /api/servers: what the panel polls. Pinned before it stops writing to disk."""

    def test_defaults_for_fields_an_older_config_lacks(self):
        first = build_manager()
        server = first.create_wireguard_server(
            {"name": "old", "protocol": "AWG 2.0", "subnet": "10.62.0.0/24", "port": 51962, "auto_start": False}
        )
        stored = json.loads(Path(first.config_file).read_text(encoding="utf-8"))
        for key in ("mtu", "enable_nat", "block_lan_cidrs", "egress_probe", "protocol", "transport_params", "client_defaults"):
            stored["servers"][0].pop(key, None)
        Path(first.config_file).write_text(json.dumps(stored), encoding="utf-8")

        manager = build_manager(config_dir=first.config_dir, wireguard_config_dir=first.wireguard_config_dir,
                                config_file=first.config_file)  # fmt: skip
        app, _ = build_app(manager=manager)
        listed = app.test_client().get("/api/servers").get_json()[0]

        self.assertEqual(listed["id"], server["id"])
        self.assertEqual(
            {key: listed[key] for key in ("mtu", "enable_nat", "block_lan_cidrs", "egress_probe", "protocol", "status")},
            {"mtu": 1420, "enable_nat": True, "block_lan_cidrs": True, "egress_probe": None, "protocol": "AWG 1.5",
             "status": "stopped"},
        )  # fmt: skip
        self.assertIsInstance(listed["transport_params"], dict)
        self.assertIsInstance(listed["client_defaults"], dict)

    def test_listing_writes_nothing(self):
        app, manager = build_app()
        client = app.test_client()
        _create_server(client)
        manager.get_server(manager.config["servers"][0]["id"])["egress_probe"] = {"external_ip": "198.51.100.9"}
        manager.save_config()
        before_file = Path(manager.config_file).read_bytes()
        before_state = json.dumps(manager.config, sort_keys=True)

        with mock.patch.object(manager, "lookup_geoip", return_value=("Somewhere", "NL")):
            self.assertEqual(client.get("/api/servers").status_code, 200)
            self.assertEqual(client.get(f"/api/servers/{manager.config['servers'][0]['id']}/info").status_code, 200)

        self.assertEqual(Path(manager.config_file).read_bytes(), before_file)
        self.assertEqual(json.dumps(manager.config, sort_keys=True), before_state)

    def test_display_values_persisted_by_older_versions_are_dropped_on_load(self):
        first = build_manager()
        first.create_wireguard_server(
            {"name": "old", "protocol": "AWG 2.0", "subnet": "10.64.0.0/24", "port": 51964, "auto_start": False}
        )
        stored = json.loads(Path(first.config_file).read_text(encoding="utf-8"))
        stored["servers"][0].update(
            public_ip_geo="Stale", public_ip_geo_country_code="XX", current_status="running",
            egress_probe={"external_ip": "198.51.100.9", "service": "https://ident.me", "service_name": "ident.me"},
        )  # fmt: skip
        Path(first.config_file).write_text(json.dumps(stored), encoding="utf-8")

        server = build_manager(config_dir=first.config_dir, wireguard_config_dir=first.wireguard_config_dir,
                               config_file=first.config_file).config["servers"][0]  # fmt: skip
        for key in ("public_ip_geo", "public_ip_geo_country_code", "current_status"):
            self.assertNotIn(key, server)
        self.assertEqual(server["egress_probe"], {"external_ip": "198.51.100.9", "service": "https://ident.me"})

    def test_geo_labels_and_probe_service_name(self):
        app, manager = build_app()
        client = app.test_client()
        server = _create_server(client)
        manager.get_server(server["id"])["egress_probe"] = {"external_ip": "198.51.100.9", "service": "https://ident.me"}

        with mock.patch.object(manager, "lookup_geoip", return_value=("Netherlands / Amsterdam", "NL")):
            listed = client.get("/api/servers").get_json()[0]

        self.assertEqual((listed["public_ip_geo"], listed["public_ip_geo_country_code"]), ("Netherlands / Amsterdam", "NL"))
        probe = listed["egress_probe"]
        self.assertEqual((probe["service_name"], probe["external_ip_geo_country_code"]), ("ident.me", "NL"))


class ServerRouteTests(_RealSystemApp):
    def _url(self, *parts):
        return "/".join(("/api/servers", self.server["id"], *parts))

    def test_start_and_stop(self):
        self.assertEqual(self.client.post(self._url("start"), json={}).get_json(), {"status": "started"})
        self.assertEqual(self.fake.argvs()[0], ["/usr/bin/awg-quick", "up", self.server["interface"]])
        self.assertEqual(self.client.post(self._url("stop"), json={}).get_json(), {"status": "stopped"})
        self.assertEqual(self.fake.argvs()[-1], ["/usr/bin/awg-quick", "down", self.server["interface"]])

    def test_failed_start_is_reported(self):
        self.fake.respond(["/usr/bin/awg-quick", "up"], 1)
        with self.assertLogs("services.amnezia_manager", "ERROR"):
            response = self.client.post(self._url("start"), json={})
        self.assertEqual(response.status_code, 404)
        self.assertIn("failed to start", response.get_json()["error"])

    def test_delete_stops_a_running_server_first(self):
        self.paths.interfaces.add(self.server["interface"])
        self.fake.respond(["ip", "link", "show"], "state UNKNOWN")
        self.assertEqual(self.client.delete(self._url(), json={}).status_code, 200)
        self.assertIn(["/usr/bin/awg-quick", "down", self.server["interface"]], self.fake.argvs())
        self.assertIsNone(self.manager.get_server(self.server["id"]))

    def test_networking_toggles_persist_and_reapply_only_when_running(self):
        response = self.client.post(self._url("networking"), json={"enable_nat": False, "block_lan_cidrs": "on"})
        self.assertEqual(
            response.get_json(),
            {"status": "updated", "server_id": self.server["id"], "enable_nat": False, "block_lan_cidrs": True,
             "iptables": "skipped"},
        )  # fmt: skip
        stored = json.loads(Path(self.manager.config_file).read_text(encoding="utf-8"))["servers"][0]
        self.assertEqual((stored["enable_nat"], stored["block_lan_cidrs"]), (False, True))

        self.paths.interfaces.add(self.server["interface"])
        self.fake.respond(["ip", "link", "show"], "state UNKNOWN")
        self.assertEqual(self.client.post(self._url("networking"), json={}).get_json()["iptables"], "reapplied")
        self.fake.respond(["/app/scripts/setup_iptables.sh"], 1)
        with self.assertLogs("services.amnezia_manager", "ERROR"):
            self.assertEqual(self.client.post(self._url("networking"), json={}).get_json()["iptables"], "failed")

    def test_server_config_and_download(self):
        on_disk = Path(self.server["config_path"]).read_text(encoding="utf-8")
        payload = self.client.get(self._url("config")).get_json()
        self.assertEqual(payload["config_content"], on_disk)
        self.assertEqual(payload["public_key"], self.server["server_public_key"])

        download = self.client.get(self._url("config", "download"))
        self.assertEqual(download.get_data(as_text=True), on_disk)
        self.assertIn(f"{self.server['interface']}.conf", download.headers["Content-Disposition"])

        os.remove(self.server["config_path"])
        self.assertEqual(self.client.get(self._url("config")).status_code, 404)
        self.assertEqual(self.client.get(self._url("config", "download")).status_code, 404)

    def test_client_config_both(self):
        added = self.client.post(self._url("clients"), json={"name": "phone"}).get_json()["client"]
        both = self.client.get(self._url("clients", added["id"], "config-both")).get_json()
        self.assertIn("# AmneziaWG Client Configuration", both["full_config"])
        self.assertNotIn("# AmneziaWG Client Configuration", both["clean_config"])
        self.assertIn("[Interface]", both["clean_config"])
        self.assertEqual((both["clean_length"], both["full_length"]), (len(both["clean_config"]), len(both["full_config"])))
        self.assertEqual(both["client_name"], "phone")

    def test_traffic(self):
        with mock.patch.object(self.manager, "get_traffic_for_server", return_value={"clients": {"a": {"rx": 1}}}):
            self.assertEqual(self.client.get(self._url("traffic")).get_json(), {"clients": {"a": {"rx": 1}}})

    def test_egress_probe(self):
        probe = {"external_ip": "198.51.100.9", "service": "https://ident.me", "error": None}
        with mock.patch.object(self.manager, "probe_server_egress_ip", return_value=probe):
            payload = self.client.post(self._url("egress-ip"), json={}).get_json()
        self.assertEqual(payload, {"server_id": self.server["id"], "server_name": self.server["name"], **probe})

    def test_add_client_copies_and_overrides_params(self):
        source = self.client.post(
            self._url("clients"), json={"name": "a", "client_params": {"Jc": 5, "Jmin": 30, "Jmax": 60}}
        ).get_json()["client"]

        def params(body):
            added = self.client.post(self._url("clients"), json=body).get_json()["client"]
            return {key: added["client_params"][key] for key in ("Jc", "Jmin", "Jmax")}

        self.assertEqual(params({"name": "b", "copy_from_client_id": source["id"]}), {"Jc": 5, "Jmin": 30, "Jmax": 60})
        self.assertEqual(params({"name": "c", "copy_from_client_id": source["id"], "client_params": {"Jc": 9}}),
                         {"Jc": 9, "Jmin": 30, "Jmax": 60})  # fmt: skip
        defaults = self.manager.get_server(self.server["id"])["client_defaults"]
        self.assertEqual(params({"name": "d", "copy_from_client_id": "gone"}),
                         {key: defaults[key] for key in ("Jc", "Jmin", "Jmax")})  # fmt: skip

    def test_client_lists_are_scoped(self):
        other = _create_server(self.client, "o", "10.63.0.0/24", 51963)
        self.client.post(self._url("clients"), json={"name": "mine"})
        self.client.post(f"/api/servers/{other['id']}/clients", json={"name": "theirs"})
        names = lambda url: sorted(c["name"] for c in self.client.get(url).get_json())
        self.assertEqual(names(self._url("clients")), ["mine"])
        self.assertEqual(names("/api/clients"), ["mine", "theirs"])


class RouteErrorTests(unittest.TestCase):
    """Bad input is a 400 and anything unexpected a JSON 500 -- never Flask's HTML page."""

    def setUp(self):
        self.app, self.manager = build_app()
        self.client = self.app.test_client()
        self.server = _create_server(self.client, subnet="10.65.0.0/30", port=51965)
        self.url = f"/api/servers/{self.server['id']}"

    def test_invalid_client_params_are_a_400(self):
        response = self.client.post(f"{self.url}/clients", json={"name": "x", "client_params": {"Jmin": 100, "Jmax": 10}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Jmin", response.get_json()["error"])

        added = self.client.post(f"{self.url}/clients", json={"name": "ok"}).get_json()["client"]
        response = self.client.post(
            f"{self.url}/clients/{added['id']}/client-params", json={"client_params": {"Jmin": 100, "Jmax": 10}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Jmin", response.get_json()["error"])

    def test_full_subnet_is_a_400(self):
        # A /30 holds the server and exactly one client.
        self.assertEqual(self.client.post(f"{self.url}/clients", json={"name": "one"}).status_code, 200)
        response = self.client.post(f"{self.url}/clients", json={"name": "two"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("No free addresses", response.get_json()["error"])

    def test_non_object_body_is_a_400(self):
        response = self.client.post(f"{self.url}/rename", json=["not", "an", "object"])
        self.assertEqual((response.status_code, response.get_json()), (400, {"error": "Name cannot be empty"}))

    def test_unexpected_error_is_a_logged_json_500(self):
        with mock.patch.object(self.manager, "rename_server", side_effect=RuntimeError("disk full")), \
             self.assertLogs("routes.servers", "ERROR"):  # fmt: skip
            response = self.client.post(f"{self.url}/rename", json={"name": "x"})
        self.assertEqual((response.status_code, response.get_json()), (500, {"error": "disk full"}))

    def test_client_whose_server_is_gone_is_a_404(self):
        added = self.client.post(f"{self.url}/clients", json={"name": "orphan"}).get_json()["client"]
        self.manager.config["servers"] = []
        for suffix in ("/suspend", "/client-params", "/rename"):
            response = self.client.post(f"{self.url}/clients/{added['id']}{suffix}", json={"name": "x", "client_params": {}})
            self.assertEqual(response.status_code, 404, suffix)


class SystemRouteExtraTests(_RealSystemApp):
    def test_refresh_ip_updates_every_server(self):
        with mock.patch.object(self.manager, "detect_public_ip", return_value="198.51.100.20"), \
             mock.patch.object(self.manager, "lookup_geoip", return_value=("Somewhere", "NL")):  # fmt: skip
            payload = self.client.get("/api/system/refresh-ip").get_json()
        self.assertEqual(payload, {"public_ip": "198.51.100.20", "public_ip_geo_country_code": "NL"})
        self.assertEqual(self.manager.public_ip, "198.51.100.20")
        stored = json.loads(Path(self.manager.config_file).read_text(encoding="utf-8"))
        self.assertEqual({s["public_ip"] for s in stored["servers"]}, {"198.51.100.20"})

    def test_iptables_check_reports_found_missing_and_error(self):
        iface, subnet = self.server["interface"], self.server["subnet"]

        def fake_run(argv):
            return {"INPUT": f"-A INPUT -i {iface} -j ACCEPT", "FORWARD": None}.get(argv[-1], "-A POSTROUTING -s 10.99.0.0/24")

        with mock.patch.object(self.manager, "run_command", side_effect=fake_run):
            payload = self.client.get(f"/api/system/iptables-test?server_id={self.server['id']}").get_json()
        self.assertEqual(
            payload["iptables_check"],
            {
                f"iptables -S INPUT | grep {iface}": "Found",
                f"iptables -S FORWARD | grep {iface}": "Error",
                f"iptables -t nat -S POSTROUTING | grep {subnet}": "Not found",
            },
        )

    def test_container_uptime(self):
        with mock.patch("routes.system.subprocess") as sp, mock.patch("routes.system.time") as clock:
            sp.check_output.return_value = "1000000000\n"
            clock.time.return_value = 1_000_090_061
            response = self.client.get("/status")
        self.assertEqual(response.get_data(as_text=True), "Container Uptime: 1d 1h 1m 1s")
        self.assertEqual(sp.check_output.call_args.args[0], ["stat", "-c %Y", "/proc/1/cmdline"])


if __name__ == "__main__":
    unittest.main()
