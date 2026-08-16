"""Golden-file tests for the two WireGuard config renderers.

The fixtures in tests/golden/ double as format documentation: they show exactly
what a server and client .conf look like for each protocol generation. If a change
is intentional, review the diff and regenerate with:

    UPDATE_GOLDEN=1 python3 -m unittest tests.test_config_rendering

These renderers are the highest-value thing to pin down: they had already drifted
apart once (the create path emitted a stray blank line the rebuild path did not).
"""

import os
import unittest

from tests.support import (
    HEADER_PROTECTION_KEY,
    build_manager,
    normalize_conf,
    read_golden,
    write_golden,
)

UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


class ConfigRenderingTests(unittest.TestCase):
    maxDiff = None

    def _server_with_client(self, protocol, subnet, extra_client_params=None):
        manager = build_manager()
        server = manager.create_wireguard_server({
            "name": f"test-{protocol}",
            "protocol": protocol,
            "subnet": subnet,
            "port": 51820,
            "auto_start": False,
            "dns": "1.1.1.1,8.8.8.8",
            "mtu": 1420,
            # Fixed transport params so the golden file is deterministic.
            "transport_params": {
                "S1": 50, "S2": 60, "S3": 40, "S4": 20,
                "H1": "1000", "H2": "2000", "H3": "3000", "H4": "4000",
                "HeaderProtectionKey": HEADER_PROTECTION_KEY,
                "RandomTrailers": True,
                "DisableCookies": True,
            },
            "client_defaults": {"Jc": 8, "Jmin": 40, "Jmax": 70},
        })
        client, _ = manager.add_wireguard_client(server["id"], "phone")
        if extra_client_params:
            manager.update_client_params(server["id"], client["id"], extra_client_params)
        server = manager.get_server(server["id"])
        client = manager.get_client(client["id"])
        return manager, server, client

    def _check(self, name, text):
        text = normalize_conf(text)
        if UPDATE:
            write_golden(name, text)
            self.skipTest(f"golden updated: {name}")
        self.assertEqual(text, read_golden(name), f"{name} changed; review then UPDATE_GOLDEN=1")

    # --- server configs -------------------------------------------------------

    def test_server_conf_awg15(self):
        _, server, _ = self._server_with_client("AWG 1.5", "10.15.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            self._check("server-awg15.conf", f.read())

    def test_server_conf_awg20(self):
        _, server, _ = self._server_with_client("AWG 2.0", "10.20.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            self._check("server-awg20.conf", f.read())

    def test_server_conf_awg30(self):
        _, server, _ = self._server_with_client("AWG 3.0", "10.30.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            self._check("server-awg30.conf", f.read())

    def test_server_conf_awg31(self):
        _, server, _ = self._server_with_client("AWG 3.1", "10.31.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            self._check("server-awg31.conf", f.read())

    # --- client configs -------------------------------------------------------

    def test_client_conf_awg15(self):
        manager, server, client = self._server_with_client("AWG 1.5", "10.15.0.0/24")
        self._check(
            "client-awg15.conf",
            manager.generate_wireguard_client_config(server, client, include_comments=True),
        )

    def test_client_conf_awg20(self):
        manager, server, client = self._server_with_client("AWG 2.0", "10.20.0.0/24")
        self._check(
            "client-awg20.conf",
            manager.generate_wireguard_client_config(server, client, include_comments=False),
        )

    def test_client_conf_awg30(self):
        manager, server, client = self._server_with_client(
            "AWG 3.0", "10.30.0.0/24",
            extra_client_params={
                "Jc": 8, "Jmin": 40, "Jmax": 70,
                "ContentPaddingAddition": "10-40",
                "RekeyAfterTime": "110-130",
                "KeepaliveTimeout": "22-30",
                "MaxHandshakeAttempts": "12",
            },
        )
        self._check(
            "client-awg30.conf",
            manager.generate_wireguard_client_config(server, client, include_comments=False),
        )

    def test_client_conf_awg31(self):
        manager, server, client = self._server_with_client(
            "AWG 3.1", "10.31.0.0/24",
            extra_client_params={
                "Jc": 8, "Jmin": 40, "Jmax": 70,
                "ContentPaddingAddition": "10-40",
                "RekeyAfterTime": "110-130",
                "KeepaliveTimeout": "22-30",
                "MaxHandshakeAttempts": "12",
            },
        )
        self._check(
            "client-awg31.conf",
            manager.generate_wireguard_client_config(server, client, include_comments=False),
        )

    # --- invariants the golden files alone would not catch --------------------

    def test_create_and_rebuild_produce_identical_output(self):
        """The create path and every later rewrite must use the same renderer."""
        manager, server, _ = self._server_with_client("AWG 2.0", "10.21.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            on_disk = f.read()
        self.assertEqual(on_disk, manager._build_server_config_content(server))

    def test_server_conf_is_not_world_readable(self):
        """It embeds PrivateKey; awg-quick warns when it is world accessible."""
        _, server, _ = self._server_with_client("AWG 1.5", "10.22.0.0/24")
        self.assertEqual(os.stat(server["config_path"]).st_mode & 0o777, 0o600)

    def test_newer_protocol_keys_absent_on_older_protocols(self):
        """Protocol-specific keys must never leak into an older config."""
        for protocol, subnet in (
            ("AWG 1.5", "10.23.0.0/24"),
            ("AWG 2.0", "10.24.0.0/24"),
            ("AWG 3.0", "10.27.0.0/24"),
        ):
            manager, server, client = self._server_with_client(protocol, subnet)
            with open(server["config_path"], encoding="utf-8") as f:
                server_text = f.read()
            client_text = manager.generate_wireguard_client_config(server, client)
            for blob, label in ((server_text, "server"), (client_text, "client")):
                if not manager.protocol_supports_awg3(protocol):
                    self.assertNotIn("HeaderProtectionKey", blob, f"{protocol} {label}")
                    self.assertNotIn("ContentPaddingAddition", blob, f"{protocol} {label}")
                    self.assertNotIn("KeepaliveTimeout", blob, f"{protocol} {label}")
                self.assertNotIn("RandomTrailers", blob, f"{protocol} {label}")
                self.assertNotIn("DisableCookies", blob, f"{protocol} {label}")

    def test_awg31_options_mirrored_to_client(self):
        manager, server, client = self._server_with_client("AWG 3.1", "10.31.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            server_text = f.read()
        client_text = manager.generate_wireguard_client_config(server, client)
        for line in ("RandomTrailers = on", "DisableCookies = on"):
            self.assertIn(line, server_text)
            self.assertIn(line, client_text)

        server["transport_params"]["RandomTrailers"] = False
        server["transport_params"]["DisableCookies"] = False
        server_text = manager.write_server_conf(server)
        client_text = manager.generate_wireguard_client_config(server, client)
        for line in ("RandomTrailers = off", "DisableCookies = off"):
            self.assertIn(line, server_text)
            self.assertIn(line, client_text)

    def test_header_protection_key_mirrored_to_client(self):
        """It is server-side: both ends must carry the identical value."""
        manager, server, client = self._server_with_client("AWG 3.0", "10.25.0.0/24")
        with open(server["config_path"], encoding="utf-8") as f:
            server_text = f.read()
        client_text = manager.generate_wireguard_client_config(server, client)
        line = f"HeaderProtectionKey = {HEADER_PROTECTION_KEY}"
        self.assertIn(line, server_text)
        self.assertIn(line, client_text)

    def test_suspended_client_peer_block_removed(self):
        manager, server, client = self._server_with_client("AWG 1.5", "10.26.0.0/24")
        manager.toggle_client_suspend(server["id"], client["id"])
        with open(server["config_path"], encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("[Peer]", text)
        self.assertNotIn(client["client_public_key"], text)

    def test_names_cannot_inject_config_directives(self):
        """A newline in a name would end the '# Client:' comment (see §5 #19)."""
        manager = build_manager()
        server = manager.create_wireguard_server({
            "name": "srv\nListenPort = 1", "protocol": "AWG 1.5",
            "subnet": "10.28.0.0/24", "auto_start": False,
        })
        client, _ = manager.add_wireguard_client(
            server["id"], "evil\nPersistentKeepalive = 1\n# x")
        manager.rename_client(server["id"], client["id"], "worse\nMTU = 99")

        server = manager.get_server(server["id"])
        with open(server["config_path"], encoding="utf-8") as f:
            server_text = f.read()
        client_text = manager.generate_wireguard_client_config(
            server, manager.get_client(client["id"]), include_comments=True)

        for blob, label in ((server_text, "server"), (client_text, "client")):
            for line in blob.splitlines():
                if line.lstrip().startswith("#"):
                    continue  # payload trapped in a comment is harmless
                self.assertNotIn("MTU = 99", line, label)
                self.assertNotIn("ListenPort = 1", line, label)
            # And no name may span lines.
            self.assertEqual(blob.count("# Client:"), blob.count("[Peer]"), label)

    def test_duplicate_client_names_delete_only_one_peer(self):
        """Regression: deleting by '# Client: <name>' match removed both blocks."""
        manager = build_manager()
        server = manager.create_wireguard_server({
            "name": "dup", "protocol": "AWG 1.5", "subnet": "10.27.0.0/24",
            "auto_start": False,
        })
        first, _ = manager.add_wireguard_client(server["id"], "same-name")
        second, _ = manager.add_wireguard_client(server["id"], "same-name")

        with open(server["config_path"], encoding="utf-8") as f:
            self.assertEqual(f.read().count("[Peer]"), 2)

        manager.delete_client(server["id"], first["id"])
        with open(server["config_path"], encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("[Peer]"), 1)
        self.assertIn(second["client_public_key"], text)
        self.assertNotIn(f"{first['client_ip']}/32", text)


if __name__ == "__main__":
    unittest.main()
