"""Tests for client IP allocation, server conflicts and the GeoIP cache bound."""

import unittest

# Tests exercise internals on purpose and use self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access

from tests.support import build_manager


class ClientIpAllocationTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self._next_port = 51820

    def _server(self, subnet, name="alloc"):
        # Distinct ports: creating a server now rejects a port already in use.
        self._next_port += 1
        return self.manager.create_wireguard_server({
            "name": name, "protocol": "AWG 1.5", "subnet": subnet,
            "port": self._next_port, "auto_start": False,
        })

    def test_first_client_gets_the_first_free_address(self):
        server = self._server("10.5.0.0/24")
        self.assertEqual(self.manager.get_client_ip(server), "10.5.0.2")

    def test_addresses_are_sequential_and_unique(self):
        server = self._server("10.6.0.0/24")
        for expected in ("10.6.0.2", "10.6.0.3", "10.6.0.4"):
            client, _ = self.manager.add_wireguard_client(server["id"], f"c-{expected}")
            self.assertEqual(client["client_ip"], expected)

    def test_gap_left_by_a_deleted_client_is_reused(self):
        server = self._server("10.7.0.0/24")
        first, _ = self.manager.add_wireguard_client(server["id"], "a")
        self.manager.add_wireguard_client(server["id"], "b")
        self.manager.delete_client(server["id"], first["id"])
        reused, _ = self.manager.add_wireguard_client(server["id"], "c")
        self.assertEqual(reused["client_ip"], first["client_ip"])

    def test_server_address_is_never_handed_out(self):
        server = self._server("10.8.0.0/24")
        self.assertNotEqual(self.manager.get_client_ip(server), server["server_ip"])

    def test_non_slash_24_subnets(self):
        """The old implementation hardcoded a /24 third-octet prefix."""
        for subnet, expected in (
            ("10.9.0.0/16", "10.9.0.2"),
            ("10.10.0.0/20", "10.10.0.2"),
            ("192.168.50.0/28", "192.168.50.2"),
        ):
            server = self._server(subnet, name=f"s-{subnet}")
            self.assertEqual(self.manager.get_client_ip(server), expected, subnet)

    def test_allocation_crosses_octet_boundary_on_a_large_subnet(self):
        """A /16 has more than 254 usable hosts; the old code stopped at .254."""
        server = self._server("10.11.0.0/16")
        # Occupy the whole first octet range so the next free address must roll over.
        server["clients"] = [{"client_ip": f"10.11.0.{n}"} for n in range(2, 256)]
        self.assertEqual(self.manager.get_client_ip(server), "10.11.1.0")

    def test_addresses_used_by_the_global_map_are_respected(self):
        """Clients are stored twice; a stale embedded list must not cause a duplicate."""
        server = self._server("10.12.0.0/24")
        self.manager.config["clients"]["ghost"] = {
            "id": "ghost", "server_id": server["id"], "client_ip": "10.12.0.2",
        }
        self.assertEqual(self.manager.get_client_ip(server), "10.12.0.3")

    def test_full_subnet_raises_rather_than_reusing(self):
        server = self._server("10.13.0.0/30")   # exactly two usable hosts
        server["clients"] = [{"client_ip": "10.13.0.2"}]
        with self.assertRaises(ValueError) as ctx:
            self.manager.get_client_ip(server)
        self.assertIn("No free addresses", str(ctx.exception))


class ServerConflictTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()
        self.manager.create_wireguard_server({
            "name": "first", "subnet": "10.20.0.0/24", "port": 51820, "auto_start": False,
        })

    def test_duplicate_port_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.manager.create_wireguard_server({
                "name": "second", "subnet": "10.21.0.0/24", "port": 51820, "auto_start": False,
            })
        self.assertIn("Port 51820", str(ctx.exception))

    def test_identical_subnet_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.manager.create_wireguard_server({
                "name": "second", "subnet": "10.20.0.0/24", "port": 51821, "auto_start": False,
            })
        self.assertIn("overlaps", str(ctx.exception))

    def test_overlapping_supernet_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.create_wireguard_server({
                "name": "second", "subnet": "10.20.0.0/16", "port": 51822, "auto_start": False,
            })

    def test_non_overlapping_subnet_and_free_port_accepted(self):
        server = self.manager.create_wireguard_server({
            "name": "second", "subnet": "10.22.0.0/24", "port": 51823, "auto_start": False,
        })
        self.assertEqual(server["subnet"], "10.22.0.0/24")

    def test_conflict_check_can_ignore_a_server(self):
        """Used when re-validating an existing server against the others."""
        existing = self.manager.config["servers"][0]
        self.manager.assert_no_conflicts(
            existing["port"], existing["subnet"], ignore_server_id=existing["id"])

    def test_a_stored_invalid_subnet_does_not_block_new_servers(self):
        self.manager.config["servers"][0]["subnet"] = "not-a-subnet"
        self.manager.create_wireguard_server({
            "name": "third", "subnet": "10.23.0.0/24", "port": 51824, "auto_start": False,
        })


class GeoipCacheTests(unittest.TestCase):
    def setUp(self):
        self.manager = build_manager()

    def test_cache_is_bounded(self):
        limit = self.manager.GEOIP_CACHE_MAX_ENTRIES
        for n in range(limit + 50):
            self.manager._cache_geoip(f"198.51.{n // 256}.{n % 256}", 1000.0, None, None, {})
        self.assertLessEqual(len(self.manager._geoip_cache), limit)

    def test_expired_entries_are_evicted_first(self):
        limit = self.manager.GEOIP_CACHE_MAX_ENTRIES
        ttl = self.manager.GEOIP_CACHE_TTL_SECONDS
        now = 100000.0
        # Fill with stale entries, then add one fresh entry past the limit.
        for n in range(limit):
            self.manager._cache_geoip(f"203.0.{n // 256}.{n % 256}", now - ttl - 1, None, None, {})
        self.manager._cache_geoip("192.0.2.7", now, "Somewhere", "SE", {})
        self.assertIn("192.0.2.7", self.manager._geoip_cache)
        self.assertLess(len(self.manager._geoip_cache), limit)

    def test_oldest_evicted_when_all_entries_are_fresh(self):
        limit = self.manager.GEOIP_CACHE_MAX_ENTRIES
        for n in range(limit):
            self.manager._cache_geoip(f"203.1.{n // 256}.{n % 256}", 1000.0 + n, None, None, {})
        oldest = "203.1.0.0"
        self.assertIn(oldest, self.manager._geoip_cache)
        self.manager._cache_geoip("192.0.2.9", 99999.0, None, None, {})
        self.assertNotIn(oldest, self.manager._geoip_cache)
        self.assertIn("192.0.2.9", self.manager._geoip_cache)


if __name__ == "__main__":
    unittest.main()
