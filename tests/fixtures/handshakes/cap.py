"""Runs in the server container of run.sh: copies every inbound UDP datagram to the AWG
ports for DUR seconds into /tmp/capture.jsonl. A raw socket sees copies; the daemon
still gets the originals."""

import json
import socket
import sys
import time

dur = float(sys.argv[1])
ports = {51820, 51821}
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
s.settimeout(0.5)
start = time.time()
n = 0
with open("/tmp/capture.jsonl", "w") as out:
    while time.time() < start + dur:
        try:
            pkt = s.recv(65535)
        except TimeoutError:
            continue
        ihl = (pkt[0] & 0x0F) * 4
        dport = int.from_bytes(pkt[ihl + 2 : ihl + 4], "big")
        if dport not in ports:
            continue
        rec = {
            "t": round(time.time() - start, 3),
            "src": socket.inet_ntoa(pkt[12:16]),
            "sport": int.from_bytes(pkt[ihl : ihl + 2], "big"),
            "dport": dport,
            "hex": pkt[ihl + 8 :].hex(),
        }
        out.write(json.dumps(rec) + "\n")
        n += 1
print(n, "datagrams captured")
