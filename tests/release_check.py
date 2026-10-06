#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The release checker (DEVELOPMENT.md §11): the image from this tree, end to end.

1. Build the image (`amneziawg-web-ui:check`, never `:local`).
2. The image carries the tree: md5 of every tracked file in web-ui/ and scripts/. Easy
   to be fooled here by hand, since day-to-day testing bind-mounts the source.
3. Boot it without a bind mount, with the default admin/changeme sign-in: / gives
   200 with the password, 401 without and 401 with a wrong one; every script loads; a
   form POST gets 415; .htpasswd is root:nginx 640.
4. A server per protocol (from #appConfig), and one on AWG 3.1 with random trailers and
   Generate's equal S1-S4, each with a client whose I1-I5 come from Generate (the
   profiles in turn): running, the .conf's peers equal `awg show`'s, mode 600;
   /status healthy.
5. Stop one, `docker restart`: the same servers come back, the stopped one stays so.
6. A real client on each server: its .conf as the panel issues it, brought up with
   awg-quick in a second container from the same image, handshakes and pings; through
   the trailers server, 300 back-to-back pings at 56 and 1100 bytes lose none.
7. Why a device cannot connect (2.8), against those devices: (a) a server's S1/H1
   regenerated under its connected device gives Old config naming the old and new;
   (b) its replies dropped (iptables in the client container) give Maybe blocked, and
   the rule removed client.recovered; (c) trailers off in a device's config, on in its
   server's, give Old config, RandomTrailers; (d) the others never get one. Then a 10 s
   capture's CPU beside a large transfer through a tunnel.
8. The smoke tests (smoke_ui, smoke_events, smoke_signin last: it changes the
   password) with local Node against the published port (puppeteer: tests/browser.js,
   a global `npm i -g puppeteer`).
9. With --scan, `publish_dockerhub.sh --scan`.

It reports and never publishes. It stops unless Docker is a local socket (whichever
of the context and DOCKER_HOST wins): the checks reach the container on 127.0.0.1 and
mount files from this tree.

    tests/release_check.py [--keep] [--scan]

--keep leaves the container running; --scan adds `publish_dockerhub.sh --scan`.
"""

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGE, NAME, CLIENT, PORT = "amneziawg-web-ui:check", "awg-release-check", "awg-release-client", 18099
HOST = "awg.check"  # the panel's alias on its network: what client configs dial
BASE = f"http://127.0.0.1:{PORT}"
failures = []


def check(label, ok, detail: object = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' -> {detail}' if detail and not ok else ''}", flush=True)
    if not ok:
        failures.append(label)
    return ok


def sh(*cmd):
    return subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, text=True).stdout


def in_container(*cmd, name=NAME, stdin=None):
    return subprocess.run(["docker", "exec", "-i", name, *cmd], input=stdin, capture_output=True, text=True, check=False)


def http(method, path, body=None, password: str | None = "changeme", content_type="application/json"):
    """(status, body) through nginx, as a browser signed in as admin would."""
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else body.encode())
    if password is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def api(method, path, data=None):
    status, text = http(method, path, None if method == "GET" else json.dumps(data or {}))
    if status != 200:
        raise RuntimeError(f"{method} {path}: {status} {text[:200]}")
    return json.loads(text)


def wait(ready, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if ready():
                return True
        except (OSError, RuntimeError):
            pass
        time.sleep(1)
    return False


def cleanup():
    subprocess.run(["docker", "rm", "-f", NAME, CLIENT], capture_output=True, check=False)
    subprocess.run(["docker", "network", "rm", NAME], capture_output=True, check=False)


def servers():
    return {s["id"]: s for s in api("GET", "/api/servers")}


def diagnosis(case):
    """A client's *Old config* / *Maybe blocked*, as GET /api/servers gives it, or None."""
    return servers()[case["sid"]]["traffic"].get(case["cid"], {}).get("diagnosis")


def events(event, case):
    return [e for e in api("GET", "/api/activity")["events"] if e["event"] == event and e["client_id"] == case["cid"]]


def await_diagnosis(case, verdict, seconds=120):
    """(the diagnosis once its verdict is `verdict`, or None; the seconds it took)."""
    start, seen = time.time(), {}

    def ready():
        seen["d"] = diagnosis(case)
        return bool(seen["d"]) and seen["d"]["verdict"] == verdict

    return seen.get("d") if wait(ready, seconds) else None, time.time() - start


def show(d):
    print(f"        {json.dumps(d, sort_keys=True)}", flush=True)


def in_range(value, spec):
    low, _, high = str(spec).partition("-")
    return int(low) <= value <= int(high or low)


def diagnoses(configs):
    """Step 7: real daemons, real devices (DEVELOPMENT.md §10, 2.8, part 5)."""
    by_label = {case["label"].split(",")[0]: case for case in configs.values()}
    untouched = [by_label["AWG 1.5"], by_label["AWG 3.1"]]
    for case in configs.values():
        case["before"] = {e["seq"] for e in api("GET", "/api/activity")["events"] if e["client_id"] == case["cid"]}
    # The probe has seen every device connected: step 6 takes a few seconds, and the API
    # may show a handshake from a read outside the monitor's 7 s loop (a start's).
    seen = wait(lambda: all(servers()[c["sid"]]["traffic"][c["cid"]]["latest_handshake_at"] for c in configs.values()))
    time.sleep(8)
    check("every device connected, and no diagnosis", seen and all(diagnosis(case) is None for case in configs.values()))

    # (a) New S1/H1 (Randomize) on a server whose device keeps its config.
    case = by_label["AWG 2.0"]
    old = servers()[case["sid"]]["transport_params"]
    fresh = api("POST", "/api/generate", {"protocol": "AWG 2.0"})["transport_params"]
    api("POST", f"/api/servers/{case['sid']}/transport-params", {"protocol": "AWG 2.0", **fresh})
    d, took = await_diagnosis(case, "old_config")
    ok = bool(d) and d["device"]["S1"] == old["S1"] and in_range(d["device"]["H1"], old["H1"])
    ok = ok and d["server"]["S1"] == fresh["S1"] and {"S1", "H1"} <= set(d["mismatch"])
    ok = ok and bool(d["last_handshake"]) and d["params_changed_at"] > d["last_handshake"]  # the restart zeroes awg's
    check(f"(a) AWG 2.0, S1 {old['S1']} -> {fresh['S1']}: Old config in {took:.0f} s, old and new S1/H1", ok, d)
    show(d)
    check("(a) the Activity has client.old_config", bool(events("client.old_config", case)))

    # (b) The device's replies dropped on their way back: the server reads and answers
    # its handshakes. A device that connects anew; one blocked mid-session sends data on
    # its old keys until they expire (180 s) and takes ~3.5 min (DEVELOPMENT.md §10, 2.8).
    case = by_label["AWG 3.0"]
    conf = f"/tmp/{case['device']}.conf"
    rule = ["INPUT", "-p", "udp", "--sport", str(case["port"]), "-j", "DROP"]
    in_container("awg-quick", "down", conf, name=CLIENT)
    in_container("iptables", "-I", *rule, name=CLIENT)
    in_container("awg-quick", "up", conf, name=CLIENT)
    d, took = await_diagnosis(case, "maybe_blocked")
    ok = bool(d) and d["mismatch"] == [] and d["attempts"] >= 2
    check(f"(b) AWG 3.0, its replies dropped: Maybe blocked in {took:.0f} s", ok, d)
    show(d)
    check("(b) the Activity has client.maybe_blocked", bool(events("client.maybe_blocked", case)))
    in_container("iptables", "-D", *rule, name=CLIENT)
    start = time.time()
    recovered = wait(lambda: diagnosis(case) is None and events("client.recovered", case), 60)
    detail = (events("client.recovered", case) or [{}])[0].get("detail")
    check(f"(b) the rule removed: client.recovered in {time.time() - start:.0f} s, {detail}", recovered)

    # (c) Trailers on the server, off on the device: the server reads it, the device
    # drops the longer response (F5).
    case = by_label["AWG 3.1 + trailers"]
    conf = f"/tmp/{case['device']}.conf"
    in_container("awg-quick", "down", conf, name=CLIENT)
    in_container("sed", "-i", "/^RandomTrailers/d", conf, name=CLIENT)
    in_container("awg-quick", "up", conf, name=CLIENT)
    d, took = await_diagnosis(case, "old_config")
    ok = bool(d) and d["mismatch"] == ["RandomTrailers"] and not d["device"]["trailers"] and d["server"]["trailers"]
    check(f"(c) AWG 3.1, trailers off on the device: Old config in {took:.0f} s, RandomTrailers", ok, d)
    show(d)

    # (d) Correct clients, through all of it: never a verdict.
    for case in untouched:
        flagged = [e["event"] for e in api("GET", "/api/activity")["events"]
                   if e["client_id"] == case["cid"] and e["seq"] not in case["before"] and e["kind"] == "session"
                   and e["event"] in ("client.old_config", "client.maybe_blocked")]  # fmt: skip
        check(f"(d) {case['label']}: no verdict", diagnosis(case) is None and not flagged, flagged)


def capture_cost(configs):
    """A capture's CPU during a large transfer through a tunnel. The raw socket gets a
    copy of every inbound UDP datagram; its BPF filter drops them in the kernel (filtered
    in Python, 3.5 s of 10 at 4 Gbit/s, and half the device's datagrams lost)."""
    case = next(iter(configs.values()))
    gateway = case["subnet"].replace(".0/24", ".1")
    subprocess.run(["docker", "exec", "-d", NAME, "sh", "-c", "nc -l -p 5001 > /dev/null"], check=False)
    time.sleep(1)
    subprocess.run(["docker", "exec", "-d", CLIENT, "sh", "-c",
                    f"dd if=/dev/zero bs=64k count=100000 2>/dev/null | nc {gateway} 5001"], check=False)  # fmt: skip
    time.sleep(2)
    rx = f"/sys/class/net/{next(iter(configs))}/statistics/rx_bytes"
    code = (
        "import sys, time; sys.path.insert(0, '/app/web-ui'); from services.probe import capture\n"
        f"b = int(open('{rx}').read()); c = time.process_time()\n"
        "n = len(capture(51999, '192.0.2.1', 1, 10, 64))\n"
        f"print(time.process_time() - c, (int(open('{rx}').read()) - b) / 10, n)"
    )
    run = in_container("python3", "-c", code)
    subprocess.run(["docker", "exec", CLIENT, "pkill", "-f", "dd if=/dev/zero"], check=False, capture_output=True)
    subprocess.run(["docker", "exec", NAME, "pkill", "-f", "nc -l -p 5001"], check=False, capture_output=True)
    try:
        cpu, rate, _ = run.stdout.split()
    except ValueError:
        return check("a 10 s capture during a transfer", False, (run.stdout + run.stderr)[-300:])
    return check(f"a 10 s capture during a {float(rate) * 8 / 1e6:.0f} Mbit/s transfer: {float(cpu):.2f} s of CPU",
                 float(rate) > 1e6 and float(cpu) < 1, run.stdout)  # fmt: skip


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    parser.add_argument("--scan", action="store_true", help="also run publish_dockerhub.sh --scan")
    args = parser.parse_args()

    endpoint = sh("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}").strip()
    if not endpoint.startswith("unix://"):
        sys.exit(f"Docker is {endpoint}, not a local socket; switch the context (docker context use) or DOCKER_HOST")
    print(f"Docker: {endpoint}")
    label = sh("git", "describe", "--tags", "--match", "v[0-9]*", "--always").strip() + " (release check)"
    print(f"1. Build {IMAGE}: {label}", flush=True)
    subprocess.run(["docker", "build", "-q", "--build-arg", f"BUILD_LABEL={label}", "-t", IMAGE, "."], cwd=REPO, check=True)

    print("2. The image carries the tree", flush=True)
    files = [f for f in sh("git", "ls-files", "web-ui", "scripts").split() if not f.endswith(".md")]
    image = sh("docker", "run", "--rm", "--entrypoint", "md5sum", IMAGE, *(f"/app/{f}" for f in files))
    baked = {line.split()[1].removeprefix("/app/"): line.split()[0] for line in image.splitlines()}
    stale = [f for f in files if baked.get(f) != hashlib.md5((REPO / f).read_bytes()).hexdigest()]
    check(f"{len(files)} files of web-ui/ and scripts/ match", not stale, stale)

    print("3. Boot without a bind mount; the sign-in", flush=True)
    cleanup()
    sh("docker", "network", "create", NAME)
    tun = ["--network", NAME, "--cap-add", "NET_ADMIN", "--device", "/dev/net/tun"]
    sh("docker", "run", "-d", "--name", NAME, *tun, "--network-alias", HOST, "-p", f"127.0.0.1:{PORT}:80", IMAGE)
    try:
        if not check("the page answers", wait(lambda: http("GET", "/")[0] == 200)):
            sys.exit(f"\nStopped: nothing answers on {BASE} (docker logs {NAME})")
        check("/ with the password 200, without 401, a wrong one 401",
              [http("GET", "/", password=p)[0] for p in ("changeme", None, "wrong")] == [200, 401, 401])  # fmt: skip
        page = http("GET", "/")[1]
        scripts = re.findall(r'src="(/static/[^"?]+)', page)
        check(f"{len(scripts)} scripts load", scripts and all(http("GET", s)[0] == 200 for s in scripts))
        check(
            "a form POST is refused (415)",
            http("POST", "/api/servers", "name=x", content_type="application/x-www-form-urlencoded")[0] == 415,
        )
        mode = in_container("stat", "-c", "%U:%G %a", "/etc/amnezia/.htpasswd").stdout.strip()
        check(".htpasswd is root:nginx 640", mode == "root:nginx 640", mode)

        print("4. A server per protocol, with a client", flush=True)
        configs = {}  # interface -> (protocol, subnet, the client's .conf)
        config = json.loads(
            re.search(r'<script type="application/json" id="appConfig">(.*?)</script>', page, re.DOTALL).group(1)
        )
        cases: list[tuple[str, str, dict | None]] = [(p["id"], p["id"], None) for p in config["protocols"]["supported"]]
        # Random trailers with Generate's S1-S4, which it draws equal: step 6 wants no
        # ping lost through it (amneziawg-go#186 drops data packets as false handshakes
        # when S1-S4 differ).
        trailers = api("POST", "/api/generate", {"protocol": "AWG 3.1", "random_trailers": True})["transport_params"]
        cases.append(("AWG 3.1", "AWG 3.1 + trailers", {**trailers, "RandomTrailers": True}))
        for i, (protocol, label, transport) in enumerate(cases):
            subnet = f"10.{90 + i}.0.0/24"
            body = {"name": f"check {i}", "protocol": protocol, "port": 51820 + i, "subnet": subnet, "endpoint_host": HOST}
            server = api("POST", "/api/servers", {**body, **({"transport_params": transport} if transport else {})})
            sid, iface = server["id"], server["interface"]
            # Only its own subnet through the tunnel, so the client container holds them all.
            client = api("POST", f"/api/servers/{sid}/clients", {"name": "phone", "allowed_ips": subnet})
            # I1-I5 from Generate, a profile per server in turn: step 6's handshake shows
            # the daemon takes them.
            profile = config["signatureProfiles"][i % len(config["signatureProfiles"])]["id"]
            packets = api("POST", "/api/generate", {"server_id": sid, "signature_profile": profile})["signature_packets"]
            cid = client["client"]["id"]
            api("POST", f"/api/servers/{sid}/clients/{cid}/client-params", {"client_params": packets})
            issued = api("GET", f"/api/servers/{sid}/clients/{cid}/config-both")["clean_config"]
            configs[iface] = {
                "label": f"{label}, {profile} I1-I5",
                "protocol": protocol,
                "subnet": subnet,
                "conf": issued,
                "trailers": transport is not None,
                "sid": sid,
                "cid": cid,
                "port": body["port"],
            }
            running = wait(lambda sid=sid: servers()[sid]["status"] == "running")
            peers = api("GET", f"/api/servers/{sid}/config")["config_content"].count("[Peer]")
            live = len(in_container("awg", "show", iface, "peers").stdout.split())
            conf = in_container("stat", "-c", "%a", f"/etc/amnezia/amneziawg/{iface}.conf").stdout.strip()
            check(f"{label}: running, .conf peers {peers} == awg peers {live}, mode {conf}",
                  running and peers == live == 1 and conf == "600")  # fmt: skip
        check("/status is healthy", in_container("wget", "-q", "-O-", "http://127.0.0.1/status").returncode == 0)

        print("5. A restart brings back what was running", flush=True)
        stopped = list(servers())[-1]
        api("POST", f"/api/servers/{stopped}/stop")
        before = {sid: s["status"] for sid, s in servers().items()}
        sh("docker", "restart", NAME)
        back = wait(lambda: {sid: s["status"] for sid, s in servers().items()} == before)
        check(f"{len(before) - 1} running and one stopped, as before", back and list(before.values()).count("stopped") == 1)
        api("POST", f"/api/servers/{stopped}/start")

        print("6. A client on each server connects (its .conf in a second container)", flush=True)
        sh("docker", "run", "-d", "--name", CLIENT, *tun, "--entrypoint", "sleep", IMAGE, "infinity")
        for n, (iface, case) in enumerate(configs.items()):
            protocol, subnet = case["label"], case["subnet"]
            case["device"] = f"c{n}"  # its interface in the client container, from the file's name
            conf = "\n".join(line for line in case["conf"].splitlines() if not line.startswith("DNS"))  # no resolvconf
            in_container("sh", "-c", f"cat > /tmp/c{n}.conf", name=CLIENT, stdin=conf)
            up = in_container("awg-quick", "up", f"/tmp/c{n}.conf", name=CLIENT)
            # Traffic for the graphs: a ping a second to the server's end of the tunnel.
            gateway = subnet.replace(".0/24", ".1")
            subprocess.run(
                ["docker", "exec", "-d", CLIENT, "sh", "-c", f"while :; do ping -c1 -W1 {gateway}; sleep 1; done"], check=False
            )
            shook = wait(
                lambda iface=iface: (
                    in_container("awg", "show", iface, "latest-handshakes").stdout.split()[-1:] not in ([], ["0"])
                ),
                20,
            )
            check(f"{protocol}: the client's tunnel is up and handshakes", up.returncode == 0 and shook, up.stderr[-300:])
            if case["trailers"]:
                # Small and near-MTU packets: #186's branches each start at a size. Back
                # to back (-A); with one H range claiming a few %, 300 lose some.
                for size in (56, 1100):
                    ping = in_container("ping", "-A", "-q", "-c", "300", "-W", "2", "-s", str(size), gateway, name=CLIENT)
                    lost = re.search(r"(\d+)% packet loss", ping.stdout)
                    check(f"{protocol}: no loss at {size} bytes", lost and lost.group(1) == "0", ping.stdout[-200:])

        print("7. Why a device cannot connect: Old config, Maybe blocked", flush=True)
        diagnoses(configs)
        capture_cost(configs)

        print("8. Smoke tests (local Node, on the published port)", flush=True)
        node = shutil.which("node")
        for script in ("smoke_ui.js", "smoke_events.js", "smoke_signin.js"):  # signin last: it changes the password
            if not node:
                check(script, False, "no node on PATH: install Node and `npm i -g puppeteer`")
                continue
            run = subprocess.run([node, str(REPO / "tests" / script), BASE, "admin", "changeme"],
                                 capture_output=True, text=True, check=False)  # fmt: skip
            failed = [line.strip() for line in run.stdout.splitlines() if line.strip().startswith("FAIL")]
            check(script, run.returncode == 0, failed or (run.stdout + run.stderr)[-300:])
    finally:
        if not args.keep:
            cleanup()

    if args.scan:
        print("9. publish_dockerhub.sh --scan", flush=True)
        check("the scan", subprocess.run(["./publish_dockerhub.sh", "--scan"], cwd=REPO, check=False).returncode == 0)

    print(f"\n{'All checks passed' if not failures else f'{len(failures)} failed: ' + '; '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
