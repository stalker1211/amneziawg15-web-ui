"""Who sent a handshake initiation, and with which parameters: the engine behind the
*Old config* and *Maybe blocked* verdicts (DEVELOPMENT.md §10, 2.8). Pure, no I/O:
it gets datagrams and keys, and says what they show.

AmneziaWG keeps WireGuard's Noise IK handshake unchanged (amneziawg-go
noise-protocol.go): the device's static public key travels AEAD-encrypted under
DH(device ephemeral, server static), so the server's private key alone recovers it.
AWG only wraps the 148-byte message (send.go SendHandshakeInitiation): S1 random
bytes in front, H1 in its type field, random trailers after, and with a
HeaderProtectionKey the whole message XORed with ChaCha20(key, nonce = the
datagram's first 12 bytes, counter 0). So `identify` tries every offset under every
key (and none); where the static key decrypts, the Poly1305 tag is the proof, the
offset is the device's S1, the type field its H1 and the bytes left over its
trailers. A datagram that is not an initiation (an I-packet, Jc junk, data) never
decrypts: a false match is a forged 128-bit tag.

The daemon reads an initiation only when its size and type fit the server's own S1,
H1, trailers and key, and drops any other without an answer or a log line. `mismatch`
names which of those a device's initiations got wrong; `describe` and `server_view`
give both sides in the shape of the API's `diagnosis` (`device`, `server`).

The cost is one X25519 exchange per offset and key, the clear text counting as one:
a 1500-byte datagram under two keys is about 4000, ~90 ms on the owner's Mac (most
datagrams a device sends before its handshake are far shorter). Part 5 times a capture.
"""

import functools
import hashlib
import hmac
import re
from base64 import b64decode, b64encode
from typing import NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

CONSTRUCTION = b"Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s"
IDENTIFIER = b"WireGuard v1 zx2c4 Jason@zx2c4.com"
INITIATION_SIZE = 148
# What identify reads of the message: type 4, sender 4, ephemeral 32, encrypted static
# 32 + its 16-byte tag. The timestamp and MACs after it prove nothing more.
_READ = 88
_NONCE_SIZE = 12  # HeaderCipherNonceSize: the datagram's first 12 bytes
_ZERO_NONCE = bytes(12)

# The parameters an initiation shows, in the order `mismatch` names them.
PARAMETERS = ("S1", "H1", "HeaderProtectionKey", "RandomTrailers")


class Initiation(NamedTuple):
    """One initiation `identify` read: the device's static public key (base64, as a
    client's `public_key` is stored), the offset of the message (the device's S1), its
    type field (the device's H1), the bytes after it (its trailers) and the index in
    `protection_keys` of the key that removed the header protection (None: none did,
    the message was in the clear)."""

    public_key: str
    offset: int
    msg_type: int
    trailer: int
    key_index: int | None


def _hash(*parts):
    digest = hashlib.blake2s(digest_size=32)
    for part in parts:
        digest.update(part)
    return digest.digest()


def _hmac(key, data):
    return hmac.new(key, data, hashlib.blake2s).digest()


def _kdf(key, data, count):
    """WireGuard's KDF1/KDF2: HKDF over HMAC-BLAKE2s."""
    secret = _hmac(key, data)
    out, previous = [], b""
    for index in range(1, count + 1):
        previous = _hmac(secret, previous + bytes([index]))
        out.append(previous)
    return out


@functools.lru_cache(maxsize=16)
def _responder(private_key):
    """The server's side of the handshake up to the device's ephemeral key: its X25519
    key and the chaining key and hash every initiation to it starts from."""
    key = X25519PrivateKey.from_private_bytes(b64decode(private_key, validate=True))
    chaining_key = _hash(CONSTRUCTION)
    start_hash = _hash(_hash(chaining_key, IDENTIFIER), key.public_key().public_bytes_raw())
    return key, chaining_key, start_hash


def _keystream(protection_key, datagram):
    """The first bytes of ChaCha20(key, nonce = datagram[:12], counter 0), as an int to
    XOR with: one per key and datagram, whatever the offset."""
    nonce = bytes(4) + datagram[:_NONCE_SIZE]  # cryptography: counter (LE) || nonce
    encryptor = Cipher(algorithms.ChaCha20(b64decode(protection_key, validate=True), nonce), mode=None).encryptor()
    return int.from_bytes(encryptor.update(bytes(_READ)), "little")


def _static_key(responder, message):
    """The device's static public key if `message` is an initiation to this server, else
    None (Noise IK, noise-protocol.go ConsumeMessageInitiation, up to the static key)."""
    private_key, chaining_key, start_hash = responder
    ephemeral, encrypted_static = message[8:40], message[40:88]
    try:
        shared = private_key.exchange(X25519PublicKey.from_public_bytes(ephemeral))
    except ValueError:  # a low-order point: the shared secret would be all zeros
        return None
    chaining_key = _kdf(chaining_key, ephemeral, 1)[0]
    _, key = _kdf(chaining_key, shared, 2)
    try:
        return ChaCha20Poly1305(key).decrypt(_ZERO_NONCE, encrypted_static, _hash(start_hash, ephemeral))
    except InvalidTag:
        return None


def identify(datagram, server_private_key, protection_keys=()):
    """The `Initiation` in `datagram` (one UDP payload to the server's port), or None
    when it is none to this server under any of `protection_keys` (base64
    HeaderProtectionKeys, the server's current one first if it has one) or without one.

    Every offset from 0 is tried, so a device's S1 is found whatever it is; the first
    message that decrypts is the answer."""
    datagram = bytes(datagram)
    if len(datagram) < INITIATION_SIZE:
        return None
    responder = _responder(server_private_key)
    keystreams = [(index, _keystream(key, datagram)) for index, key in enumerate(protection_keys)]
    keystreams.append((None, 0))
    for offset in range(len(datagram) - INITIATION_SIZE + 1):
        raw = int.from_bytes(datagram[offset : offset + _READ], "little")
        for key_index, keystream in keystreams:
            message = (raw ^ keystream).to_bytes(_READ, "little")
            static = _static_key(responder, message)
            if static is not None:
                return Initiation(
                    public_key=b64encode(static).decode("ascii"),
                    offset=offset,
                    msg_type=int.from_bytes(message[:4], "little"),
                    trailer=len(datagram) - offset - INITIATION_SIZE,
                    key_index=key_index,
                )
    return None


def protection_keys(transport_params, previous_keys=()):
    """The keys `identify` should try for a server: its current HeaderProtectionKey
    first (when it has one), then its previous ones, newest first."""
    current = (transport_params or {}).get("HeaderProtectionKey") or None
    return ([current] if current else []) + [key for key in previous_keys if key and key != current]


def key_label(initiation, transport_params):
    """Which key the device used, as `diagnosis.device.key` says it: `current` (the
    server's own), `previous` (one the server used before) or `none`. Never the key.
    `key_index` counts in `protection_keys(transport_params, ...)`."""
    if initiation.key_index is None:
        return "none"
    if initiation.key_index == 0 and (transport_params or {}).get("HeaderProtectionKey"):
        return "current"
    return "previous"


def _header_range(value):
    """A stored H value ('100000-100999', '100000' or an int) as (low, high)."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(value))
    if not match:
        raise ValueError(f"not a header value or range: {value!r}")
    low = int(match.group(1))
    return low, int(match.group(2)) if match.group(2) is not None else low


def _differs(initiation, transport_params):
    """The parameters this one initiation shows wrong, RandomTrailers only for trailers
    the server does not accept (their absence takes more than one, see `mismatch`)."""
    params = transport_params or {}
    names = set()
    if initiation.offset != (params.get("S1") or 0):
        names.add("S1")
    low, high = _header_range(params.get("H1") or 1)  # unset: WireGuard's own type 1
    if not low <= initiation.msg_type <= high:
        names.add("H1")
    if key_label(initiation, params) != ("current" if params.get("HeaderProtectionKey") else "none"):
        names.add("HeaderProtectionKey")
    if initiation.trailer and not params.get("RandomTrailers"):
        names.add("RandomTrailers")
    return names


def mismatch(initiations, transport_params):
    """What a device's `initiations` (one client's, from `identify`) got wrong against
    its server's `transport_params` (as stored: S1, H1, HeaderProtectionKey,
    RandomTrailers), in `PARAMETERS` order; empty when all of them fit.

    A parameter is named when any initiation shows it wrong. Trailers while the server
    has them off are wrong at once; none while it has them on only over two or more
    initiations, since one random trailer may be 0 bytes long."""
    initiations = list(initiations)
    names = set()
    for initiation in initiations:
        names |= _differs(initiation, transport_params)
    if (
        (transport_params or {}).get("RandomTrailers")
        and len(initiations) >= 2
        and not any(initiation.trailer for initiation in initiations)
    ):
        names.add("RandomTrailers")
    return [name for name in PARAMETERS if name in names]


def matches(initiation, transport_params):
    """Whether this one initiation fits the server in everything it shows: the daemon
    reads it (trailers' absence aside, which `mismatch` judges over several)."""
    return not _differs(initiation, transport_params)


def describe(initiation, transport_params):
    """The device's side of a diagnosis: `{"S1", "H1", "trailers", "key"}`."""
    return {
        "S1": initiation.offset,
        "H1": initiation.msg_type,
        "trailers": initiation.trailer > 0,
        "key": key_label(initiation, transport_params),
    }


def server_view(transport_params):
    """The server's side of a diagnosis: `{"S1", "H1", "trailers", "key"}`, `key` only
    whether it has a HeaderProtectionKey."""
    params = transport_params or {}
    return {
        "S1": params.get("S1") or 0,
        "H1": str(params.get("H1") or 1),
        "trailers": bool(params.get("RandomTrailers")),
        "key": bool(params.get("HeaderProtectionKey")),
    }
