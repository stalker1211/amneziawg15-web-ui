"""Live updates for the page: Server-Sent Events on GET /api/events.

The push is one-way -- `server_status` after a start or stop, `traffic_update` every
7 s per running server -- so an ordinary HTTP response that never ends carries it.
Being an ordinary request under /api/, it passes nginx's Basic Auth like every other
call: the browser's EventSource sends the cached credential, which iPadOS Safari did
not do for a WebSocket upgrade (DEVELOPMENT.md §6). The browser also reconnects a
dropped stream by itself, after the `retry:` interval sent first.

Each open stream is a subscriber with a bounded queue, so a stuck tab loses events
rather than growing memory. An idle stream sends a `ping` event every
HEARTBEAT_SECONDS. That keeps nginx (60 s without data) from closing the stream; it
finds a closed tab, since the write fails and the server closes the generator, which
unsubscribes; and it lets the page notice a connection that died without a word (an
event, not an SSE comment, which would never reach the page's script).
"""

import json
import queue
import threading

from core.logging_setup import get_logger

logger = get_logger(__name__)

RETRY_MS = 3000
HEARTBEAT_SECONDS = 15
QUEUE_SIZE = 64


def format_event(event, data):
    """One message: the event's name and its JSON payload on a single `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


PING = format_event("ping", {})


class EventBroadcaster:
    """Fans every published event out to each open stream."""

    def __init__(self, queue_size=QUEUE_SIZE):
        self._queue_size = queue_size
        self._subscribers = set()
        self._lock = threading.Lock()

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def subscribe(self):
        subscriber = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event, data):
        """Queue the event for every open stream. Safe from any thread; never blocks."""
        message = format_event(event, data)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                logger.debug("An event stream is not keeping up; dropped a %s event", event)

    def stream(self, heartbeat=HEARTBEAT_SECONDS, retry_ms=RETRY_MS):
        """The body of one GET /api/events response. It never ends by itself: the
        server closes it when a write fails, and `finally` unsubscribes."""
        subscriber = self.subscribe()
        try:
            yield f"retry: {retry_ms}\n\n"
            while True:
                try:
                    yield subscriber.get(timeout=heartbeat)
                except queue.Empty:
                    yield PING
        finally:
            self.unsubscribe(subscriber)
