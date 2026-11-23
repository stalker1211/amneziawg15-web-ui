#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""DisableCookies under a replay flood (DEVELOPMENT.md §4, "AWG 3.1 specifics"): does a new
device still get in, and does the server answer the flood?

Three containers from the release checker's image on one Docker network: a server
(amneziawg-go, AWG 3.1, held to --cpus), a client and a flooder. One real handshake
initiation of the client is captured at the server and replayed by the flooder at a set
rate. MAC1 holds on a replay, so it reaches the under-load logic that DisableCookies
switches off, and nobody needs a key to send it. Then the client starts over and pings
until its first reply. Per (DisableCookies, rate), one JSON line: the client's seconds
to that reply (null: not within 45 s), the server's CPU, and the packets it sent back
per second (cookie replies, with cookies on).

Measured 2026-11-22, one CPU: with cookies 5.2 s at every rate up to 200k/s and a
cookie reply per replay; with DisableCookies on nothing sent back, 0.3-17 s at 100k/s
(once not within 45 s in 5 runs), and at 200k/s twice not within 45 s in 4. One trial is luck of a free slot in the
handshake queue, so repeat the DisableCookies-on runs (--repeat).

Build the image first (tests/release_check.py builds amneziawg-web-ui:check). The
default run, both settings at 100k/s, takes about 75 s. It stops unless Docker is a
local socket, like the release checker: it floods.

    tests/cookie_flood.py [--rates 100000] [--modes off,on] [--repeat 1] [--cpus 1]
"""

import argparse
import json
import subprocess
import sys
import time

NET, SRV, CLI, FLD = "cfl-net", "cfl-srv", "cfl-cli", "cfl-fld"
PORT, S1 = 51820, 20
TRANSPORT = (
    "S1 = 20\nS2 = 33\nS3 = 27\nS4 = 17\nH1 = 100000-200000\nH2 = 300000-400000\nH3 = 500000-600000\nH4 = 700000-800000\n"
)
DEADLINE = 45  # seconds the client keeps pinging

# Run in the server: print the first UDP payload to PORT of the given length (an initiation).
SNIFF = r"""
import socket, sys
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
s.settimeout(20)
want, port = int(sys.argv[1]), int(sys.argv[2])
while True:
    frame = s.recv(65535)
    if frame[12:14] != b"\x08\x00" or frame[23] != 17:
        continue
    udp = frame[14 + (frame[14] & 15) * 4:]
    if int.from_bytes(udp[2:4], "big") == port and len(udp) - 8 == want:
        print(udp[8:].hex())
        break
"""

# Run in the flooder: send the payload at `rate` per second from 4 processes for `secs`.
FLOOD = r"""
# FLOOD_MARK
import socket, sys, time, os
host, port, rate, secs, payload = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), bytes.fromhex(sys.argv[5])
procs = 4
for _ in range(procs - 1):
    if os.fork() == 0:
        break
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setblocking(False)
addr = (socket.gethostbyname(host), port)
sent, start = 0, time.monotonic()
while (now := time.monotonic()) - start < secs:
    while sent < int(rate / procs * (now - start)):
        try:
            s.sendto(payload, addr)
        except BlockingIOError:
            pass
        sent += 1
    time.sleep(0.0005)
"""


def sh(*args, check=True):
    r = subprocess.run(args, check=False, text=True, capture_output=True)
    if check and r.returncode:
        sys.exit(f"{' '.join(args[:4])}…: {r.stderr.strip()}")
    return r.stdout.strip()


def dexec(name, script, *args, check=True):
    return sh("docker", "exec", name, "bash", "-c", script, "_", *args, check=check)


def up(name, conf, address):
    """A fresh amneziawg-go interface from `conf` (awg setconf format)."""
    dexec(
        name,
        "ip link del awg0 2>/dev/null; pkill -x amneziawg-go; sleep 0.3; amneziawg-go awg0 && sleep 0.5 "
        '&& printf "%s" "$1" > /tmp/awg0.conf && awg setconf awg0 /tmp/awg0.conf '
        '&& ip addr add "$2" dev awg0 && ip link set awg0 up',
        conf,
        address,
    )


def server_conf(keys, cookies_off):
    return (
        f"[Interface]\nPrivateKey = {keys['srv']}\nListenPort = {PORT}\n{TRANSPORT}"
        f"DisableCookies = {'on' if cookies_off else 'off'}\n\n"
        f"[Peer]\nPublicKey = {keys['cli_pub']}\nAllowedIPs = 10.9.0.2/32\n"
    )


def client_conf(keys, cookies_off):
    return (
        f"[Interface]\nPrivateKey = {keys['cli']}\nJc = 4\nJmin = 40\nJmax = 70\n{TRANSPORT}"
        f"DisableCookies = {'on' if cookies_off else 'off'}\n\n"
        f"[Peer]\nPublicKey = {keys['srv_pub']}\nEndpoint = {SRV}:{PORT}\nAllowedIPs = 10.9.0.1/32\n"
    )


def time_to_reply(deadline):
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        ping = ["docker", "exec", CLI, "ping", "-c", "1", "-W", "1", "10.9.0.1"]
        if subprocess.run(ping, check=False, capture_output=True).returncode == 0:
            return time.monotonic() - start
    return None


def cpu_percent(name):
    out = sh("docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", name, check=False)
    return float(out.rstrip("%") or 0)


def counters():
    """The server's eth0 (rx, tx) packets: replays that arrived, and what it sent back."""
    stats = "/sys/class/net/eth0/statistics"
    rx, tx = dexec(SRV, f"cat {stats}/rx_packets {stats}/tx_packets").split()
    return int(rx), int(tx)


def trial(keys, initiation, cookies_off, rate):
    # A running server has seen the client's newer handshakes, so the replay is stale.
    up(SRV, server_conf(keys, cookies_off), "10.9.0.1/24")
    up(CLI, client_conf(keys, cookies_off), "10.9.0.2/24")
    if time_to_reply(15) is None:
        sys.exit("no tunnel before the flood")
    if rate:
        flood = ["docker", "exec", FLD, "python3", "-c", FLOOD, SRV, str(PORT), str(rate), str(DEADLINE + 15), initiation]
        subprocess.Popen(flood, stdout=subprocess.DEVNULL)
        time.sleep(3)
    (rx0, tx0), t0 = counters(), time.monotonic()
    cpu = cpu_percent(SRV)
    up(CLI, client_conf(keys, cookies_off), "10.9.0.2/24")
    seconds = time_to_reply(DEADLINE)
    cpu = max(cpu, cpu_percent(SRV))
    (rx1, tx1), elapsed = counters(), time.monotonic() - t0
    dexec(FLD, "pkill -f FLOOD_MARK || true", check=False)
    time.sleep(1)
    return {
        "DisableCookies": "on" if cookies_off else "off",
        "rate_pps": rate,
        "arrived_pps": round((rx1 - rx0) / elapsed),
        "first_reply_s": None if seconds is None else round(seconds, 1),
        "server_cpu_pct": round(cpu),
        "server_sent_pps": round((tx1 - tx0) / elapsed),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", default="amneziawg-web-ui:check")
    ap.add_argument("--cpus", default="1", help="the server container's CPU cap")
    ap.add_argument("--rates", default="100000", help="replays per second, comma-separated")
    ap.add_argument("--modes", default="off,on", help="DisableCookies values, comma-separated")
    ap.add_argument("--repeat", type=int, default=1, help="trials per (mode, rate)")
    a = ap.parse_args()

    endpoint = sh("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if not endpoint.startswith("unix://"):
        sys.exit(f"Docker is {endpoint}, not a local socket; switch the context (docker context use) or DOCKER_HOST")
    if subprocess.run(["docker", "image", "inspect", a.image], check=False, capture_output=True).returncode:
        sys.exit(f"no image {a.image}: run tests/release_check.py first")

    for n in (SRV, CLI, FLD):
        sh("docker", "rm", "-f", n, check=False)
    sh("docker", "network", "rm", NET, check=False)
    sh("docker", "network", "create", NET)
    tun = ("--cap-add", "NET_ADMIN", "--device", "/dev/net/tun")
    try:
        sh("docker", "run", "-d", "--name", SRV, "--network", NET, "--cpus", a.cpus, *tun,
           "--entrypoint", "sleep", a.image, "infinity")  # fmt: skip
        for n in (CLI, FLD):
            sh("docker", "run", "-d", "--name", n, "--network", NET, *tun, "--entrypoint", "sleep", a.image, "infinity")
        keys = {}
        for who in ("srv", "cli"):
            keys[who] = dexec(SRV, "awg genkey")
            keys[f"{who}_pub"] = dexec(SRV, 'printf "%s" "$1" | awg pubkey', keys[who])

        # One real initiation, captured at the server (no trailers: 148 + S1 bytes).
        up(SRV, server_conf(keys, False), "10.9.0.1/24")
        sniff = ["docker", "exec", SRV, "python3", "-c", SNIFF, str(148 + S1), str(PORT)]
        sniffer = subprocess.Popen(sniff, stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        up(CLI, client_conf(keys, False), "10.9.0.2/24")
        time_to_reply(10)
        initiation = sniffer.communicate(timeout=30)[0].strip()
        if not initiation:
            sys.exit("no initiation captured")

        for mode in a.modes.split(","):
            for rate in [int(r) for r in a.rates.split(",")]:
                for _ in range(a.repeat):
                    print(json.dumps(trial(keys, initiation, mode == "on", rate)), flush=True)
    finally:
        for n in (SRV, CLI, FLD):
            sh("docker", "rm", "-f", n, check=False)
        sh("docker", "network", "rm", NET, check=False)


if __name__ == "__main__":
    main()
