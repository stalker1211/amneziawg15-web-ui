"""Tests for the live updates: core/events.py and GET /api/events.

The broadcaster fans each published event out to every open stream, each through a
bounded queue. A stream starts with `retry:`, sends a ping when idle, and
unsubscribes when the server closes it, which is what happens when a write to a
closed tab fails. The route serves that generator with the headers nginx and the
browser need.
"""

import json
import threading
import unittest

from core.events import PING, EventBroadcaster, format_event

from tests.support import build_app


def parse(message):
    """(event, data) of one SSE message."""
    message = message.decode() if isinstance(message, bytes) else message
    fields = dict(line.split(": ", 1) for line in message.strip().split("\n"))
    return fields["event"], json.loads(fields["data"])


class FormatTests(unittest.TestCase):
    def test_an_event_is_its_name_and_one_data_line(self):
        message = format_event("traffic_update", {"server_id": "a1", "geo": "Zürich\nsecond line"})
        self.assertEqual(message.count("\n"), 3)  # event:, data:, the blank line ending it
        self.assertEqual(parse(message), ("traffic_update", {"server_id": "a1", "geo": "Zürich\nsecond line"}))

    def test_the_ping_is_an_event_the_page_can_see(self):
        # An SSE comment (": ...") never reaches the page's script, so it could not
        # tell a quiet stream from a dead one.
        self.assertEqual(parse(PING), ("ping", {}))


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.events = EventBroadcaster(queue_size=4)

    def open(self, **kwargs):
        stream = self.events.stream(**{"heartbeat": 5, **kwargs})
        self.addCleanup(stream.close)
        return stream

    def test_a_stream_starts_with_retry_and_only_then_subscribes(self):
        stream = self.open(retry_ms=1234)
        self.assertEqual(self.events.subscriber_count(), 0)  # nothing until the response starts
        self.assertEqual(next(stream), "retry: 1234\n\n")
        self.assertEqual(self.events.subscriber_count(), 1)

    def test_every_stream_gets_every_event(self):
        streams = [self.open(), self.open()]
        for stream in streams:
            next(stream)
        self.events.publish("server_status", {"server_id": "a1", "status": "running"})
        for stream in streams:
            self.assertEqual(parse(next(stream)), ("server_status", {"server_id": "a1", "status": "running"}))

    def test_an_idle_stream_pings(self):
        stream = self.open(heartbeat=0.01)
        next(stream)
        self.assertEqual(next(stream), PING)

    def test_a_closed_stream_unsubscribes(self):
        # What the server does when a write to a closed tab fails.
        stream = self.open()
        next(stream)
        stream.close()
        self.assertEqual(self.events.subscriber_count(), 0)
        self.events.publish("server_status", {})  # nobody left: nothing happens

    def test_a_stuck_stream_loses_events_instead_of_growing(self):
        stream = self.open()
        next(stream)
        for n in range(10):
            self.events.publish("traffic_update", {"n": n})
        self.assertEqual([parse(next(stream))[1]["n"] for _ in range(4)], [0, 1, 2, 3])  # the queue held 4
        self.events.publish("traffic_update", {"n": 10})
        self.assertEqual(parse(next(stream))[1]["n"], 10)  # and it carries on once read

    def test_publishing_from_plain_threads(self):
        stream = self.open()
        next(stream)
        threads = [threading.Thread(target=self.events.publish, args=("traffic_update", {"n": n})) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(parse(next(stream))[1]["n"] for _ in range(4)), [0, 1, 2, 3])


class EventsRouteTests(unittest.TestCase):
    def test_the_route_streams_with_the_headers_nginx_and_the_browser_need(self):
        app, manager = build_app()
        manager.events = EventBroadcaster()
        response = app.test_client().get("/api/events", buffered=False)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "text/event-stream")
            self.assertEqual(response.headers["X-Accel-Buffering"], "no")  # nginx passes it through
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
            body = iter(response.response)
            self.assertTrue(next(body).startswith(b"retry: "))
            manager.events.publish("server_status", {"server_id": "a1", "status": "stopped"})
            self.assertEqual(parse(next(body)), ("server_status", {"server_id": "a1", "status": "stopped"}))
        finally:
            response.close()
        self.assertEqual(manager.events.subscriber_count(), 0)


if __name__ == "__main__":
    unittest.main()
