"""Activity: the panel's events -- a server started, a client edited, a session, a
failed sign-in -- dozens a day, not the log (DEVELOPMENT.md §3, Activity events).

Each event goes three ways, the same object in each: a ring in memory (the newest
RING_SIZE, for GET /api/activity; a restart empties it, like the traffic history),
the page's live updates (an SSE `activity` event), and one JSON line on PID 1's
stdout, which is supervisord (start.sh execs it), so `docker logs` and Promtail get
them. The line adds `"src": "awg-webui"`, since the panel's log, nginx and supervisord
write their own lines there too (2.7.1), and a `level` (`LEVELS`: info, warning or
error), so Grafana shows it like any log.

An event never carries a key, a password, I1-I5's content or a config: a *change*
names the fields it touched, with old -> new only for the short, non-secret values
in `VALUE_FIELDS` (`field_changes`).
"""

import json
import threading
import time
from collections import deque

from core.logging_setup import get_logger

logger = get_logger(__name__)

STDOUT = "/proc/1/fd/1"
SOURCE = "awg-webui"
RING_SIZE = 1000
# The stdout line's `level`, for log tooling (Grafana colours only standard level names);
# `kind` stays the category. Any other event is "info".
LEVELS = {
    "health.problem": "error",
    "egress.change": "warning",
    "auth.fail": "warning",
    # services/probe.py: a device that cannot connect, so a Grafana alert on Loki sees it.
    "client.old_config": "warning",
    "client.maybe_blocked": "warning",
    "client.recovered": "info",
}

# Fields whose old and new values an event may show; any other field goes by name
# alone (I1-I5, HeaderProtectionKey, RandomTrailers, ContentPaddingAddition...).
VALUE_FIELDS = frozenset(
    ("Protocol", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "Jc", "Jmin", "Jmax")
    + ("RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts")
    + ("MTU", "Port", "Subnet", "DNS", "AllowedIPs", "Endpoint host", "NAT", "Block LAN", "Name")
    + ("Connection analyzer",)
)


def utc_stamp(at):
    """Epoch seconds as the events' `ts`: UTC to the second."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def field_changes(old, new, fields=None, values: frozenset[str] | None = VALUE_FIELDS):
    """[{field, old, new}] for each field whose value differs between the dicts `old`
    and `new` (`fields`: their keys, in order; by default every key of either), with
    the values only for a field in `values` (None: every field, for the settings).
    A missing field and an empty one are the same (an unset parameter is stored as either)."""
    keys = list(fields) if fields is not None else list(dict.fromkeys([*old, *new]))
    changes = []
    for key in keys:
        before, after = old.get(key), new.get(key)
        if before == after or (before in (None, "") and after in (None, "")):
            continue
        if values is None or key in values:
            changes.append({"field": key, "old": before, "new": after})
        else:
            changes.append({"field": key})
    return changes


class Activity:
    """The ring, the stream and the stdout line (none with `path=None`: most tests).
    `record` is safe from any thread and never raises a write failure into a route or
    the monitor."""

    def __init__(self, events=None, path=STDOUT, size=RING_SIZE, clock=time.time):
        self._lock = threading.Lock()
        self._ring = deque(maxlen=size)
        self._seq = 0
        self._events = events
        self._clock = clock
        self._path = path
        self._file = None  # opened at the first event, then kept, line-buffered
        self._failed = False
        self.since = utc_stamp(clock())

    def record(self, kind, event, *, server=None, client=None, detail=None):
        """One event; `server` and `client` are the stored dicts (names as they are now)."""
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "ts": utc_stamp(self._clock()),
                "kind": kind,
                "event": event,
                "server_id": server.get("id") if server else None,
                "server": server.get("name") if server else None,
                "client_id": client.get("id") if client else None,
                "client": client.get("name") if client else None,
                "detail": detail or {},
            }
            self._ring.append(item)
            # Under the lock, so lines and the stream keep seq order: the page skips
            # an event at or below the last it has.
            self._write(item)
            if self._events is not None:
                self._events.publish("activity", item)
        return item

    def change(self, event, *, server=None, client=None, changes=None):
        """A *change*: `detail.changes` lists the fields (empty for create, start...)."""
        return self.record("change", event, server=server, client=client, detail={"changes": changes or []})

    def payload(self):
        """GET /api/activity: the whole ring, newest first, and the boot's time."""
        with self._lock:
            return {"events": list(reversed(self._ring)), "since": self.since}

    def close(self):
        """Close the stdout file (the tests; the panel keeps it for its lifetime)."""
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def _write(self, item):
        if self._failed or self._path is None:
            return
        try:
            if self._file is None:
                self._file = open(self._path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 -- kept open
            line = {"src": SOURCE, "level": LEVELS.get(item["event"], "info"), **item}
            self._file.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            # Logged once; the ring and the page still get every event.
            self._failed = True
            logger.warning("Activity events will not reach %s (docker logs): %s", self._path, e)
