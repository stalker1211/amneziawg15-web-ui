"""Name the device behind AWG datagrams with the server's keys alone: the proof of
concept for 2.8 (DEVELOPMENT.md §10), and the reference for services/initiation.py.

    uv run --no-project --with cryptography python identify.py keys.json capture.jsonl

AmneziaWG keeps WireGuard's Noise IK handshake (amneziawg-go noise-protocol.go): the
device's static public key is AEAD-encrypted under DH(device ephemeral, server
static). AWG only wraps the 148-byte initiation: S1 random bytes in front, H1 in its
type field, with header protection the whole message XORed with
ChaCha20(HeaderProtectionKey, nonce = the datagram's first 12 bytes), and random
trailers after (send.go SendHandshakeInitiation). So: try every offset; where the
static key decrypts (the Poly1305 tag is the proof), the offset is the device's S1.
"""

import hashlib
import hmac
import json
import sys
from base64 import b64decode
from collections import defaultdict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

CONSTRUCTION = b"Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s"
IDENTIFIER = b"WireGuard v1 zx2c4 Jason@zx2c4.com"
INIT_SIZE = 148


def blake(*parts):
    h = hashlib.blake2s(digest_size=32)
    for p in parts:
        h.update(p)
    return h.digest()


def hmac_b(key, data):
    return hmac.new(key, data, hashlib.blake2s).digest()


def kdf(key, data, n):
    t0 = hmac_b(key, data)
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = hmac_b(t0, prev + bytes([i]))
        out.append(prev)
    return out


class Server:
    def __init__(self, priv_b64, hpk_b64):
        self.priv = X25519PrivateKey.from_private_bytes(b64decode(priv_b64))
        pub = self.priv.public_key().public_bytes_raw()
        ck = blake(CONSTRUCTION)
        self.ck0 = ck
        self.h0 = blake(blake(ck, IDENTIFIER), pub)
        self.hpk = b64decode(hpk_b64) if hpk_b64 else None

    def keystream(self, datagram):
        nonce = b"\x00\x00\x00\x00" + datagram[:12]  # counter 0 || 12-byte nonce
        enc = Cipher(algorithms.ChaCha20(self.hpk, nonce), mode=None).encryptor()
        return enc.update(bytes(INIT_SIZE))

    def identify(self, datagram):
        """(offset, type, device public key) or None."""
        ks = self.keystream(datagram) if self.hpk and len(datagram) >= 12 else None
        for off in range(len(datagram) - INIT_SIZE + 1):
            msg = datagram[off : off + INIT_SIZE]
            if ks:
                msg = bytes(a ^ b for a, b in zip(msg, ks))
            eph, enc_static = msg[8:40], msg[40:88]
            h = blake(self.h0, eph)
            ck = kdf(self.ck0, eph, 1)[0]
            try:
                ss = self.priv.exchange(X25519PublicKey.from_public_bytes(eph))
            except ValueError:
                continue
            ck, key = kdf(ck, ss, 2)
            try:
                static = ChaCha20Poly1305(key).decrypt(bytes(12), enc_static, h)
            except InvalidTag:
                continue
            return off, int.from_bytes(msg[:4], "little"), static
        return None


with open(sys.argv[1]) as f:
    keys = json.load(f)
servers = {int(p): Server(s["priv"], s.get("hpk")) for p, s in keys["servers"].items()}
names = {b64decode(v): k for k, v in keys["clients"].items()}
by_src = {v: k for k, v in keys["sources"].items()}
with open(sys.argv[2]) as f:
    records = [json.loads(line) for line in f]

stats = defaultdict(lambda: {"datagrams": 0, "unnamed": 0, "found": defaultdict(int)})
for rec in records:
    d = bytes.fromhex(rec["hex"])
    case = by_src.get(rec["src"], rec["src"])
    st = stats[case]
    st["datagrams"] += 1
    hit = servers[rec["dport"]].identify(d) if len(d) >= INIT_SIZE else None
    if not hit:
        st["unnamed"] += 1
        continue
    off, typ, static = hit
    who = names.get(static, "unknown key " + static.hex()[:12])
    trailer = len(d) - off - INIT_SIZE
    st["found"][f"{who}: S1={off} H1={typ} trailer={'yes' if trailer else 'no'}"] += 1

for case in sorted(stats):
    st = stats[case]
    found = "; ".join(f"{k} (x{v})" for k, v in st["found"].items()) or "nobody"
    print(f"{case}: {st['datagrams']} datagrams, {st['unnamed']} unnamed -> {found}")
