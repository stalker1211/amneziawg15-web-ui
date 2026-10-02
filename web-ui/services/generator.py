"""Random AmneziaWG parameters for a new server or client: POST /api/generate.

The draws are ported from AmneziaWG Architect's generator (Any-Tech-ARCHITECT,
src/engines/awg/generator: strategy.ts, awg3.ts, index.ts), keeping the traps its
comments record. Its parameter catalogue is not ported: its RandomTrailers scope and
S4 cap contradict the daemon's source (DEVELOPMENT.md §4). Header windows are 1k-50k
wide, as in every Architect mode. Width is not free: with RandomTrailers on, a data
packet is tried as each handshake type first, and a range can claim it (amneziawg-go
#186). So with trailers on, S1-S4 are drawn equal, which closes that.

Everything drawn here passes the panel's validators without a warning; a test draws
many sets through them.

Ported code: Copyright (c) 2026 Vadim Khristenko

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import secrets

# H1-H4 stay under 2^31-1: some clients (older Windows builds, per Architect's client
# matrix) take a signed int32.
H_CAP = 2**31 - 1
# Widest window a header range opens, and its narrowest.
RANGE_MIN_WIDTH = 1_000
RANGE_MAX_WIDTH = 50_000
ZONE_GAP = 10_000

# Padding sizes. S1-S3 stay inside the 15-150 the validators call common; S3 pads
# cookie replies, which are rare, and S4 every data packet, so both stay small.
S_MIN, S_MAX = 15, 150
S3_MAX = 64
S4_MAX = 32
# amneziawg-go reads the header cipher's 12-byte nonce out of each S padding.
HEADER_CIPHER_NONCE_SIZE = 12

# Message sizes (device/noise-protocol.go): initiation 148, response 92, cookie 64.
# Two message types padded to one length are one signal, so these differences are
# the lengths to avoid.
INIT_TO_RESPONSE = 148 - 92  # S2 = S1 + 56
INIT_TO_COOKIE = 148 - 64  # S3 = S1 + 84
RESPONSE_TO_COOKIE = 92 - 64  # S3 = S2 + 28

# Junk before every handshake -- about every 2 min with keepalive 25, so an always-on
# phone sends it all day. Upstream recommends Jc 4-12; sizes stay far below any MTU.
JC_RANGE = (4, 12)
JMIN_RANGE = (8, 40)
JMAX_MAX = 160
JMAX_ROOM = 64  # Jmax leaves Jmin this much to vary under it


def rnd(low, high):
    """Inclusive random integer from the OS's secure source."""
    return low + secrets.randbelow(high - low + 1)


def uint_range(low, high):
    """'a' or 'a-b', the way amneziawg-go's UintRange parses it back."""
    return str(low) if low == high else f"{low}-{high}"


def header_zones(cap=H_CAP):
    """Four disjoint zones for H1-H4 under the cap, and how far a start may wander.

    Five zones, so the fourth has room above it. The layout scales with the cap
    rather than being clamped to it: clamping put every range's end on the cap.
    """
    zone = cap // 5

    def spread(want):
        return min(want, zone - ZONE_GAP)

    return {
        "H1": (zone, zone * 2 - ZONE_GAP, spread(100_000_000)),
        "H2": (zone * 2, zone * 3 - ZONE_GAP, spread(100_000_000)),
        "H3": (zone * 3, zone * 4 - ZONE_GAP, spread(100_000_000)),
        "H4": (zone * 4, cap, spread(150_000_000)),
    }


def header_range(key, cap=H_CAP):
    """A 'start-end' window inside the key's own zone.

    The base leaves room for the spread and the window above it. Drawn across the
    whole zone, the range ran into the next one about once in forty thousand draws,
    and the last zone's range ended exactly on the cap in one draw of six --
    2147483647 is not a random number, it is a signature. At the cap the window
    slides down rather than collapsing onto it.
    """
    low, high, spread = header_zones(cap)[key]
    # One below, so even the largest draw of all three ends short of the zone's top.
    top = max(low, high - (spread + RANGE_MAX_WIDTH) - 1)
    width = rnd(RANGE_MIN_WIDTH, RANGE_MAX_WIDTH)
    start = rnd(low, top) + rnd(0, spread)
    start = min(start, max(0, cap - width))
    return f"{start}-{min(start + width, cap)}"


def header_single(key, cap=H_CAP):
    """One value out of the key's zone, for AWG 1.5 (no ranges).

    Clamping constants to a cap once put H2-H4 all on it: three identical headers,
    the same for every user.
    """
    low, _high, spread = header_zones(cap)[key]
    if key == "H1":
        spread = 4_000_000
    return min(low + rnd(0, spread), cap)


def avoid_collision(value, ceiling, collides):
    """Step a size off a colliding length without leaving its range.

    Stepping up at the ceiling went over it (S2 151 out of a 1-150 draw), so at the
    ceiling the step goes down; any neighbour misses the one colliding length.
    """
    if not collides(value):
        return value
    step = -1 if value >= ceiling else 1
    value += step
    for _ in range(10):
        if not collides(value):
            break
        value += step
    return value


def lift_above_floor(value, floor, high):
    """Redraw a size under the floor from the floor up, rather than clamping to it.

    Clamping turned most S4 draws into exactly 12, and S3 = S4 = 12 on every server
    is a signature.
    """
    if value >= floor:
        return value
    return rnd(floor, high) if high > floor else floor


def padding_sizes(*, with_s34, header_protection, mtu, equal=False):
    """S1-S4 with none of the three equal-length cases, and the nonce floor if needed.

    equal (AWG 3.1 with RandomTrailers): one size for all four. amneziawg-go then reads
    every handshake branch's header window at a data packet's own type field, an H4
    value that disjoint ranges keep out of H1-H3, so no data packet is taken for a
    handshake and dropped (amneziawg-go#186). 15-32: S4 pads every data packet, and
    the trailers already vary the handshake lengths.
    """
    if equal and with_s34:
        size = rnd(S_MIN, S4_MAX)
        return {"S1": size, "S2": size, "S3": size, "S4": size}
    s_max = min(S_MAX, mtu - 148)
    s1, s2 = rnd(S_MIN, s_max), rnd(S_MIN, s_max)
    s2 = avoid_collision(s2, s_max, lambda v: v == s1 + INIT_TO_RESPONSE)
    sizes = {"S1": s1, "S2": s2}
    if not with_s34:
        return sizes

    s3 = rnd(S_MIN, S3_MAX)
    s4 = rnd(1, S4_MAX)
    if header_protection:
        s4 = lift_above_floor(s4, HEADER_CIPHER_NONCE_SIZE, S4_MAX)
    s3 = avoid_collision(s3, S3_MAX, lambda v: v in (s1 + INIT_TO_COOKIE, s2 + RESPONSE_TO_COOKIE))
    return {**sizes, "S3": s3, "S4": s4}


def transport_params(*, with_s34, header_ranges, awg3, mtu, header_protection_key=None, equal_padding=False):
    """Server-side parameters: S1-S4, H1-H4 and, on AWG 3.x, a HeaderProtectionKey."""
    # S values are ints; H ranges and the key are strings.
    sizes = padding_sizes(with_s34=with_s34, header_protection=awg3, mtu=mtu, equal=equal_padding)
    params: dict[str, int | str] = {**sizes}
    for key in ("H1", "H2", "H3", "H4"):
        params[key] = header_range(key) if header_ranges else str(header_single(key))
    if awg3 and header_protection_key:
        params["HeaderProtectionKey"] = header_protection_key
    return params


def timings():
    """The five AWG 3.x timers as ranges that keep timers.go's invariants with a margin.

    keyRefreshTimeoutReceiving = RejectAfterTime.pick - KeepaliveTimeout.Lo -
    RekeyTimeout.Lo, clamped at 0, and at 0 every received packet counts as due for a
    rekey; and RekeyAfterTime must end before RejectAfterTime starts, or the session
    is rejected before it ever rekeys.
    """
    jitter = 25
    rekey_timeout_lo = rnd(4, 6)
    rekey_timeout_hi = rekey_timeout_lo + rnd(1, 4)
    keepalive_lo = rnd(8, 14)
    keepalive_hi = keepalive_lo + rnd(2, 8)
    rekey_after_lo = rnd(100, 120)
    rekey_after_hi = rekey_after_lo + rnd(10, jitter)
    reject_lo = max(170, rekey_after_hi + keepalive_hi + rekey_timeout_hi + 15)
    reject_hi = reject_lo + rnd(10, jitter)
    attempts_lo = rnd(12, 18)
    attempts_hi = attempts_lo + rnd(2, 10)
    return {
        "RekeyAfterTime": uint_range(rekey_after_lo, rekey_after_hi),
        "RekeyTimeout": uint_range(rekey_timeout_lo, rekey_timeout_hi),
        "RejectAfterTime": uint_range(reject_lo, reject_hi),
        "KeepaliveTimeout": uint_range(keepalive_lo, keepalive_hi),
        "MaxHandshakeAttempts": uint_range(attempts_lo, attempts_hi),
    }


def content_padding():
    """ContentPaddingAddition: a moderate range within 16-128 bytes per data packet."""
    low = rnd(16, 72)
    return uint_range(low, rnd(low + 8, 128))


def client_defaults(*, awg3):
    """Client-side parameters: the junk train and, on AWG 3.x, padding and timers."""
    jmin = rnd(*JMIN_RANGE)
    params = {
        "Jc": rnd(*JC_RANGE),
        "Jmin": jmin,
        "Jmax": rnd(jmin + JMAX_ROOM, JMAX_MAX),
        **{key: "" for key in ("I1", "I2", "I3", "I4", "I5")},
    }
    if awg3:
        params["ContentPaddingAddition"] = content_padding()
        params.update(timings())
    return params
