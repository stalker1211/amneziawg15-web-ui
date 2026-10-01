"""Traffic history: what each running server's clients moved, kept in memory.

The 7 s traffic loop reads every peer's byte counters anyway (`awg show all dump`);
`record` turns each read into per-client deltas, so the page can draw the last hour
at every tick and the last 24 h per minute without sampling anything itself. Memory
only, like the telemetry snapshot: the monitor never writes web_config.json, and a
restart of the panel starts the history over (`since`).

Directions are the daemon's: `received` is what the server got from a device (its
upload) and `sent` what the server sent it (its download). Only the page translates.
"""

import threading
import time
from collections import deque
from typing import ClassVar


class TrafficHistory:
    """Per server: the last hour's ticks and 24 h of per-minute sums. The traffic loop
    writes (`record`), request threads read (`rates`, `series`); one lock covers both."""

    TICK_SECONDS = 7  # the traffic loop's period; the first tick's interval
    FINE_SECONDS = 3600  # every tick is kept this long
    MINUTES = 24 * 60  # per-minute sums are kept this many minutes
    RANGES: ClassVar[dict] = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}
    # A client's state at a tick: online (a recent handshake), suspended, offline.
    # In a series, " " is a tick or minute with no data for it.
    ONLINE, SUSPENDED, OFFLINE = "o", "s", "-"

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._servers = {}  # server id -> _ServerHistory
        self._last_at = None
        self.since = None  # the first tick: the panel's start

    def record(self, at, samples):
        """One tick of the traffic loop, at `at` (epoch seconds).

        `samples` holds every server: None when it is stopped (no tick, so a gap),
        else {client id: (state, rx, tx)} with the peer's counters, or None for both
        when the daemon has no such peer (a suspended client). A server missing from
        `samples` was deleted, and its history goes.
        """
        with self._lock:
            if self._last_at is not None and at <= self._last_at:
                return
            interval = at - self._last_at if self._last_at is not None else self.TICK_SECONDS
            self._last_at = at
            if self.since is None:
                self.since = at
            for server_id in [sid for sid in self._servers if sid not in samples]:
                del self._servers[server_id]
            for server_id, clients in samples.items():
                server = self._servers.setdefault(server_id, _ServerHistory())
                if clients is None:
                    server.stopped()
                else:
                    server.add(at, interval, clients)

    def rates(self, server_id):
        """{client id: (received bit/s, sent bit/s)} over the last tick, or {} when the
        server was not running at it."""
        with self._lock:
            server = self._servers.get(server_id)
            if not server or not server.ticks or server.ticks[-1][0] != self._last_at:
                return {}
            _, interval, data = server.ticks[-1]
            return {cid: (_bps(rx, interval), _bps(tx, interval)) for cid, (rx, tx, _state) in data.items()}

    def series(self, server_id, range_name, client_ids):
        """The history of `range_name` for the clients `client_ids`, for the page.

        1h is every tick; 6h and 24h one point per minute, on a grid that ends at the
        current minute. A point with no data for a client is None, its state " ": the
        page draws a gap there, not a zero. Totals are bytes over the range.
        """
        span = self.RANGES[range_name]
        with self._lock:
            now = self._clock()
            server = self._servers.get(server_id)
            if range_name == "1h":
                points = [tick for tick in (server.ticks if server else ()) if tick[0] > now - span]
            else:
                minutes = {bucket[0]: bucket for bucket in (server.minutes if server else ())}
                first = int(now // 60) * 60 - span + 60
                points = [minutes.get(minute) or (minute, None, {}) for minute in range(first, first + span, 60)]
            clients, totals = {}, {}
            for cid in client_ids:
                entries = [(data.get(cid), seconds) for _, seconds, data in points]
                clients[cid] = {
                    "received_bps": [_bps(e[0], seconds) if e else None for e, seconds in entries],
                    "sent_bps": [_bps(e[1], seconds) if e else None for e, seconds in entries],
                    "state": "".join(e[2] if e else " " for e, _ in entries),
                }
                totals[cid] = {
                    "received_bytes": sum(e[0] for e, _ in entries if e),
                    "sent_bytes": sum(e[1] for e, _ in entries if e),
                }
            return {
                "since": self.since,
                "now": now,
                "t": [round(at, 1) for at, _, _ in points],
                "clients": clients,
                "totals": totals,
            }


class _ServerHistory:
    """One server's rings. A tick is (at, seconds since the last tick, {client id:
    (received bytes, sent bytes, state)}); a minute is [its start, the seconds its
    ticks cover, {client id: [received bytes, sent bytes, state of its last tick]}]."""

    def __init__(self):
        self.ticks = deque()
        self.minutes = deque()
        self.counters = {}  # client id -> (rx, tx) at its last reading
        # Stopped at the last reading: whatever peers it has next start from 0.
        self.from_zero = False

    def stopped(self):
        self.counters, self.from_zero = {}, True

    def add(self, at, interval, clients):
        data, counters = {}, {}
        for cid, (state, rx, tx) in clients.items():
            if rx is None or tx is None:
                # No peer in the daemon: nothing moved, and when it is added back (a
                # client reactivated) it counts from 0.
                data[cid] = (0, 0, state)
                counters[cid] = (0, 0)
                continue
            last = self.counters.get(cid, (0, 0) if self.from_zero else None)
            if last is None:
                delta = (0, 0)  # the first reading (the panel started): a baseline
            elif rx < last[0] or tx < last[1]:
                delta = (rx, tx)  # reset between two readings (an interface restart)
            else:
                delta = (rx - last[0], tx - last[1])
            data[cid] = (*delta, state)
            counters[cid] = (rx, tx)
        self.counters, self.from_zero = counters, False  # a deleted client's go

        self.ticks.append((at, interval, data))
        while self.ticks[0][0] <= at - TrafficHistory.FINE_SECONDS:
            self.ticks.popleft()

        minute = int(at // 60) * 60
        if not self.minutes or self.minutes[-1][0] != minute:
            self.minutes.append([minute, 0.0, {}])
        bucket = self.minutes[-1]
        bucket[1] += interval
        for cid, (rx, tx, state) in data.items():
            sums = bucket[2].setdefault(cid, [0, 0, state])
            sums[0] += rx
            sums[1] += tx
            sums[2] = state
        while self.minutes[0][0] <= minute - TrafficHistory.MINUTES * 60:
            self.minutes.popleft()


def _bps(byte_count, seconds):
    return round(byte_count * 8 / seconds) if seconds else 0
