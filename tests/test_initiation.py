"""Tests for services/initiation.py: naming the device behind a handshake initiation
and what its parameters got wrong (DEVELOPMENT.md §10, 2.8, part 1).

The datagrams are tests/fixtures/handshakes/capture.jsonl, every one the real daemon's
two ports received from five devices (run.sh there, with throwaway keys.json):
  A  old S1-S4/H1-H4, trailers on, the current key (51820)  -> S1 31, H1 500000-500999
  B  old S1-S4/H1-H4, no header protection (51821)          -> S1 33, H1 1500000-1500999
  C  current S/H, an old HeaderProtectionKey not in keys.json -> nobody
  D  a config for another server's key                        -> nobody
  E  correct                                                   -> S1 40, H1 100000-100999
The server sides are run.sh's CUR0 (51820) and CUR1 (51821). Other keys' cases (a
rotated key, protection switched on or off) re-key those servers' parameters.
"""

import json
import os
import secrets
import time
import unittest
from base64 import b64encode
from pathlib import Path

# Tests exercise internals on purpose and use self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,wrong-import-order
import tests.support  # noqa: F401 -- puts web-ui on the path

from services import initiation  # isort: skip -- after tests.support
from services.initiation import Initiation  # isort: skip

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "handshakes"
KEYS = json.loads((FIXTURE / "keys.json").read_text(encoding="utf-8"))
RECORDS = [json.loads(line) for line in (FIXTURE / "capture.jsonl").read_text(encoding="utf-8").splitlines()]
K1 = KEYS["servers"]["51820"]["hpk"]
PRIVATE = {int(port): server["priv"] for port, server in KEYS["servers"].items()}
CASE = {source: case for case, source in KEYS["sources"].items()}
CLIENT = {key: case for case, key in KEYS["clients"].items()}

# run.sh's current parameters, as web_config.json stores them (AWG 3.1 on 51820).
CUR0 = {"S1": 40, "S2": 30, "S3": 50, "S4": 20, "H1": "100000-100999", "H2": "200000-200999",
        "H3": "300000-300999", "H4": "400000-400999", "HeaderProtectionKey": K1,
        "RandomTrailers": False, "DisableCookies": False}  # fmt: skip
CUR1 = {"S1": 55, "S2": 66, "S3": 40, "S4": 20, "H1": "1100000-1100999", "H2": "1200000-1200999",
        "H3": "1300000-1300999", "H4": "1400000-1400999"}  # fmt: skip
CURRENT = {51820: CUR0, 51821: CUR1}
TRAILERS_ON = {**CUR0, "RandomTrailers": True}


def new_key():
    return b64encode(secrets.token_bytes(32)).decode("ascii")


def datagrams(case):
    return [(rec["dport"], bytes.fromhex(rec["hex"])) for rec in RECORDS if CASE[rec["src"]] == case]


def identified(case, keys=None):
    """identify() over every datagram of a case: (datagram, Initiation) for those that
    name someone, each tried under its port's current key unless `keys` is given."""
    found = []
    for port, datagram in datagrams(case):
        tried = initiation.protection_keys(CURRENT[port]) if keys is None else keys
        hit = initiation.identify(datagram, PRIVATE[port], tried)
        if hit:
            found.append((datagram, hit))
    return found


class FixtureTests(unittest.TestCase):
    """The answers DEVELOPMENT.md §10 *Fixture* expects, from the real daemon's capture."""

    def test_old_parameters_with_trailers_are_named(self):
        found = identified("A")
        self.assertEqual(len(found), 5)
        for datagram, hit in found:
            self.assertEqual(CLIENT[hit.public_key], "A")
            self.assertEqual(hit.offset, 31)
            self.assertTrue(500000 <= hit.msg_type <= 500999, hit.msg_type)
            self.assertGreater(hit.trailer, 0)
            self.assertEqual(hit.offset + initiation.INITIATION_SIZE + hit.trailer, len(datagram))
            self.assertEqual(hit.key_index, 0)  # the server's current key
            self.assertEqual(initiation.key_label(hit, CUR0), "current")
        self.assertEqual(len({hit.msg_type for _, hit in found}), 5, "H1 is drawn per initiation")
        self.assertEqual(initiation.mismatch([hit for _, hit in found], CUR0), ["S1", "H1", "RandomTrailers"])

    def test_old_parameters_without_protection_are_named(self):
        found = identified("B")
        self.assertEqual(len(found), 5)
        for datagram, hit in found:
            self.assertEqual(CLIENT[hit.public_key], "B")
            self.assertEqual((hit.offset, hit.trailer, hit.key_index), (33, 0, None))
            self.assertTrue(1500000 <= hit.msg_type <= 1500999, hit.msg_type)
            self.assertEqual(len(datagram), 33 + initiation.INITIATION_SIZE)
            self.assertEqual(initiation.key_label(hit, CUR1), "none")
        self.assertEqual(initiation.mismatch([hit for _, hit in found], CUR1), ["S1", "H1"])

    def test_an_unknown_protection_key_names_nobody(self):
        self.assertEqual(identified("C"), [])

    def test_another_servers_key_names_nobody(self):
        self.assertEqual(identified("D"), [])
        # Not under the other port's private key either.
        self.assertEqual([d for _, d in datagrams("D") if initiation.identify(d, PRIVATE[51821], [K1])], [])

    def test_the_correct_device_is_named_and_fits(self):
        found = identified("E")
        self.assertEqual(len(found), 1)
        _, hit = found[0]
        self.assertEqual(CLIENT[hit.public_key], "E")
        self.assertEqual((hit.offset, hit.msg_type, hit.trailer, hit.key_index), (40, 100939, 0, 0))
        self.assertEqual(initiation.mismatch([hit], CUR0), [])
        self.assertTrue(initiation.matches(hit, CUR0))

    def test_nothing_but_initiations_names_anyone(self):
        # 166 datagrams: I1 (<r 60>), Jc junk (40-80 bytes), the initiations and E's
        # data (148 bytes, so a single offset, under each key and without). Only the 11
        # initiations of A, B and E decrypt.
        named = 0
        for rec in RECORDS:
            datagram = bytes.fromhex(rec["hex"])
            if initiation.identify(datagram, PRIVATE[rec["dport"]], [K1, new_key()]):
                named += 1
        self.assertEqual(len(RECORDS), 166)
        self.assertEqual(named, 11)
        data = [d for _, d in datagrams("E") if len(d) == initiation.INITIATION_SIZE]
        self.assertGreaterEqual(len(data), 40)
        self.assertEqual([d for d in data if initiation.identify(d, PRIVATE[51820], [K1])], [])


class KeyTests(unittest.TestCase):
    """The HeaderProtectionKey a device used, against the server's current and previous."""

    def test_a_previous_key_is_found_and_named_previous(self):
        rotated = {**CUR0, "HeaderProtectionKey": new_key()}
        keys = initiation.protection_keys(rotated, [new_key(), K1])
        self.assertEqual(len(keys), 3)
        found = identified("E", keys)
        self.assertEqual(len(found), 1)
        _, hit = found[0]
        self.assertEqual(hit.key_index, 2)
        self.assertEqual(initiation.key_label(hit, rotated), "previous")
        self.assertEqual(initiation.mismatch([hit], rotated), ["HeaderProtectionKey"])
        self.assertEqual(initiation.describe(hit, rotated)["key"], "previous")

    def test_without_the_previous_key_the_device_is_unreadable(self):
        rotated = {**CUR0, "HeaderProtectionKey": new_key()}
        self.assertEqual(identified("E", initiation.protection_keys(rotated)), [])

    def test_a_key_while_the_server_has_none(self):
        # Protection switched off (the key kept as a previous one): the device still has it.
        off = {key: value for key, value in CUR0.items() if key != "HeaderProtectionKey"}
        keys = initiation.protection_keys(off, [K1])
        self.assertEqual(keys, [K1])
        _, hit = identified("E", keys)[0]
        self.assertEqual(hit.key_index, 0)
        self.assertEqual(initiation.key_label(hit, off), "previous")
        self.assertEqual(initiation.mismatch([hit], off), ["HeaderProtectionKey"])

    def test_none_while_the_server_has_one(self):
        # Protection switched on: the device's initiations are still in the clear.
        on = {**CUR1, "HeaderProtectionKey": new_key()}
        found = identified("B", initiation.protection_keys(on))
        self.assertEqual(len(found), 5)
        hits = [hit for _, hit in found]
        self.assertEqual({hit.key_index for hit in hits}, {None})
        self.assertEqual(initiation.key_label(hits[0], on), "none")
        self.assertEqual(initiation.mismatch(hits, on), ["S1", "H1", "HeaderProtectionKey"])

    def test_a_server_without_protection_reads_its_own_devices(self):
        # B's server has no key and never had one: identify tries the clear text alone.
        self.assertEqual(initiation.protection_keys(CUR1), [])
        fitting = {**CUR1, "S1": 33, "H1": "1500000-1500999"}
        hits = [hit for _, hit in identified("B", [])]
        self.assertEqual(len(hits), 5)
        self.assertEqual(initiation.mismatch(hits, fitting), [])
        self.assertTrue(all(initiation.matches(hit, fitting) for hit in hits))

    def test_protection_keys_order_and_duplicates(self):
        old, older = new_key(), new_key()
        self.assertEqual(initiation.protection_keys(CUR0, [old, K1, "", older]), [K1, old, older])
        self.assertEqual(initiation.protection_keys({}, [old]), [old])
        self.assertEqual(initiation.protection_keys(None), [])


class MismatchTests(unittest.TestCase):
    """The comparison alone, on initiations as identify returns them (DEVELOPMENT.md §10,
    *Verdict*): each parameter, and RandomTrailers judged over several (F5)."""

    KEY = "Zi6GX55mR+QJb9NrnDfjjJOMOLafxKSORaiJ558573k="

    def init(self, offset=40, msg_type=100500, trailer=0, key_index=0):
        return Initiation(self.KEY, offset, msg_type, trailer, key_index)

    def test_each_parameter(self):
        self.assertEqual(initiation.mismatch([self.init()], CUR0), [])
        self.assertEqual(initiation.mismatch([self.init(offset=41)], CUR0), ["S1"])
        self.assertEqual(initiation.mismatch([self.init(msg_type=99999)], CUR0), ["H1"])
        self.assertEqual(initiation.mismatch([self.init(msg_type=101000)], CUR0), ["H1"])
        self.assertEqual(initiation.mismatch([self.init(msg_type=100000), self.init(msg_type=100999)], CUR0), [])
        self.assertEqual(initiation.mismatch([self.init(key_index=None)], CUR0), ["HeaderProtectionKey"])
        self.assertEqual(initiation.mismatch([self.init(key_index=1)], CUR0), ["HeaderProtectionKey"])
        self.assertEqual(initiation.mismatch([self.init(trailer=7)], CUR0), ["RandomTrailers"])

    def test_any_initiation_that_differs_counts(self):
        mixed = [self.init(), self.init(offset=31), self.init(trailer=3)]
        self.assertEqual(initiation.mismatch(mixed, CUR0), ["S1", "RandomTrailers"])
        self.assertEqual([initiation.matches(one, CUR0) for one in mixed], [True, False, False])

    def test_missing_trailers_need_two_initiations(self):
        self.assertEqual(initiation.mismatch([self.init()], TRAILERS_ON), [])
        self.assertEqual(initiation.mismatch([self.init(), self.init()], TRAILERS_ON), ["RandomTrailers"])
        # One random trailer may be 0 bytes long: one with trailers is enough.
        self.assertEqual(initiation.mismatch([self.init(), self.init(trailer=12)], TRAILERS_ON), [])
        self.assertTrue(initiation.matches(self.init(), TRAILERS_ON))

    def test_header_values_and_unset_parameters(self):
        single = {"S1": 30, "H1": "1182367"}
        self.assertEqual(initiation.mismatch([self.init(offset=30, msg_type=1182367, key_index=None)], single), [])
        self.assertEqual(initiation.mismatch([self.init(offset=30, msg_type=1182368, key_index=None)], single), ["H1"])
        # Unset S1/H1 are the daemon's defaults: no padding, WireGuard's type 1.
        self.assertEqual(initiation.mismatch([self.init(offset=0, msg_type=1, key_index=None)], {}), [])
        self.assertEqual(initiation.mismatch([self.init(offset=0, msg_type=1, key_index=None)], {"S1": None}), [])
        self.assertEqual(initiation.mismatch([], CUR0), [])

    def test_contract_shapes(self):
        device = initiation.describe(self.init(offset=31, msg_type=500284, trailer=9), CUR0)
        self.assertEqual(device, {"S1": 31, "H1": 500284, "trailers": True, "key": "current"})
        self.assertEqual(initiation.server_view(CUR0), {"S1": 40, "H1": "100000-100999", "trailers": False, "key": True})
        self.assertEqual(initiation.server_view({}), {"S1": 0, "H1": "1", "trailers": False, "key": False})
        self.assertNotIn(K1, json.dumps([device, initiation.server_view(CUR0)]), "never a key in a payload")


class InputTests(unittest.TestCase):
    def test_short_and_random_datagrams_name_nobody(self):
        for size in (0, 12, 147, 148, 149, 188, 600, 1500):
            datagram = os.urandom(size)
            self.assertIsNone(initiation.identify(datagram, PRIVATE[51820], [K1]))
            self.assertIsNone(initiation.identify(bytearray(datagram), PRIVATE[51821]))

    def test_a_low_order_ephemeral_is_skipped(self):
        # An all-zero ephemeral key is a low-order point: X25519 refuses it, no match.
        self.assertIsNone(initiation.identify(bytes(initiation.INITIATION_SIZE), PRIVATE[51821]))

    def test_a_full_size_datagram_stays_cheap(self):
        # The capture in part 2 reads up to 64 datagrams; a 1500-byte one under two
        # keys and none is ~4000 exchanges, ~90 ms. Generous bound: no accidental blow-up.
        started = time.perf_counter()
        initiation.identify(os.urandom(1500), PRIVATE[51820], [K1, new_key()])
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
