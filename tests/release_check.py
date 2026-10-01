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
4. A server per protocol (from #appConfig), each with a client: running, the .conf's
   peers equal `awg show`'s, mode 600; /status healthy.
5. Stop one, `docker restart`: the same servers come back, the stopped one stays so.
6. A real client on each server: its .conf as the panel issues it, brought up with
   awg-quick in a second container from the same image, handshakes and pings.
7. The smoke tests (smoke_ui, smoke_events, smoke_signin last: it changes the
   password) in the puppeteer image, on the panel's network.
8. With --scan, `publish_dockerhub.sh --scan`.

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
PUPPETEER = "ghcr.io/puppeteer/puppeteer:latest"
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
        protocols = [p["id"] for p in config["protocols"]["supported"]]
        for i, protocol in enumerate(protocols):
            subnet = f"10.{90 + i}.0.0/24"
            server = api("POST", "/api/servers", {"name": f"check {i}", "protocol": protocol, "port": 51820 + i,
                                                  "subnet": subnet, "endpoint_host": HOST})  # fmt: skip
            sid, iface = server["id"], server["interface"]
            # Only its own subnet through the tunnel, so the client container holds all four.
            client = api("POST", f"/api/servers/{sid}/clients", {"name": "phone", "allowed_ips": subnet})
            configs[iface] = (protocol, subnet, client["config"])
            running = wait(lambda sid=sid: servers()[sid]["status"] == "running")
            peers = api("GET", f"/api/servers/{sid}/config")["config_content"].count("[Peer]")
            live = len(in_container("awg", "show", iface, "peers").stdout.split())
            conf = in_container("stat", "-c", "%a", f"/etc/amnezia/amneziawg/{iface}.conf").stdout.strip()
            check(f"{protocol}: running, .conf peers {peers} == awg peers {live}, mode {conf}",
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
        for n, (iface, (protocol, subnet, conf)) in enumerate(configs.items()):
            conf = "\n".join(line for line in conf.splitlines() if not line.startswith("DNS"))  # no resolvconf here
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

        print("7. Smoke tests (in the puppeteer image, on the container's network)", flush=True)
        for script in ("smoke_ui.js", "smoke_events.js", "smoke_signin.js"):  # signin last: it changes the password
            run = subprocess.run(["docker", "run", "--rm", "--network", f"container:{NAME}", "-v",
                                  f"{REPO / 'tests' / script}:/home/pptruser/s.js:ro", "-w", "/home/pptruser",
                                  PUPPETEER, "node", "s.js", "http://127.0.0.1", "admin", "changeme"],
                                 capture_output=True, text=True, check=False)  # fmt: skip
            failed = [line.strip() for line in run.stdout.splitlines() if line.strip().startswith("FAIL")]
            check(script, run.returncode == 0, failed or (run.stdout + run.stderr)[-300:])
    finally:
        if not args.keep:
            cleanup()

    if args.scan:
        print("8. publish_dockerhub.sh --scan", flush=True)
        check("the scan", subprocess.run(["./publish_dockerhub.sh", "--scan"], cwd=REPO, check=False).returncode == 0)

    print(f"\n{'All checks passed' if not failures else f'{len(failures)} failed: ' + '; '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
