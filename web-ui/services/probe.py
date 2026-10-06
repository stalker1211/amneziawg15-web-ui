"""Why a device cannot connect: the *Old config* and *Maybe blocked* verdicts
(DEVELOPMENT.md §10, 2.8). services/initiation.py names a handshake's sender and its
parameters; this decides when to look, captures, and keeps the verdicts.

Each tick of the traffic monitor brings the servers and the last `awg show all dump`.
Two triggers pick a source worth a capture:

- T1, old config: `conntrack -L -p udp`. The daemon never answers an initiation it
  cannot read, so a device retrying one leaves its flow `[UNREPLIED]`. A tick counts
  only when a packet refreshed the flow since the last reading (it is new, or its
  timeout fell by less than the time elapsed): an unreplied flow stays listed 30 s,
  so a scanner's one datagram is listed for four ticks but refreshed on one. Two
  ticks running trigger. A flow the daemon answered stays `[ASSURED]` for as long as
  its device keeps sending, after a change of parameters too, so a server's start
  deletes its port's flows (`forget_flows`): a device left on the old config is
  unreplied again from its next datagram.
- T2, maybe blocked: a client whose handshake did not move while its rx grew by whole
  initiations (148 bytes each, padding and trailers stripped) up to 2048 bytes, on
  two ticks running. The server reads and answers these; data adds 32 + 16k bytes
  per packet, so a light session does not look like attempts, and a sleeping phone
  grows nothing.

One capture at a time, in a background task: up to 10 s or 64 datagrams from the
source to the server's port, read by a raw socket (copies; the daemon still gets the
originals), named by `initiation.identify`. The tick after it ends judges: per client
named (the key decides, so it may be another than T2 suspected), *Old config* when an
initiation shows a parameter the server does not use, *Maybe blocked* when two or more
fit everything and the handshake still has not moved (neither if it moved since the
trigger: it connected; a suspended client counts as nobody). A source whose capture
named nobody (a stranger, a scanner) rests 5 minutes; a client's source is not
captured again while its verdict holds. A verdict holds until the client's handshake moves
(`client.recovered`) and ends silently at a suspend, a delete or a server stop.
A server whose `connection_analyzer` is off counts as stopped (off by default): no
trigger, no capture, no verdict, and with none on no `conntrack -L` at all.

Every I/O is injected (`run_command`, the capture, `start_background_task`, the GeoIP
lookup, the event callback), so the tests run it inline.
"""

import ctypes
import ipaddress
import re
import socket
import struct
import time

from core.logging_setup import get_logger

from services import initiation

logger = get_logger(__name__)

CONNTRACK = ["conntrack", "-L", "-p", "udp"]
ATTEMPT = initiation.INITIATION_SIZE  # what one read initiation adds to rx
ATTEMPTS_MAX_BYTES = 2048  # more per tick is a session, not attempts
TICKS = 2  # ticks running a trigger needs
CAPTURE_SECONDS = 10
CAPTURE_LIMIT = 64
REST_SECONDS = 300

_FIELD = re.compile(r"(src|dst|sport|dport)=(\S+)")
_ENDPOINT = re.compile(r"\[([^\]]+)\]:(\d+)|([^:]+):(\d+)")


def parse_conntrack(text):
    """`conntrack -L -p udp` as a list of {src, sport, dport, timeout, unreplied}, in the
    original direction (the first src/dst/sport/dport of a line: from the device).
    Lines it cannot read are skipped."""
    flows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "udp" or not parts[2].isdigit():
            continue
        fields = {}
        for name, value in _FIELD.findall(line):
            fields.setdefault(name, value)
        try:
            flows.append(
                {
                    "src": fields["src"],
                    "sport": int(fields["sport"]),
                    "dport": int(fields["dport"]),
                    "timeout": int(parts[2]),
                    "unreplied": "[UNREPLIED]" in parts,
                }
            )
        except (KeyError, ValueError):
            logger.debug("Skipping an unreadable conntrack line")
    return flows


SO_ATTACH_FILTER = 26  # Linux; not in Python's socket module


def bpf_program(family, source, sport, port):
    """A classic BPF program, as (code, jt, jf, k) rows, that keeps only UDP from
    `source`:`sport` to `port`. The kernel runs it on the raw socket, so a large transfer
    through the server costs no Python and fills no buffer (measured: in Python, a 10 s
    capture beside a 4 Gbit/s transfer took 3.5 s of CPU and lost half the device's
    datagrams). An IPv4 packet starts at its IP header (the address, then the UDP header
    after IHL; a later fragment has no UDP header), an IPv6 one at the UDP header."""
    keep, drop = (0x06, 0, 0, 0x40000), (0x06, 0, 0, 0)
    if family == socket.AF_INET6:
        return [
            (0x28, 0, 0, 0),  # ldh [0]: the source port
            (0x15, 0, 2, sport),
            (0x28, 0, 0, 2),  # ldh [2]: the destination port
            (0x15, 1, 0, port),
            drop,
            keep,
        ]
    return [
        (0x20, 0, 0, 12),  # ld [12]: the source address
        (0x15, 0, 7, int.from_bytes(source.packed, "big")),
        (0x28, 0, 0, 6),  # ldh [6]: flags and fragment offset
        (0x45, 5, 0, 0x1FFF),
        (0xB1, 0, 0, 0),  # ldxb 4*([0]&0xf): the IP header's length
        (0x48, 0, 0, 0),  # ldh [x+0]: the source port
        (0x15, 0, 2, sport),
        (0x48, 0, 0, 2),  # ldh [x+2]: the destination port
        (0x15, 1, 0, port),
        drop,
        keep,
    ]


def attach_filter(raw, program):
    """SO_ATTACH_FILTER: a struct sock_fprog pointing at the program; the kernel copies
    it, so the buffer only has to outlive the call."""
    rows = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *row) for row in program))
    raw.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, struct.pack("HL", len(program), ctypes.addressof(rows)))


def capture(port, src, sport, seconds, limit):
    """The UDP payloads from `src`:`sport` to `port` that arrive within `seconds`, at
    most `limit`, read by a raw socket. The kernel filters (`bpf_program`); Python
    checks again, for what was queued before the filter was attached. Raises
    PermissionError without CAP_NET_RAW. IPv4 packets come with their IP header, IPv6
    ones without."""
    family = socket.AF_INET6 if ":" in src else socket.AF_INET
    source = ipaddress.ip_address(src)
    payloads, deadline = [], time.monotonic() + seconds
    with socket.socket(family, socket.SOCK_RAW, socket.IPPROTO_UDP) as raw:
        attach_filter(raw, bpf_program(family, source, sport, port))
        while len(payloads) < limit:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            raw.settimeout(left)
            try:
                packet, address = raw.recvfrom(65535)
            except TimeoutError:
                break
            if ipaddress.ip_address(address[0].split("%")[0]) != source:
                continue
            udp = packet[(packet[0] & 0x0F) * 4 :] if family == socket.AF_INET else packet
            if len(udp) < 8 or int.from_bytes(udp[0:2], "big") != sport or int.from_bytes(udp[2:4], "big") != port:
                continue
            payloads.append(udp[8:])
    return payloads


def format_endpoint(src, sport):
    return f"[{src}]:{sport}" if ":" in src else f"{src}:{sport}"


class Probe:
    """Per running server: the T1 flows and the T2 clients being watched, the capture
    in flight, the sources resting, and the verdicts. `on_event(event, server, client,
    detail)` records `client.old_config`, `client.maybe_blocked` and
    `client.recovered`; `lookup_geoip(ip)` gives (label, country code)."""

    def __init__(self, *, run_command, start_background_task, lookup_geoip, on_event, capture=capture):
        self.run_command = run_command
        self.start_background_task = start_background_task
        self.lookup_geoip = lookup_geoip
        self.on_event = on_event
        self.capture = capture
        self.enabled = True  # off for good without a raw socket
        self.conntrack = True  # T1 off for good without conntrack
        self._flows = {}  # (port, src, sport) -> {since, at, timeout, ticks, seen_at}
        self._clients = {}  # client id -> {handshake_at, rx, ticks, since, rx_since, grew_at}
        self._resting = {}  # (port, src, sport) -> until
        self._job = None  # the capture in flight, or done and waiting for the next tick
        self._verdicts = {}  # client id -> {server_id, handshake_at, source, rx_base, diagnosis}
        # client id -> the last handshake seen: a server's restart (every change of its
        # parameters) zeroes its daemon's, and its device did connect before.
        self._handshakes = {}

    def forget_flows(self, port):
        """A server (re)started on `port`: conntrack forgets the flows to it, which the
        daemon may have answered with the parameters it had before. Listed first, since
        `conntrack -D` fails when nothing matches."""
        if not self.conntrack:
            return
        flows = parse_conntrack(self.run_command([*CONNTRACK, "--orig-port-dst", str(port)]))
        if any(flow["dport"] == port for flow in flows):
            self.run_command(["conntrack", "-D", "-p", "udp", "--orig-port-dst", str(port)])

    def diagnosis(self, client_id):
        """The client's verdict in the API's shape (DEVELOPMENT.md §10, 2.8,
        *Contract*), or None."""
        verdict = self._verdicts.get(client_id)
        return dict(verdict["diagnosis"]) if verdict else None

    def tick(self, at, servers, snapshot):
        """One monitor tick: `servers` as stored, `snapshot` the manager's last
        telemetry ({at, interfaces, read}). A failed read is not a tick."""
        if not self.enabled or not snapshot.get("read"):
            return
        interfaces = snapshot.get("interfaces") or {}
        live = [server for server in servers if server.get("interface") in interfaces]
        known = {client.get("id") for server in servers for client in server.get("clients", [])}
        self._handshakes = {cid: value for cid, value in self._handshakes.items() if cid in known}
        for server in live:
            peers = interfaces[server["interface"]]
            for client in server.get("clients", []):
                handshake_at = (peers.get(client.get("client_public_key")) or {}).get("handshake_at")
                if handshake_at:
                    self._handshakes[client.get("id")] = handshake_at

        # A server with its Connection analyzer off counts as stopped; only the last
        # handshakes above are kept for it, for its dialog once it is switched on.
        running = {server["id"]: server for server in live if server.get("connection_analyzer")}
        clients = {}  # client id -> (server, client, its dump entry or {})
        for server in running.values():
            peers = interfaces[server["interface"]]
            for client in server.get("clients", []):
                clients[client.get("id")] = (server, client, peers.get(client.get("client_public_key")) or {})
        self._end_verdicts(at, clients)
        refreshed = self._watch_flows(at, running)
        grew = self._watch_clients(at, clients)
        if self._job is not None and self._job["done"]:
            job, self._job = self._job, None
            self._judge(at, job, running, clients)
        self._follow_verdicts(at, clients, refreshed, grew)
        self._resting = {source: until for source, until in self._resting.items() if until > at}
        if self._job is None:
            self._start_capture(running, clients)

    # --- triggers -----------------------------------------------------------------

    def _watch_flows(self, at, running):
        """T1: the unreplied flows to a running server's port, each tick refreshed or
        not. Returns the sources refreshed this tick."""
        ports = {server.get("port") for server in running.values()}
        if not self.conntrack or not ports:
            self._flows = {}
            return set()
        output = self.run_command(CONNTRACK)
        if output is None:
            logger.warning("conntrack is unavailable: the panel cannot see handshakes left unanswered (Old config)")
            self.conntrack = False
            self._flows = {}
            return set()
        flows, refreshed = {}, set()
        for flow in parse_conntrack(output):
            if not flow["unreplied"] or flow["dport"] not in ports:
                continue
            source = (flow["dport"], flow["src"], flow["sport"])
            was = self._flows.get(source)
            if was is None:
                state = {"since": at, "ticks": 1, "seen_at": at}
            # Without a packet the timeout drops by the time elapsed (±1 for rounding).
            elif flow["timeout"] > was["timeout"] - (at - was["at"]) + 1:
                state = {"since": was["since"], "ticks": was["ticks"] + 1, "seen_at": at}
            else:
                state = {"since": was["since"], "ticks": 0, "seen_at": was["seen_at"]}
            flows[source] = {**state, "at": at, "timeout": flow["timeout"]}
            if state["seen_at"] == at:
                refreshed.add(source)
        self._flows = flows
        return refreshed

    def _watch_clients(self, at, clients):
        """T2: each non-suspended client's handshake and rx. Returns the clients whose
        rx grew this tick while their handshake did not move."""
        watched, grew = {}, set()
        for client_id, (_server, client, info) in clients.items():
            rx = info.get("rx")
            if client.get("suspended") or rx is None:
                continue
            was = self._clients.get(client_id)
            handshake_at = info.get("handshake_at")
            growth = 0
            if was is not None and was["handshake_at"] == handshake_at and rx >= was["rx"]:
                growth = rx - was["rx"]
            if growth:
                grew.add(client_id)
            if was is not None and 0 < growth <= ATTEMPTS_MAX_BYTES and growth % ATTEMPT == 0:
                streak = was["ticks"] > 0
                state = {
                    "ticks": was["ticks"] + 1,
                    "since": was["since"] if streak else at,
                    "rx_since": was["rx_since"] if streak else was["rx"],
                    "grew_at": at,
                }
            else:
                state = {"ticks": 0, "since": None, "rx_since": None, "grew_at": was and was["grew_at"]}
            watched[client_id] = {"handshake_at": handshake_at, "rx": rx, **state}
        self._clients = watched
        return grew

    # --- the capture --------------------------------------------------------------

    def _start_capture(self, running, clients):
        """The first trigger due, captured in a background task: T1 flows, then T2
        clients. Its trigger starts over, so a capture that names a client without
        a verdict is repeated only after two more ticks."""
        held = {verdict["source"] for verdict in self._verdicts.values()}
        servers_by_port = {server.get("port"): server for server in running.values()}
        for source, flow in self._flows.items():
            if flow["ticks"] >= TICKS and source not in held and source not in self._resting:
                flow["ticks"] = 0
                self._capture(servers_by_port[source[0]], source, flow["since"], clients)
                return
        for client_id, state in self._clients.items():
            if state["ticks"] < TICKS or client_id in self._verdicts:
                continue
            server, _client, info = clients[client_id]
            match = _ENDPOINT.fullmatch(info.get("endpoint") or "")
            if not match:
                continue
            source = (server.get("port"), match.group(1) or match.group(3), int(match.group(2) or match.group(4)))
            if source in self._resting:
                continue
            self._capture(server, source, state["since"], clients, {client_id: state["rx_since"]})
            state.update(ticks=0, since=None, rx_since=None)
            return

    def _capture(self, server, source, since, clients, rx_since=None):
        """Capture `source` to `server` in the background, name what it sent, and leave
        the result for the next tick. Everything the judgement compares against is
        taken now: the server's parameters and keys, its clients' handshakes, and
        their rx (`rx_since` for T2's client: from before its growth)."""
        port, src, sport = source
        params = dict(server.get("transport_params") or {})
        keys = initiation.protection_keys(params, server.get("previous_header_protection_keys") or ())
        private_key = server.get("server_private_key")
        job = {
            "server_id": server["id"],
            "source": source,
            "since": since,
            "params": params,
            "rx_base": {cid: entry[2].get("rx") for cid, entry in clients.items() if entry[0] is server} | (rx_since or {}),
            "handshakes": {cid: entry[2].get("handshake_at") for cid, entry in clients.items() if entry[0] is server},
            "initiations": [],
            "country": None,
            "done": False,
        }
        self._job = job

        def run():
            try:
                datagrams = self.capture(port, src, sport, CAPTURE_SECONDS, CAPTURE_LIMIT)
                found = [initiation.identify(datagram, private_key, keys) for datagram in datagrams]
                job["initiations"] = [one for one in found if one is not None]
                if job["initiations"]:
                    job["country"] = self.lookup_geoip(src)[1]
                logger.debug(
                    "Captured %d datagrams from %s: %d initiations",
                    len(datagrams),
                    format_endpoint(src, sport),
                    len(job["initiations"]),
                )
            except PermissionError as e:
                logger.warning("No raw socket (%s): the panel cannot capture handshakes (Old config, Maybe blocked)", e)
                self.enabled = False
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.warning("Capturing %s failed: %s", format_endpoint(src, sport), e)
            finally:
                job["done"] = True  # last: the tick reads the rest only after this

        self.start_background_task(run)

    # --- verdicts -----------------------------------------------------------------

    def _judge(self, at, job, running, clients):
        """The verdicts a finished capture gives, per non-suspended client it names."""
        server = running.get(job["server_id"])
        if server is None:  # stopped meanwhile
            return
        by_key = {client.get("client_public_key"): client for client in server.get("clients", [])}
        named = {}
        for one in job["initiations"]:
            client = by_key.get(one.public_key)
            if client is not None and not client.get("suspended") and client.get("id") in clients:
                named.setdefault(client.get("id"), []).append(one)
        if not named:
            self._resting[job["source"]] = at + REST_SECONDS
            return
        params = job["params"]
        for client_id, initiations in named.items():
            server, client, info = clients[client_id]
            held = self._verdicts.get(client_id)
            if held is not None:  # the same device from a new port
                held["source"] = job["source"]
                held["diagnosis"]["endpoint"] = format_endpoint(*job["source"][1:])
                held["diagnosis"]["last_attempt"] = int(at)
                continue
            handshake_at = info.get("handshake_at")
            if handshake_at != job["handshakes"].get(client_id):
                continue  # it connected meanwhile
            names = initiation.mismatch(initiations, params)
            if names:
                verdict, attempts = "old_config", len(initiations)
            elif sum(initiation.matches(one, params) for one in initiations) >= 2:
                verdict, attempts = "maybe_blocked", None
            else:
                continue
            rx_base = job["rx_base"].get(client_id) or 0
            if attempts is None:
                attempts = max(0, (info.get("rx") or 0) - rx_base) // ATTEMPT
            changed_at = server.get("transport_changed_at")
            last_handshake = handshake_at or self._handshakes.get(client_id)
            device = initiation.describe(initiations[0], params)
            device["trailers"] = any(one.trailer for one in initiations)  # one may be 0 bytes
            diagnosis = {
                "verdict": verdict,
                "since": int(job["since"]),
                "last_attempt": self._last_attempt(at, client_id, job["source"]),
                "attempts": attempts,
                "endpoint": format_endpoint(*job["source"][1:]),
                "country": job["country"],
                "device": device,
                "server": initiation.server_view(params),
                "mismatch": names,
                "last_handshake": last_handshake,
                "params_changed_at": changed_at if changed_at and changed_at > (last_handshake or 0) else None,
            }
            self._verdicts[client_id] = {
                "server_id": server["id"],
                "handshake_at": handshake_at,
                "source": job["source"],
                "rx_base": rx_base,
                "diagnosis": diagnosis,
            }
            detail = {key: value for key, value in diagnosis.items() if key != "since"}
            self.on_event(f"client.{verdict}", server, client, detail)

    def _last_attempt(self, at, client_id, source):
        """The last tick the trigger held: the flow refreshed or the rx grew."""
        flow, state = self._flows.get(source) or {}, self._clients.get(client_id) or {}
        seen = [value for value in (flow.get("seen_at"), state.get("grew_at")) if value]
        return int(max(seen) if seen else at)

    def _end_verdicts(self, at, clients):
        """A moved handshake recovers a verdict; a suspend, a delete or a server stop
        ends it silently."""
        for client_id, verdict in list(self._verdicts.items()):
            entry = clients.get(client_id)
            if entry is None or entry[1].get("suspended") or entry[0]["id"] != verdict["server_id"]:
                del self._verdicts[client_id]
                continue
            server, client, info = entry
            handshake_at = info.get("handshake_at")
            if handshake_at is not None and handshake_at != verdict["handshake_at"]:
                del self._verdicts[client_id]
                diagnosis = verdict["diagnosis"]
                detail = {"verdict": diagnosis["verdict"], "duration_s": int(at - diagnosis["since"])}
                self.on_event("client.recovered", server, client, detail)

    def _follow_verdicts(self, at, clients, refreshed, grew):
        """While a verdict holds, its device still trying moves `last_attempt` (its
        flow refreshed, or its rx grew) and, for maybe blocked, `attempts`."""
        for client_id, verdict in self._verdicts.items():
            diagnosis = verdict["diagnosis"]
            if verdict["source"] in refreshed or client_id in grew:
                diagnosis["last_attempt"] = int(at)
            rx = clients[client_id][2].get("rx")
            if diagnosis["verdict"] == "maybe_blocked" and rx is not None:
                if rx < verdict["rx_base"]:  # the counter started over
                    verdict["rx_base"] = 0
                diagnosis["attempts"] = (rx - verdict["rx_base"]) // ATTEMPT
