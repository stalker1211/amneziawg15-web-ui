"""Signature packets I1-I5 shaped like a protocol: Generate in the client drawer.

amneziawg-go sends I1-I5 before every handshake initiation, then the Jc junk packets,
then the initiation (device/send.go). Each is built from tags (device/obf*.go): the
bytes of `<b 0x...>` are fixed when the config is written, while `<r n>` is n random
bytes the daemon draws again on every send. Hence the rule here: a protocol's fixed
fields go in `<b>`, and everything a real client draws per connection (connection
ids, a packet number, a transaction id, encrypted payload) goes in `<r>`. A value in
`<b>` is sent unchanged before every handshake, about every 2 minutes all day: a
fingerprint of that one client.

Written from the RFCs (QUIC: RFC 9000; DNS: RFC 1035, 6891, 7873), not ported.
Architect (DEVELOPMENT.md §4) prompted it; its QUIC Initials are below RFC 9000's
1200-byte floor by default, and it writes the connection ids into `<b>`.

The disguise is the packets' shape. Anyone can decrypt a real QUIC Initial (its keys
derive from its DCID and a published salt); these carry random bytes, so a DPI that
decrypts Initials sees a broken one.
"""

import re
import secrets

SIGNATURE_KEYS = ("I1", "I2", "I3", "I4", "I5")

# What the drawer offers (page_config renders it into the page). `port` is where the
# real protocol runs, for the hint; `host` whether the profile asks for a host name.
PROFILES = (
    {"id": "quic", "label": "QUIC Initial", "port": 443, "host": False},
    {"id": "dns", "label": "DNS query", "port": 53, "host": True},
    {"id": "random", "label": "Random", "port": None, "host": False},
)

# A client pads the datagram carrying its first Initial to at least 1200 bytes, and
# servers drop a shorter one (RFC 9000 §14.1). These are the sizes browsers were
# measured sending (Architect's fingerprint table); one is drawn per client.
QUIC_INITIAL_SIZES = (1200, 1232, 1250, 1252)
QUIC_VERSION_1 = "00000001"
# A client's first DCID is at least 8 bytes (§7.2); connection ids cap at 20 (§17.2).
QUIC_DCID = (8, 20)
QUIC_SCID = (0, 20)
# Short-header packets after the Initials: ACK-sized to a small request.
QUIC_SHORT = (40, 400)

# Names with both an A and an AAAA record (checked 2026-10-01): I2 asks for AAAA,
# and a resolver asks both of a name that has both.
DEFAULT_HOSTS = (
    "www.google.com",
    "www.youtube.com",
    "cloudflare.com",
    "www.wikipedia.org",
    "www.microsoft.com",
    "www.apple.com",
    "ya.ru",
    "yandex.ru",
)
DNS_TYPE_A = 1
DNS_TYPE_AAAA = 28
# The UDP payload size an EDNS0 query advertises (the DNS flag day value).
EDNS_UDP_SIZE = 1232
DNS_COOKIE_OPTION = 10
DNS_CLIENT_COOKIE = 8

HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def rnd(low, high):
    """Inclusive random integer from the OS's secure source."""
    return low + secrets.randbelow(high - low + 1)


def quic_varint(value):
    """RFC 9000 §16, the two-byte form (the Length of a packet up to 16383 bytes)."""
    if not 0 <= value < 0x4000:
        raise ValueError(f"{value} does not fit a two-byte varint")
    return f"{0x4000 | value:04x}"


def quic_initial(dcid_len, scid_len, size):
    """A client Initial (RFC 9000 §17.2.2) of `size` bytes, its random fields as <r>.

    First byte: long header, fixed bit, type Initial (00), then the reserved bits and
    the packet number length, which header protection masks on the wire, so any
    value is plausible. No token: a fresh connection has none. Length is a varint
    covering the packet number and the payload, i.e. everything after it.
    """
    pn_len = rnd(1, 4)
    header = 1 + 4 + 1 + dcid_len + 1 + scid_len + 1 + 2
    rest = size - header
    tags = f"<b 0x{0xC0 | (pn_len - 1):02x}{QUIC_VERSION_1}{dcid_len:02x}><r {dcid_len}><b 0x{scid_len:02x}>"
    if scid_len:
        tags += f"<r {scid_len}>"
    return tags + f"<b 0x00{quic_varint(rest)}><r {rest}>"


def quic_short(size):
    """A short-header (1-RTT) packet: 0b01, then five bits header protection masks,
    then the DCID and the payload, which look random on the wire."""
    return f"<b 0x{0x40 | rnd(0, 0x3F):02x}><r {size - 1}>"


def clean_host(host):
    """A domain name for the DNS profile, lower-cased; one of DEFAULT_HOSTS when empty."""
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return secrets.choice(DEFAULT_HOSTS)
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or labels[-1].isdigit() or not all(HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError(f"Host must be a domain name such as www.example.com, got {host!r}")
    return host


def dns_name(host):
    """The QNAME: each label behind its length byte, then the root (RFC 1035 §3.1)."""
    return "".join(f"{len(label):02x}{label.encode('ascii').hex()}" for label in host.split(".")) + "00"


def dns_query(host, qtype):
    """A recursive query for one name (RFC 1035 §4.1) with an EDNS0 OPT record carrying
    a client cookie (RFC 6891, RFC 7873), as dig and unbound send them.

    The transaction id and the cookie are <r>: a real stub draws the id per query.
    Header: flags 0x0100 (a standard query, recursion desired), one question, one
    additional record. OPT: the root name, type 41, the UDP size as its class, a zero
    TTL (no extended rcode, version 0, DO off), then the COOKIE option.
    """
    header = "0100" + "0001" + "0000" + "0000" + "0001"
    question = dns_name(host) + f"{qtype:04x}" + "0001"
    option = f"{DNS_COOKIE_OPTION:04x}{DNS_CLIENT_COOKIE:04x}"
    opt = "00" + "0029" + f"{EDNS_UDP_SIZE:04x}" + "00000000" + f"{len(option) // 2 + DNS_CLIENT_COOKIE:04x}" + option
    return f"<r 2><b 0x{header}{question}{opt}><r {DNS_CLIENT_COOKIE}>"


def random_packet():
    """A mix of tags with no <b>: nothing in it repeats from one handshake to the next."""
    parts = [f"<r {rnd(8, 64) if rnd(1, 10) <= 7 else rnd(100, 400)}>"]
    if rnd(0, 1):
        parts.append("<t>")
    if rnd(0, 1):
        parts.append(f"<rc {rnd(4, 12)}>")
    if rnd(0, 1):
        parts.append(f"<rd {rnd(4, 8)}>")
    secrets.SystemRandom().shuffle(parts)
    return "".join(parts)


def profile_spec(profile):
    spec = next((p for p in PROFILES if p["id"] == profile), None)
    if spec is None:
        known = ", ".join(p["id"] for p in PROFILES)
        raise ValueError(f"Unknown signature profile {profile!r} (known: {known})")
    return spec


def signature_packets(profile, *, mtu, port, host=None):
    """I1-I5 for a profile -> {packets, host, notes}. Raises ValueError for an unknown
    profile or a bad host. `host` is the name the DNS profile asked for (None for the
    others); `notes` holds the port hint for the drawer."""
    spec = profile_spec(profile)
    used_host = None
    if profile == "quic":
        dcid, scid = rnd(*QUIC_DCID), rnd(*QUIC_SCID)
        size = min(secrets.choice(QUIC_INITIAL_SIZES), mtu)
        packets = [quic_initial(dcid, scid, size), quic_initial(dcid, scid, size)]
        packets += [quic_short(rnd(*QUIC_SHORT)) for _ in range(3)]
    elif profile == "dns":
        used_host = clean_host(host)
        packets = [dns_query(used_host, DNS_TYPE_A), dns_query(used_host, DNS_TYPE_AAAA), "", "", ""]
    else:
        packets = [random_packet() for _ in SIGNATURE_KEYS]

    notes = []
    if spec["port"] and int(port) != spec["port"]:
        notes.append(
            f"{spec['label']}: the real protocol goes to UDP {spec['port']}, and this server "
            f"listens on {port}, an unusual port for it."
        )
    return {"packets": dict(zip(SIGNATURE_KEYS, packets, strict=True)), "host": used_host, "notes": notes}
