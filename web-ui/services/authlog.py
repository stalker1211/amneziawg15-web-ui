"""Failed sign-ins, from nginx's error log: the *auth* Activity events
(DEVELOPMENT.md §10, 2.7 Activity part 3).

nginx (Basic Auth, config/nginx.conf) writes a failure to /var/log/nginx/error.log
(`error_log ... warn` in Alpine's /etc/nginx/nginx.conf), one record each:

    2026/10/02 23:25:28 [error] 37#37: *3 user "admin": password mismatch, client: 192.168.215.1, server: _, ...
    2026/10/02 23:25:28 [error] 38#38: *4 user "mallory" was not found in "/etc/amnezia/.htpasswd", client: ...

Never the password. No credential, an empty user or a malformed header is logged at
`info`, below `warn`, so it is not a failure here (a browser's first request has none).
The user is logged raw: a newline in it continues the record on the next line, which
is joined back, and it cannot hold a colon (Basic Auth splits there), so it can forge
neither a timestamp nor the `, client:` that follows it.

`AuthLog.poll()` runs once per monitor tick: it reads what was added since the last
one (the first sets a baseline at the end of the file), follows a rotation (it drains
the renamed file before it opens the new one) and a truncation, and returns one
`{address, user, count}` per address for each minute that has ended.
"""

import os
import re
import time
from collections import Counter

from core.logging_setup import get_logger

logger = get_logger(__name__)

ERROR_LOG = "/var/log/nginx/error.log"
# At most this much per tick: a flood drains over a few ticks, its minutes intact.
CHUNK = 1 << 20

# A record starts with nginx's timestamp; any other line continues the one before.
RECORD_START = re.compile(rb"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ")
FAILURE = re.compile(
    r'^(\d{4}/\d\d/\d\d \d\d:\d\d):\d\d \[error\] \d+#\d+: \*\d+ user "(.*?)"'
    r'(?:: password mismatch| was not found in "[^"]*"), client: ([^,\s]+), server: ',
    re.DOTALL,
)


def parse_failure(record):
    """(minute, address, user) from one error-log record, or None for any other record.
    The minute is nginx's local time to the minute, "2026/10/02 23:25"."""
    match = FAILURE.match(record)
    return (match.group(1), match.group(3), match.group(2)) if match else None


class AuthLog:
    """nginx's error log, read incrementally (no file with `path=None`: the tests).
    Only the monitor thread calls `poll`, so there is no lock."""

    def __init__(self, path=ERROR_LOG, clock=time.time, chunk=CHUNK):
        self._path = path
        self._clock = clock
        self._chunk = chunk
        self._file = None  # kept open between ticks, so a renamed file can be drained
        self._identity = None  # (st_dev, st_ino) of the open file
        self._started = False  # the first poll skips what the file already holds
        self._pending = {}  # (minute, address) -> Counter of users, in the order seen
        self._warned = False

    def poll(self):
        """[{address, user, count}] for each address and minute that has ended since
        the last poll, oldest first; `user` is the one typed most often in it."""
        if self._path is None:
            return []
        try:
            lines = self._read()
            self._warned = False
        except OSError as e:
            if not self._warned:
                logger.warning("Failed sign-ins cannot be read from %s: %s", self._path, e)
                self._warned = True
            return []
        for record in self._records(lines):
            failure = parse_failure(record.decode("utf-8", errors="replace"))
            if failure:
                minute, address, user = failure
                self._pending.setdefault((minute, address), Counter())[user] += 1
        return self._ended(time.strftime("%Y/%m/%d %H:%M", time.localtime(self._clock())))

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
            self._identity = None

    def _read(self):
        """The complete lines added since the last poll, from the open file and, after
        a rotation, from the new one."""
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            stat = None
        identity = (stat.st_dev, stat.st_ino) if stat else None
        lines = []
        if self._file is not None:
            if identity == self._identity and stat.st_size < self._file.tell():
                self._file.seek(0)  # truncated in place (copytruncate, `: >`)
            lines, full = self._drain()
            if identity == self._identity or full:
                return lines
            self.close()  # renamed or removed, and read to its end: the new one now
        if stat is not None:
            self._file = open(self._path, "rb")  # noqa: SIM115 -- kept open between ticks
            opened = os.fstat(self._file.fileno())
            self._identity = (opened.st_dev, opened.st_ino)
            if not self._started:
                self._file.seek(0, os.SEEK_END)  # boot: only what comes after
            lines += self._drain()[0]
        self._started = True
        return lines

    def _drain(self):
        """Up to a chunk of complete lines from the open file, and whether the chunk was
        full. A partial last line is left for the next poll."""
        data = self._file.read(self._chunk)
        full = len(data) == self._chunk
        end = data.rfind(b"\n") + 1
        if end < len(data) and (end or not full):  # a full chunk with no newline is dropped
            self._file.seek(end - len(data), os.SEEK_CUR)
            data = data[:end]
        return data.splitlines(), full

    @staticmethod
    def _records(lines):
        """Lines joined into records: a line without a timestamp continues the one before."""
        records = []
        for line in lines:
            if RECORD_START.match(line) or not records:
                records.append(line)
            else:
                records[-1] += b"\n" + line
        return records

    def _ended(self, now):
        """The pending minutes before `now`, as events, removed from the pending set."""
        events = []
        for key in sorted((key for key in self._pending if key[0] < now), key=lambda key: key[0]):
            users = self._pending.pop(key)
            user = users.most_common(1)[0][0]
            events.append({"address": key[1], "user": user or None, "count": sum(users.values())})
        return events
