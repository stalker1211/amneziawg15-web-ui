"""Client sessions from the monitor's ticks: the *session* Activity events
(DEVELOPMENT.md §10, 2.7 Activity part 2).

No read of its own: each tick the manager hands over what the last `awg show all
dump` said of every client -- online or not (a handshake within the client's
threshold, `AmneziaManager.active_within`), and its peer's counters -- and gets back
the clients that came online or went offline since the tick before.

The first tick after boot is a baseline and reports nothing: a client online then
was already online, and its session counts from there. A failed read is not a tick
(the manager skips it), so a session never ends on one. A client deleted or moved
out of the config is dropped without an event (its *change* event says why).
"""


def counted(previous, current):
    """Bytes since the last tick; a counter that went down (the interface came back
    up, the peer was re-added) counts from 0, as in the traffic history."""
    if current is None:
        return 0
    if previous is None or current < previous:
        return current
    return current - previous


class Sessions:
    """Per client: its last counters and, while online, its session so far."""

    def __init__(self):
        self._clients = None  # None until the baseline tick

    def tick(self, at, observed):
        """`observed` is (client id, online, rx, tx) per client (rx/tx None with no
        peer in the dump). Returns ("client.online", id, None) and ("client.offline",
        id, {duration_s, received_bytes, sent_bytes}) in `observed`'s order.

        A session runs from the tick that saw it online to the last tick its received
        counter grew (keepalives, every 25 s in the configs issued here): the offline
        tick comes a whole threshold later, which is not time spent connected. Bytes
        are in the daemon's terms (received = the device's upload, §3).
        """
        baseline = self._clients is None
        previous = self._clients or {}
        current, changes = {}, []
        for client_id, online, rx, tx in observed:
            was = previous.get(client_id)
            session = was["session"] if was else None
            if online and session is None:
                session = {"since": at, "last_at": at, "received": 0, "sent": 0}
                if not baseline:
                    # The bytes since the last tick: its handshake and first packets.
                    session["received"] = counted(was and was["rx"], rx)
                    session["sent"] = counted(was and was["tx"], tx)
                    changes.append(("client.online", client_id, None))
            elif online:
                received = counted(was["rx"], rx)
                session = {
                    "since": session["since"],
                    "last_at": at if received else session["last_at"],
                    "received": session["received"] + received,
                    "sent": session["sent"] + counted(was["tx"], tx),
                }
            elif session is not None:
                detail = {
                    "duration_s": int(session["last_at"] - session["since"]),
                    "received_bytes": session["received"],
                    "sent_bytes": session["sent"],
                }
                changes.append(("client.offline", client_id, detail))
                session = None
            current[client_id] = {"rx": rx, "tx": tx, "session": session}
        self._clients = current
        return changes
