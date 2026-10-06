"""Tests for services/signatures.py and Generate for I1-I5 (POST /api/generate).

Each packet is rendered the way amneziawg-go builds it (device/obf*.go: <b> fixed
bytes, <r>/<rc>/<rd> drawn on every send, <t> a 4-byte timestamp) and parsed back by
the RFCs: a QUIC Initial's fields and its Length (RFC 9000), a DNS query's sections
and its OPT record (RFC 1035, 6891, 7873). Plus the rule the module exists for:
nothing a real client draws per connection may sit in <b>, so two sends differ there.
"""

import os
import re
import secrets
import string
import struct
import unittest
from unittest import mock

# tests.support puts web-ui on sys.path. Tests exercise internals on purpose and use
# self-describing method names.
# pylint: disable=missing-function-docstring,missing-class-docstring,wrong-import-order
from tests.support import build_app, build_manager

from services import signatures  # isort: skip -- after tests.support, which puts web-ui on the path

DRAWS = 200
TAG = re.compile(r"<(\w+)(?: ([^>]*))?>")


def wire(tags):
    """The bytes one send of a tag string puts on the wire."""
    assert TAG.sub("", tags) == "", f"text outside tags: {tags}"
    out = b""
    for tag, arg in TAG.findall(tags):
        if tag == "b":
            out += bytes.fromhex(arg.removeprefix("0x"))
        elif tag == "r":
            out += os.urandom(int(arg))
        elif tag == "rc":
            out += "".join(secrets.choice(string.ascii_letters) for _ in range(int(arg))).encode()
        elif tag == "rd":
            out += "".join(secrets.choice(string.digits) for _ in range(int(arg))).encode()
        elif tag == "t":
            out += struct.pack(">I", 1_790_000_000)
        else:
            raise AssertionError(f"unexpected tag <{tag}>")
    return out


def quic_varint(data, at):
    """(value, next offset) for an RFC 9000 §16 varint."""
    length = 1 << (data[at] >> 6)
    value = data[at] & 0x3F
    for byte in data[at + 1 : at + length]:
        value = value << 8 | byte
    return value, at + length


def parse_quic_initial(data):
    first = data[0]
    version = int.from_bytes(data[1:5])
    at = 5
    dcid_len = data[at]
    dcid = data[at + 1 : at + 1 + dcid_len]
    at += 1 + dcid_len
    scid_len = data[at]
    at += 1 + scid_len
    token_len, at = quic_varint(data, at)
    at += token_len
    length, at = quic_varint(data, at)
    return {
        "first": first, "version": version, "dcid_len": dcid_len, "dcid": dcid, "scid_len": scid_len,
        "token_len": token_len, "length": length, "after_length": len(data) - at,
    }  # fmt: skip


def parse_dns_query(data):
    txid, flags, qd, an, ns, ar = struct.unpack(">6H", data[:12])
    at, labels = 12, []
    while data[at]:
        labels.append(data[at + 1 : at + 1 + data[at]].decode("ascii"))
        at += 1 + data[at]
    at += 1
    qtype, qclass = struct.unpack(">HH", data[at : at + 4])
    at += 4
    opt_name = data[at]
    opt_type, udp_size, ttl, rdlength = struct.unpack(">HHIH", data[at + 1 : at + 11])
    at += 11
    option, option_len = struct.unpack(">HH", data[at : at + 4])
    at += 4 + option_len
    return {
        "txid": txid, "flags": flags, "counts": (qd, an, ns, ar), "name": ".".join(labels), "qtype": qtype,
        "qclass": qclass, "opt": (opt_name, opt_type, udp_size, ttl, rdlength), "option": (option, option_len),
        "rest": len(data) - at,
    }  # fmt: skip


class QuicProfileTests(unittest.TestCase):
    def test_i1_and_i2_are_client_initials_by_rfc_9000(self):
        for _ in range(DRAWS):
            packets = signatures.signature_packets("quic", mtu=1280, port=443)["packets"]
            initials = [parse_quic_initial(wire(packets[k])) for k in ("I1", "I2")]
            for key, initial in zip(("I1", "I2"), initials, strict=True):
                size = len(wire(packets[key]))
                self.assertIn(size, signatures.QUIC_INITIAL_SIZES)
                self.assertGreaterEqual(size, 1200)  # §14.1: servers drop a shorter one
                self.assertEqual(initial["first"] & 0xF0, 0xC0)  # long header, fixed bit, Initial
                self.assertEqual(initial["version"], 1)
                self.assertTrue(8 <= initial["dcid_len"] <= 20)  # §7.2, §17.2
                self.assertTrue(0 <= initial["scid_len"] <= 20)
                self.assertEqual(initial["token_len"], 0)
                # Length covers the packet number and the payload: everything after it.
                self.assertEqual(initial["length"], initial["after_length"])
            # One client: the same id lengths and size in both.
            self.assertEqual(initials[0]["dcid_len"], initials[1]["dcid_len"])
            self.assertEqual(initials[0]["scid_len"], initials[1]["scid_len"])

    def test_the_connection_ids_are_drawn_on_every_send(self):
        # In <b> they would repeat before every handshake: one client's fingerprint.
        i1 = signatures.signature_packets("quic", mtu=1280, port=443)["packets"]["I1"]
        first, second = parse_quic_initial(wire(i1)), parse_quic_initial(wire(i1))
        self.assertNotEqual(first["dcid"], second["dcid"])
        fixed = "".join(arg for tag, arg in TAG.findall(i1) if tag == "b")
        self.assertLessEqual(len(fixed.replace("0x", "")) // 2, 1 + 4 + 1 + 1 + 1 + 2)  # header fields only

    def test_i3_to_i5_are_short_header_packets(self):
        for _ in range(DRAWS):
            packets = signatures.signature_packets("quic", mtu=1280, port=443)["packets"]
            for key in ("I3", "I4", "I5"):
                data = wire(packets[key])
                self.assertEqual(data[0] & 0xC0, 0x40, key)  # header form 0, fixed bit 1
                self.assertTrue(signatures.QUIC_SHORT[0] <= len(data) <= signatures.QUIC_SHORT[1], key)

    def test_the_length_is_a_two_byte_varint(self):
        with mock.patch.object(signatures.secrets, "choice", return_value=1252):
            packets = signatures.signature_packets("quic", mtu=1280, port=443)["packets"]
        self.assertEqual(len(wire(packets["I1"])), 1252)
        self.assertEqual(signatures.quic_varint(1220), "44c4")
        with self.assertRaises(ValueError):
            signatures.quic_varint(0x4000)


class DnsProfileTests(unittest.TestCase):
    def test_i1_asks_for_a_and_i2_for_aaaa_by_rfc_1035_and_6891(self):
        for _ in range(DRAWS // 4):
            answer = signatures.signature_packets("dns", mtu=1280, port=53, host="Www.Example.COM.")
            self.assertEqual(answer["host"], "www.example.com")
            packets = answer["packets"]
            for key, qtype in (("I1", 1), ("I2", 28)):
                query = parse_dns_query(wire(packets[key]))
                self.assertEqual(query["flags"], 0x0100)  # a standard query, recursion desired
                self.assertEqual(query["counts"], (1, 0, 0, 1))
                self.assertEqual(query["name"], "www.example.com")
                self.assertEqual((query["qtype"], query["qclass"]), (qtype, 1))
                # OPT: root name, type 41, UDP size, TTL 0, RDLENGTH covering the cookie option.
                self.assertEqual(query["opt"], (0, 41, 1232, 0, 12))
                self.assertEqual(query["option"], (10, 8))  # COOKIE, an 8-byte client cookie
                self.assertEqual(query["rest"], 0)
            self.assertEqual(packets["I3"] + packets["I4"] + packets["I5"], "")

    def test_the_transaction_id_and_the_cookie_are_drawn_on_every_send(self):
        i1 = signatures.signature_packets("dns", mtu=1280, port=53, host="ya.ru")["packets"]["I1"]
        sends = [wire(i1) for _ in range(4)]
        self.assertGreater(len({send[:2] for send in sends}), 1)
        self.assertGreater(len({send[-8:] for send in sends}), 1)

    def test_an_empty_host_is_a_common_name_with_both_records(self):
        answer = signatures.signature_packets("dns", mtu=1280, port=53, host="  ")
        self.assertIn(answer["host"], signatures.DEFAULT_HOSTS)
        self.assertEqual(parse_dns_query(wire(answer["packets"]["I1"]))["name"], answer["host"])

    def test_a_bad_host_is_refused(self):
        too_long = ".".join(["a" * 60] * 5)
        for host in (
            "exa mple.com",
            "-a.com",
            "a-.com",
            f"{'a' * 64}.com",
            "localhost",
            "1.2.3.4",
            "a..b",
            "ex_ample.com",
            too_long,
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                signatures.clean_host(host)
        self.assertEqual(signatures.clean_host("xn--d1acufc.xn--p1ai"), "xn--d1acufc.xn--p1ai")


class RandomProfileTests(unittest.TestCase):
    def test_five_packets_with_nothing_fixed(self):
        for _ in range(DRAWS):
            packets = signatures.signature_packets("random", mtu=1280, port=51820)["packets"]
            self.assertTrue(all(packets[k] for k in signatures.SIGNATURE_KEYS))
            for value in packets.values():
                self.assertNotIn("<b", value)
                self.assertTrue(wire(value))


class ProfileRulesTests(unittest.TestCase):
    def setUp(self):
        self.m = build_manager()

    def test_every_draw_passes_the_validators_without_a_warning(self):
        for profile in ("quic", "dns", "random"):
            for _ in range(DRAWS):
                packets = signatures.signature_packets(profile, mtu=1280, port=443)["packets"]
                params = {"Jc": 8, "Jmin": 8, "Jmax": 80, **packets}
                self.m.validate_client_params(params)
                self.assertEqual(self.m.client_param_warnings(params, 1280), [], profile)
                for key, value in packets.items():
                    if value:
                        self.assertEqual(self.m.parse_signature_packet(key, value)[0], len(wire(value)))

    def test_the_port_hint(self):
        def notes(profile, port):
            return signatures.signature_packets(profile, mtu=1280, port=port)["notes"]

        self.assertEqual(notes("quic", 443), [])
        self.assertEqual(notes("dns", 53), [])
        self.assertEqual(notes("random", 51820), [])
        self.assertIn("UDP 443", notes("quic", 51820)[0])
        self.assertIn("51820", notes("quic", 51820)[0])
        self.assertIn("UDP 53", notes("dns", 443)[0])

    def test_an_unknown_profile_is_refused(self):
        with self.assertRaises(ValueError):
            signatures.signature_packets("tls", mtu=1280, port=443)


class GenerateSignaturesRouteTests(unittest.TestCase):
    def setUp(self):
        self.app, self.manager = build_app()
        self.http = self.app.test_client()
        self.server = self.manager.create_wireguard_server(
            {"name": "home", "protocol": "AWG 3.1", "subnet": "10.41.0.0/24", "port": 51941, "auto_start": False}
        )

    def generate(self, body, status=200):
        response = self.http.post("/api/generate", json=body)
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def test_answers_i1_to_i5_for_a_client_of_the_server(self):
        before = self.manager.load_config()
        data = self.generate({"server_id": self.server["id"], "signature_profile": "quic"})
        self.assertEqual(set(data["signature_packets"]), set(signatures.SIGNATURE_KEYS))
        self.assertIsNone(data["signature_host"])
        self.assertIn("51941", data["signature_notes"][0])
        self.assertIn("transport_params", data)
        self.assertEqual(self.manager.load_config(), before)  # nothing saved

    def test_the_dns_profile_answers_the_host_it_asked_for(self):
        data = self.generate({"server_id": self.server["id"], "signature_profile": "dns", "host": ""})
        self.assertIn(data["signature_host"], signatures.DEFAULT_HOSTS)
        data = self.generate({"server_id": self.server["id"], "signature_profile": "dns", "host": "ya.ru"})
        self.assertEqual(data["signature_host"], "ya.ru")

    def test_bad_input(self):
        self.generate({"signature_profile": "quic"}, 400)
        self.generate({"server_id": "nope", "signature_profile": "quic"}, 404)
        self.generate({"server_id": self.server["id"], "signature_profile": "tls"}, 400)
        self.generate({"server_id": self.server["id"], "signature_profile": "dns", "host": "bad host"}, 400)
        self.assertNotIn("signature_packets", self.generate({"server_id": self.server["id"]}))


if __name__ == "__main__":
    unittest.main()
