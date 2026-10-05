"""Tests for the failed sign-ins (services/authlog.py, DEVELOPMENT.md §3
Activity events): nginx's error-log records parsed, read incrementally across rotation and
truncation, and grouped into one `auth.fail` per address per minute.
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import build_manager

FIXTURE = Path(__file__).parent / "fixtures" / "nginx_error.log"
# The fixture's minute, 2026/10/02 23:25 in nginx's local time, and one in the next.
IN_MINUTE = time.mktime((2026, 10, 2, 23, 25, 40, 0, 0, -1))
NEXT_MINUTE = IN_MINUTE + 60


def failure(at, address="203.0.113.7", user="admin", missing=False):
    """One record as nginx writes it (the fixture's shapes), at local time `at`."""
    stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(at)).encode()
    reason = b' was not found in "/etc/amnezia/.htpasswd"' if missing else b": password mismatch"
    user = user.encode() if isinstance(user, str) else user
    return (
        stamp + b" [error] 37#37: *3 user \"" + user + b'"' + reason + b", client: " + address.encode()
        + b', server: _, request: "GET / HTTP/1.1", host: "127.0.0.1:8187"\n'
    )  # fmt: skip


class AuthLogTests(unittest.TestCase):
    def setUp(self):
        from services.authlog import AuthLog

        tmp = tempfile.mkdtemp(prefix="awg-authlog-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = os.path.join(tmp, "error.log")
        Path(self.path).write_bytes(b"2026/10/02 23:00:00 [notice] 1#1: start worker processes\n")
        self.now = IN_MINUTE
        self.log = AuthLog(self.path, clock=lambda: self.now)
        self.addCleanup(self.log.close)
        self.assertEqual(self.log.poll(), [])  # the boot's baseline

    def append(self, data, path=None):
        with open(path or self.path, "ab") as f:
            f.write(data)

    def test_the_real_lines(self):
        from services.authlog import parse_failure

        mismatch, missing = FIXTURE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(parse_failure(mismatch), ("2026/10/02 23:25", "192.168.215.1", "admin"))
        self.assertEqual(parse_failure(missing), ("2026/10/02 23:25", "192.168.215.1", "mallory"))
        self.assertIsNone(parse_failure('2026/10/02 23:25:19 [notice] 1#1: using the "epoll" event method'))

        self.append(FIXTURE.read_bytes())
        self.assertEqual(self.log.poll(), [])  # the minute has not ended
        self.now = NEXT_MINUTE
        self.assertEqual(self.log.poll(), [{"address": "192.168.215.1", "user": "admin", "count": 2}])
        self.assertEqual(self.log.poll(), [])

    def test_one_event_per_address_per_minute(self):
        # Attempts across several ticks of one minute, and the next minute's.
        self.append(failure(IN_MINUTE, user="root") + failure(IN_MINUTE + 1, address="2001:db8::5", missing=True))
        self.assertEqual(self.log.poll(), [])
        self.append(failure(IN_MINUTE + 8) + failure(IN_MINUTE + 9) + failure(NEXT_MINUTE))
        self.now = NEXT_MINUTE + 1
        self.assertEqual(
            self.log.poll(),
            [{"address": "203.0.113.7", "user": "admin", "count": 3},  # the user typed most often
             {"address": "2001:db8::5", "user": "admin", "count": 1}],
        )  # fmt: skip
        self.now = NEXT_MINUTE + 60
        self.assertEqual(self.log.poll(), [{"address": "203.0.113.7", "user": "admin", "count": 1}])

    def test_what_the_file_held_at_boot_is_not_reported(self):
        from services.authlog import AuthLog

        self.append(failure(IN_MINUTE))
        later = AuthLog(self.path, clock=lambda: NEXT_MINUTE)
        self.addCleanup(later.close)
        self.assertEqual(later.poll(), [])
        self.append(failure(IN_MINUTE + 5))
        self.assertEqual(later.poll(), [{"address": "203.0.113.7", "user": "admin", "count": 1}])

    def test_a_rotation_drains_the_old_file_then_reads_the_new_one(self):
        self.append(failure(IN_MINUTE))
        os.rename(self.path, self.path + ".1")
        self.append(failure(IN_MINUTE + 1), self.path + ".1")  # nginx still writes to the renamed file
        self.append(failure(IN_MINUTE + 2, address="198.51.100.9"), self.path)
        self.now = NEXT_MINUTE
        self.assertEqual(
            self.log.poll(),
            [{"address": "203.0.113.7", "user": "admin", "count": 2},
             {"address": "198.51.100.9", "user": "admin", "count": 1}],
        )  # fmt: skip
        self.append(failure(NEXT_MINUTE + 1, user="new"), self.path)
        self.now = NEXT_MINUTE + 60
        self.assertEqual(self.log.poll(), [{"address": "203.0.113.7", "user": "new", "count": 1}])

    def test_a_removed_file_and_one_that_appears_later(self):
        os.remove(self.path)
        self.assertEqual(self.log.poll(), [])
        self.append(failure(IN_MINUTE))  # a new file: read from its start
        self.now = NEXT_MINUTE
        self.assertEqual(self.log.poll(), [{"address": "203.0.113.7", "user": "admin", "count": 1}])

    def test_a_truncation_reads_from_the_start(self):
        self.append(failure(IN_MINUTE) * 3)
        self.log.poll()
        os.truncate(self.path, 0)
        self.append(failure(IN_MINUTE + 5, user="after"))
        self.now = NEXT_MINUTE
        self.assertEqual(self.log.poll(), [{"address": "203.0.113.7", "user": "admin", "count": 4}])

    def test_a_partial_line_waits_for_its_end(self):
        line = failure(IN_MINUTE)
        self.append(line[:50])
        self.log.poll()
        self.append(line[50:])
        self.now = NEXT_MINUTE
        self.assertEqual(self.log.poll(), [{"address": "203.0.113.7", "user": "admin", "count": 1}])

    def test_a_flood_drains_over_several_ticks(self):
        from services.authlog import AuthLog

        log = AuthLog(self.path, clock=lambda: NEXT_MINUTE, chunk=1000)
        self.addCleanup(log.close)
        log.poll()
        self.append(failure(IN_MINUTE) * 40)  # ~200 bytes each
        counts = [sum(event["count"] for event in log.poll()) for _ in range(10)]
        self.assertGreater(counts.count(0), 0)  # done before the tenth tick
        self.assertEqual(sum(counts), 40)

    def test_users_nginx_logs_raw(self):
        from services.authlog import parse_failure

        # Records from a real container: a newline in the user continues the record on
        # the next line (which tries to look like another failure); bytes that are not
        # UTF-8; a quote; a user that copies the record's own wording. The address is
        # nginx's in each.
        records = [
            (
                b'2026/10/02 23:25:46 [error] 43#43: *9 user "eve\nuser "x"" was not found in "/etc/amnezia/.htpasswd",'
                b' client: 192.168.215.1, server: _, request: "GET / HTTP/1.1", host: "127.0.0.1:8187"\n'
            ),
            failure(IN_MINUTE, user=b"\xff\xfe\xd0\xbf", address="192.168.215.1", missing=True),
            failure(IN_MINUTE, user='ad"m in', address="192.168.215.1", missing=True),
            failure(IN_MINUTE, user='x" was not found in "', address="192.168.215.1", missing=True),
        ]
        users = ['eve\nuser "x"', "\ufffd\ufffdп", 'ad"m in', 'x" was not found in "']
        for record, user in zip(records, users, strict=True):
            self.assertEqual(
                parse_failure(record.decode("utf-8", errors="replace")), ("2026/10/02 23:25", "192.168.215.1", user)
            )
        self.append(b"".join(records))
        self.now = NEXT_MINUTE
        self.assertEqual(self.log.poll(), [{"address": "192.168.215.1", "user": users[0], "count": 4}])

    def test_no_file_and_no_path(self):
        from services.authlog import AuthLog

        missing = AuthLog(self.path + ".none")
        with self.assertNoLogs("services.authlog"):
            self.assertEqual(missing.poll(), [])
        self.assertEqual(AuthLog(None).poll(), [])


class AuthFailEventTests(unittest.TestCase):
    def test_the_manager_records_an_auth_fail_event(self):
        tmp = tempfile.mkdtemp(prefix="awg-authlog-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "error.log")
        Path(path).touch()
        manager = build_manager(auth_log_path=path)
        self.addCleanup(manager.auth_log.close)
        manager.auth_log._clock = lambda: NEXT_MINUTE
        manager.record_auth_failures()  # the baseline
        Path(path).write_bytes(FIXTURE.read_bytes())
        manager.record_auth_failures()

        (event,) = manager.activity.payload()["events"]
        self.assertEqual(
            {k: v for k, v in event.items() if k not in ("seq", "ts")},
            {"kind": "auth", "event": "auth.fail", "server_id": None, "server": None, "client_id": None,
             "client": None, "detail": {"address": "192.168.215.1", "user": "admin", "count": 2}},
        )  # fmt: skip
        self.assertEqual(manager.events.published[-1], ("activity", event))


if __name__ == "__main__":
    unittest.main()
