# AmneziaWG Web UI

A web UI for running [AmneziaWG](https://github.com/amnezia-vpn/amneziawg-go) VPN
servers — WireGuard with obfuscation that resists DPI-based blocking. Create servers,
manage clients, hand out configs, and watch traffic live, all from one container.

Everything is configurable in the panel, including its own settings (⚙ in the
header: sign-in, logging, GeoIP). An environment variable can
pin a setting; only `NGINX_PORT`, `WAN_IF` and `AWG_LOG_FILE` are deployment-only. See [Environment variables](#environment-variables).

Current version: **2.7.1**

<img src="screenshot.png" alt="Web UI screenshot" width="50%"/>

## 🚀 Features

- **Servers and clients from the browser** — create, start/stop, rename, delete; add
  clients and hand out configs as `.conf`, text or QR code. A new server starts from
  the newest one's MTU, DNS, NAT and LAN blocking.
- **AWG 1.5 / 2.0 / 3.0 / 3.1**, with only the relevant fields shown per protocol.
  AWG 3.0 adds header protection, content padding and tunable timings; 3.1 adds
  random packet trailers and optional cookie-reply suppression.
- **Live monitoring** — per-client traffic, endpoint and handshake age, pushed to the
  page as it changes (Server-Sent Events), with country flags for endpoint / public /
  egress IPs. A server's egress (where its clients' traffic leaves) is checked again
  after each start and each NAT or LAN-block change.
- **Traffic graphs** — every running server's last hour on its card and each online
  client's in its row, with the rates now; a server's Traffic view shows 1 h, 6 h or
  24 h, per client, with when each was online or suspended. Kept in the panel's memory
  (every 7 s for an hour, per minute for a day), so a restart starts it over; nothing is
  written to disk. Download (↓) and upload (↑) are from the device's side.
- **Server totals** — beside the graph, what a running server has carried since it
  started (and since when): the kernel's counters for its interface, so a client
  suspended or deleted since still counts. A stop and start begins them again. A click
  opens the Traffic view, as on the graph.
- **Activity** — what happened, newest first: changes to servers, clients and
  settings (old → new for short values, never a key), clients coming online and going
  offline (endpoint, country, how long, bytes), health problems and egress changes,
  and failed sign-ins. From the header's clock button, or a server's ⋯ for that server
  alone; filters per kind; live. Kept in memory (the last ~1000), and each event is
  also a JSON line in `docker logs` (see [Logging](#-logging)).
- **Signature packets from a profile** — in a client's drawer, **Generate** fills
  I1–I5 with packets shaped like a QUIC connection opening or a DNS lookup (or random
  ones), so a handshake is preceded by traffic that looks like something else.
- **Health check** — Docker marks the container `unhealthy` when a server that should
  run is down, or has drifted from the panel: a device the panel suspended or deleted
  still let in, or firewall rules that are not what its switches call for.
- **Re-import marks** — the panel remembers which config each device received and
  marks a client **Re-import** when its config has changed since (new transport
  parameters, new client parameters, a new public IP). Server settings say how many
  devices a change affects before you save.
- **Split tunnelling and a stable endpoint** — each client's AllowedIPs is editable
  (all IPv4 by default; a narrower list for split tunnelling), and each server can
  give its clients a DNS name to dial (e.g. dynamic DNS), so a new public IP needs no
  re-import.
- **Client suspend** — revoke access without deleting; keys are preserved.
- **Automatic networking** — iptables NAT and optional private-LAN blocking per
  server (which also keeps its clients off the panel); a container restart brings back exactly the servers that were running;
  smart port/subnet/IP proposals.
- **Panel settings** (⚙) — the sign-in credential, the daemon's and the panel's log
  levels, and GeoIP, stored with the servers.
- **Forms checked as you type** by the server, in a side drawer; toasts and in-app
  confirmations instead of browser pop-ups; works down to phone width.
- **Dark theme** (the OS preference picks the first one), collapsible help, inline rename.
- **Self-contained UI** — no CDN: the page loads nothing from other hosts, so it works
  without internet access and never tells a third party where your panel is.
- Behind nginx HTTP Basic Auth; the web UI itself listens on loopback only, and the
  page runs no inline script (CSP `script-src 'self'`).

## 📝 Logging

Everything except the AWG daemon's log goes to the container's output, so `docker logs`
(and Promtail/Loki) see it all: the web UI's log, nginx's requests and errors,
supervisord, and the Activity events. Two levels are set in **⚙ → Logging** (or pinned
by their variables): the daemon's and the web UI's.

**AWG daemon (`amneziawg-go`)** — `off`, `error` (the default: silent unless something
fails) or `debug`, which adds every handshake and "Received message with unknown
type": the only trace of a client with outdated parameters, or of a scanner. The
daemon reads its level when a server starts, so the drawer offers to restart the
running servers. Variable: `AWG_LOG_LEVEL` (`verbose` means `debug`, `silent` means
`off`); it reaches the daemon only, never the panel's own level. The file is
`AWG_LOG_FILE` (default `/var/log/amnezia/amneziawg-go.log`).

Use **⋯ → AWG Logs** on a server card. The log view filters by the selected server interface and shows related “startup banner” lines for that interface.

**Web UI** — always on, with timestamps, levels and module names.
`ERROR`/`WARNING`/`INFO` (default)/`DEBUG`, applied at once. Variable: `LOG_LEVEL`.

```
2026-08-07 17:49:02 INFO    [services.amnezia_manager] Server myvpn started successfully
```

**nginx** — one line per request, with a level from the status (`error` for 5xx,
`warning` for 4xx, `info` otherwise; a 401 is `info`, since a browser's first request
always gets one and failed sign-ins are Activity events). The Docker health check's
`/status`, every 30 s, is left out. nginx's warnings and errors follow in its own
format (a failed sign-in among them); they also stay in `/var/log/nginx/error.log`,
where the panel reads failed sign-ins.

```
2026-10-05T18:04:21+00:00 info 200 POST /api/servers/a1b2c3/stop 192.168.1.50 user=admin bytes=21
2026/10/05 18:04:21 [error] 41#41: *11 user "admin": password mismatch, client: 192.168.1.50, ...
```

**supervisord** — process starts and exits. `reaped unknown pid … (exit status 0)` is
an `amneziawg-go` daemon ending when its server stops: supervisord is the container's
PID 1, which collects every exited process, and the daemon is not one it started.

Docker keeps a container's output without a size limit unless told otherwise; with
`/status` left out it is small (about a hundred lines a day besides the events),
but `--log-opt max-size=10m --log-opt max-file=3` (or `logging:` in Compose) caps it.

**Activity events** — one JSON line each, marked `"src": "awg-webui"` to tell them
from the lines above. Each also carries a
`level` — `error` for `health.problem`, `warning` for `auth.fail` and `egress.change`,
`info` for the rest — while `kind` stays the category:

```
{"src": "awg-webui", "level": "info", "seq": 42, "ts": "2026-10-02T19:32:52Z", "kind": "change", "event": "client.params", "server_id": "abc123", "server": "home", "client_id": "cl1", "client": "iphone", "detail": {"changes": [{"field": "MTU", "old": 1420, "new": 1380}]}}
```

In Grafana, as JSON: `{container="..."} | json | src="awg-webui"`. To read them like
other logs instead, have Promtail rewrite each event into a plain line with a `level`
label (the line is then text, so `| json` no longer applies):

```
awg-webui client.offline server="home" client="iphone" duration=0h50m down=670.70MiB up=21.06MiB
```

```yaml
pipeline_stages:
  - docker: {}
  - json: { expressions: { src: src, level: level, event: event, server: server, client: client, detail: detail, ts: ts } }
  - labels: { src: }
  - match:
      selector: '{src="awg-webui"}'
      stages:
        - labels: { level: }
        - timestamp: { source: ts, format: RFC3339 }
        - template:
            source: message
            template: |-
              awg-webui {{ .event }}
              {{- with .server }} server={{ printf "%q" . }}{{ end }}
              {{- with .client }} client={{ printf "%q" . }}{{ end }}
              {{- $d := .detail | fromJson }}
              {{- if eq .event "client.online" }} endpoint={{ $d.endpoint }} country={{ $d.country }}{{ end }}
              {{- if eq .event "client.offline" }} duration={{ printf "%dh%02dm" (div $d.duration_s 3600) (mod (div $d.duration_s 60) 60) }} down={{ printf "%.2fMiB" (divf $d.sent_bytes 1048576) }} up={{ printf "%.2fMiB" (divf $d.received_bytes 1048576) }}{{ end }}
        - output: { source: message }
```

`down`/`up` are the device's (`sent_bytes` is the server's tx). Other events' `detail`
(`changes`, `problem`, `address`...) can be added to the template the same way. Keep
Promtail's `positions.filename` on persistent storage: if it is lost, Promtail re-reads
the whole container log, and a changed template stores every old event again.

The other lines can take the same shape (`webui [services.amnezia_manager] Server
myvpn stopped successfully`), with `src` and `level` labels, by a second `match` after
the first. Each format starts with its time, which is dropped (Docker's is kept); the
regex has no `$`, because the `docker` stage leaves the line's newline on:

```yaml
  - match:
      selector: '{container="amnezia-web-ui"} |~ "^\\d{4}[-/]\\d\\d[-/]\\d\\d[ T]\\d\\d:"'
      stages:
        - regex:
            expression: '^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (?P<py_lvl>[A-Z]+) +(?P<py_msg>.*)|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (?P<sv_lvl>[A-Z]+) (?P<sv_msg>.*)|\d{4}/\d\d/\d\d \d\d:\d\d:\d\d \[(?P<ng_lvl>[a-z]+)\] \d+#\d+: (?:\*\d+ )?(?P<ng_msg>.*)|\d{4}-\d\d-\d\dT\S+ (?P<ac_lvl>[a-z]+) (?P<ac_msg>.*))'
        - template:
            source: src
            template: '{{ if .py_lvl }}webui{{ else if .sv_lvl }}supervisord{{ else if or .ng_lvl .ac_lvl }}nginx{{ end }}'
        - template:
            source: level
            template: >-
              {{- $l := lower (or .py_lvl .sv_lvl .ng_lvl .ac_lvl) -}}
              {{- if hasPrefix "warn" $l }}warning
              {{- else if hasPrefix "err" $l }}error
              {{- else if has $l (list "crit" "critical" "alert" "emerg") }}critical
              {{- else if hasPrefix "deb" $l }}debug
              {{- else if eq $l "notice" }}info
              {{- else }}{{ $l }}{{ end -}}
        - template:
            source: message
            template: '{{ .src }} {{ or .py_msg .sv_msg .ng_msg .ac_msg }}'
        - labels: { src: , level: }
        - output: { source: message }
```

## 🏗️ Architecture

One container, three processes under supervisord: **nginx** (port 80, Basic Auth,
reverse proxy), the **Flask web UI** (127.0.0.1:5000), and one
**`amneziawg-go`** daemon per VPN interface.

```
web-ui/
├── app.py                      Flask entrypoint, env parsing
├── core/                       request guards (CSRF), live updates (events), settings, runtime wiring, helpers, logging
├── routes/                     servers.py, settings.py, system.py (all /api routes)
├── services/amnezia_manager.py all business logic
├── services/history.py         the traffic history, in memory
├── templates/index.html        page shell
└── static/
    ├── css/style.css           a few styles (tailwind.css is built by build_css.sh)
    ├── vendor/                 qrcode, an unmodified release file
    └── js/  app.js             state, live updates, API calls, page actions
              forms.js          the forms (side drawer), checked by /api/validate
              modals.js         the QR, traffic, logs and full-config views
              charts.js         the traffic charts (SVG)
              ui.js             toasts, dialogs, drawer, menus, inline rename
              server-ui.js      server card and client row rendering
              protocols.js      reads the page config (protocol table, defaults)
              api.js            fetch plumbing
```

State lives in `/etc/amnezia/web_config.json` — the source of truth. WireGuard
`.conf` files under `/etc/amnezia/amneziawg/` are generated from it and never parsed
back. See [DEVELOPMENT.md](DEVELOPMENT.md) for the details.

## 🧩 I1–I5 (Custom Signature Packets)

`I1`–`I5` are custom signature packets sent prior to every handshake. They do not carry actual data, so they only need to be configured on the client side.

- `I1` is the primary packet (if `I1` is empty, the entire I1–I5 chain is skipped).
- `I2`–`I5` are optional follow-up packets (sent in order; empty values are skipped).

Official reference: https://github.com/amnezia-vpn/amneziawg-go#custom-signature-packets

This Web UI treats I1–I5 as **client-only** parameters:

- Server has **default** I1–I5 values (used only when creating *new* clients).
- Each client can override I1–I5 independently (different clients on the same server may have different values).
- Existing clients are **not** modified when server defaults change.
- If an I value is empty, the corresponding `I* = ...` line is **omitted** from generated client configs.

### Tag syntax (quick summary)

I-values are strings composed of tags:

- `<b 0x[hex]>` — static bytes (hex-encoded, e.g. `<b 0xf6ab3267fa>`)
- `<r [size]>` — random bytes (cryptographically secure)
- `<rd [size]>` — random digits (`0-9`)
- `<rc [size]>` — random chars (`a-zA-Z`)
- `<t>` — unix timestamp (4 bytes)

Example: `I1 = <b 0xf6ab3267fa><t><r 10>`

> **Note**: if the final size of any custom signature packet exceeds the system MTU, it may be fragmented, which can look suspicious to DPI.

In the UI:

- **+ Client**: set I1–I5 for a new client, starting from the server's defaults or
  copying another client.
- **Client row → Edit**: change I1–I5 for that client.

### Generate: packets shaped like a protocol

In the client drawer's I1–I5 section, pick a shape and press **Generate**; the fields
fill in and **Save** keeps them (then re-import the client's config on the device).
Nothing is generated for a client by itself: new clients start with no I1–I5.

| Shape | I1 | I2 | I3–I5 |
|---|---|---|---|
| **QUIC Initial** | a QUIC client Initial, 1200–1252 bytes as browsers send it | a second Initial | short packets, as a QUIC connection carries next |
| **DNS query** | an A query for the host (a common name when left empty) | the AAAA query | — |
| **Random** | random bytes and tags | the same | the same |

Everything a real client picks per connection (connection ids, a DNS transaction id
and cookie, the encrypted payload) is a random tag, so it changes on every handshake;
only the protocol's fixed fields are static bytes. A note says when the server's port
is not where the protocol normally goes (UDP 443 for QUIC, 53 for DNS), since a QUIC
packet to port 51820 is less convincing.

The disguise is the packets' shape: Wireshark reads them as QUIC and DNS. It is not a
full imitation: a real QUIC Initial can be decrypted by anyone who sees it, and these
carry random bytes, so a DPI that decrypts Initials finds them broken. The junk packets
(Jc) and the handshake still follow them. For other shapes (TLS, DTLS, SIP), the drawer
links to [AmneziaWG Architect](https://architect.vai-rice.space).

## 📷 QR code notes

WireGuard configs can become too large to fit into a single QR code (especially with long I1–I5 values). When this happens, the QR view shows an error and you should use **Download .conf** instead. The QR carries the config without comments, which keeps it as small as possible.

## 🔧 API Endpoints

All `/api/*` routes sit behind nginx HTTP Basic Auth (the panel's sign-in, see
[Security](#security)), the only credential: the web UI listens on 127.0.0.1 inside the container, so nothing
reaches it around nginx. Scripts send the same Basic Auth (`curl -u`). `API_TOKEN` was
removed in 2.4; a container that still sets it logs a warning and ignores it.

Live updates are no exception: `GET /api/events` is a Server-Sent Events stream (an
ordinary request that stays open), behind the same Basic Auth, which the browser
sends with it as with any request. Until 2.5 they went over Socket.IO, whose
WebSocket handshake iPadOS Safari sent without the credential, so `/socket.io/` had
to be gated by a session cookie instead; that exception is gone, with the cookie.

**Mutating requests must send `Content-Type: application/json`** (anything else gets
`415`). This is what stops another site's page from driving the API using your cached
Basic Auth credentials.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/servers` | everything the page shows: servers with live status, their clients and each client's traffic (bytes and bit/s over the last 7 s, endpoint, handshake age), and a running server's `totals` since its interface came up (`received_bytes`, `sent_bytes`, `since`) |
| GET | `/api/events` | live updates, a Server-Sent Events stream: `traffic_update` (every 7 s per running server: its time `at`, the same per-client shape as `traffic` above, and its `totals`), `server_status` (after a start or stop), `activity` (each new event, as in `/api/activity`), `ping` (every 15 s when idle) |
| GET | `/api/activity` | the Activity events kept in memory: `{"events": [...newest first], "since": "<boot time>"}` |
| GET | `/api/servers/<id>/traffic?range=1h\|6h\|24h` | the traffic history per client, in bit/s: every 7 s for `1h` (the default), per minute for `6h` and `24h`; `null` where there is no data; totals in bytes; `since`, when the history started |
| POST | `/api/servers` | create (`name` required; `protocol`, `port`, `subnet`, `mtu`, `dns`, `endpoint_host`, `auto_start`, `enable_nat`, `block_lan_cidrs`, `transport_params`, `client_defaults`) |
| DELETE | `/api/servers/<id>` | delete server and its clients |
| POST | `/api/servers/<id>/start` \| `/stop` | bring the interface up/down |
| GET | `/api/servers/<id>/info` | summary (status, keys, params, client count) |
| GET | `/api/servers/<id>/config` \| `/config/download` | the generated `.conf` |
| POST | `/api/servers/<id>/transport-params` | protocol + S1–S4 / H1–H4 / `HeaderProtectionKey` / AWG 3.1 toggles; restarts if running |
| POST | `/api/servers/<id>/networking` | NAT / LAN-block toggles; reapplies iptables and, on a running server, re-checks the egress |
| POST | `/api/servers/<id>/rename` | `{"name": "..."}` |
| POST | `/api/servers/<id>/endpoint-host` | `{"endpoint_host": "vpn.example.com"}`: what client configs dial (empty: the detected IP) |
| POST | `/api/servers/<id>/egress-ip` | probe the server's outbound IP (also done by itself after a start or a networking change) |
| POST | `/api/servers/<id>/clients` | add (`name`, optional `client_params`, `copy_from_client_id`, `allowed_ips`) |
| DELETE | `/api/servers/<id>/clients/<cid>` | delete client |
| GET | `/api/servers/<id>/clients/<cid>/config` | download client `.conf` |
| GET | `/api/servers/<id>/clients/<cid>/config-both` | JSON: clean + commented, for QR |
| POST | `/api/servers/<id>/clients/<cid>/client-params` | update Jc/Jmin/Jmax, I1–I5, AWG 3.x timings, and optionally `allowed_ips` |
| POST | `/api/servers/<id>/clients/<cid>/rename` \| `/suspend` | rename, or toggle access |
| POST | `/api/servers/<id>/clients/<cid>/issued` | record that the current config was handed to the device (clears `config_outdated`) |
| GET | `/api/settings` | panel settings: values, where each comes from (`env`, `panel`, `default`), the sign-in's state, versions |
| POST | `/api/settings` | `{"settings": {...}, "access": {"current_password", "user", "password"}}`; nothing applies unless all of it is valid |
| POST | `/api/settings/restart-servers` | restart the running servers (a new daemon log level) |
| POST | `/api/generate` | random parameters for a protocol: `{"protocol", "mtu"}` → `{"transport_params", "client_defaults"}`; saves nothing |
| POST | `/api/validate` | dry-run a form: `{"server": {...}}`, `{"server_id", "protocol", "transport_params", "endpoint_host"}` or `{"server_id", "client_params", "allowed_ips"}` → `{"errors", "warnings"}` (a transport change also gets the re-import counts) |
| GET | `/api/system/status` | health, counts, public IP, supported protocols |
| GET | `/api/system/awg-log` | tail the daemon log (`?interface=&lines=`) |
| POST | `/api/system/refresh-ip` | re-detect the public IP (`502`, nothing changed, when detection fails) |
| GET | `/api/system/iptables-test` | a server's firewall rules as they are now (`?server_id=`); every server's under **⚙ → About → Firewall rules** |
| GET | `/status` | container uptime, plain text (localhost only); a 503 with a line per problem — a server that should be running but is down, a running server whose peers differ from its clients, or whose firewall rules differ from its switches — which the Docker health check turns into `unhealthy` |

Example:

```bash
curl -u admin:pass -H 'Content-Type: application/json' \
  -d '{"name":"My VPN","protocol":"AWG 3.0","subnet":"10.10.0.0/24","port":51820}' \
  http://localhost:8080/api/servers
```

Server-side params (S1–S4, H1–H4, `HeaderProtectionKey`, `RandomTrailers`,
`DisableCookies`) are written to both the server config and every client config, so
changing them means clients must re-import.
Client-side params (Jc, Jmin, Jmax, I1–I5, `ContentPaddingAddition`, timings) are
per-client and only appear in client configs.

## 🐳 Docker Deployment

Official docker image repository: https://hub.docker.com/r/stalker1211/amneziawg15-web-ui

### Environment variables

**Settings.** Each is also in the panel (⚙). While its variable is set (and not
empty) the value is copied into the stored settings at every boot and the field is
read-only; remove the variable and the last value stays, now editable. An invalid
value is logged and ignored. So upgrading changes nothing: the first 2.4 boot copies
today's environment.

| Variable | Default | Setting |
|----------|---------|---------|
| `NGINX_USER` / `NGINX_PASSWORD` | `admin` / `changeme` | The Basic Auth sign-in. Stored hashed in `/etc/amnezia/.htpasswd`; see [Security](#security) |
| `ENABLE_GEOIP` | `1` | Country and city of endpoint, public and egress IPs (asks ipapi.co) |
| `AWG_LOG_LEVEL` | `error` | The VPN daemon's log: `off`, `error`, `debug` |
| `LOG_LEVEL` | `INFO` | The web panel's log |

**Deployment.**

| Variable | Default | Description |
|----------|---------|-------------|
| `NGINX_PORT` | `80` | Port nginx listens on inside the container |
| `WAN_IF` | *(auto)* | Outbound interface for the NAT and forwarding rules; detected from the default route |
| `AWG_LOG_FILE` | `/var/log/amnezia/amneziawg-go.log` | Where the daemon's log goes |

Retired, ignored with a warning at boot: `API_TOKEN` (2.4), `AUTO_START_SERVERS`
(2.5; a restart brings back what was running, and `false` no longer stops that),
`ALLOWED_ORIGINS` (2.5; the live updates no longer use Socket.IO, so a reverse proxy
needs nothing set), and the new-server defaults `DEFAULT_MTU`, `DEFAULT_SUBNET`,
`DEFAULT_PORT`, `DEFAULT_DNS`, `ENABLE_NAT` and `BLOCK_LAN_CIDRS` (2.5; the New
server form starts from your newest server, and each server keeps its own values).
`SYS_MODULE` is not needed: the daemon runs in userspace.

## 🧪 Local build/run (dev)

This repo includes a convenience script that builds and runs a local container:

- `./run.sh` (idempotent; replaces existing container; builds image by default)
- Common overrides:
  - `BUILD=0 ./run.sh` (skip build)
  - `INTERACTIVE=1 ./run.sh` (run interactively)
  - `ENTRYPOINT=/bin/sh INTERACTIVE=1 ./run.sh` (debug shell)

### Image tags

`stalker1211/amneziawg15-web-ui:latest` is the newest build; `:2.6` and so on pin a
release. The version under the page heading (e.g. `v2.6 build 20261001.1`) shows
exactly which one is running.

### Docker Compose Example

```yaml
version: '3.8'
services:
  amnezia-web-ui:
    image: stalker1211/amneziawg15-web-ui:latest
    container_name: amnezia-web-ui
    ports:
      - "8080:8080/tcp"
      - "51820:51820/udp"
    environment:
      - NGINX_PORT=8080
    volumes:
      - amnezia-data:/etc/amnezia
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun
    sysctls:
      - net.ipv4.ip_forward=1
      - net.ipv4.conf.all.src_valid_mark=1
    restart: unless-stopped
volumes:
 amnezia-data:
```

### Docker Run Example

```bash
docker run -d \
  --name amnezia-web-ui \
  --cap-add=NET_ADMIN \
  --sysctl net.ipv4.ip_forward=1 \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --device /dev/net/tun \
  --restart unless-stopped \
  -p 9090:9090 \
  -p 51821:51821/udp \
  -e NGINX_PORT=9090 \
  -v amnezia-data:/etc/amnezia \
  stalker1211/amneziawg15-web-ui:latest
```

## 📊 Obfuscation Parameters

Two kinds, and the distinction matters:

- **Server-side** — must be identical on both ends. Written into the server config
  *and* every client config, so changing one means clients must re-import.
- **Client-side** — may differ per client; only appear in client configs.

| Parameter | Side | Protocol | Notes |
| --- | --- | --- | --- |
| `Jc` | client | 1.5+ | Junk packets sent before each handshake (4–12 typical; 0 sends none) |
| `Jmin` / `Jmax` | client | 1.5+ | Junk packet size range; `Jmin` ≤ `Jmax`, keep `Jmax` < MTU or packets fragment |
| `I1`–`I5` | client | 1.5+ | Custom signature packets (tag syntax above). Empty values are omitted |
| `S1` | server | 1.5+ | Padding of the handshake initiation message. `S1 + 56 ≠ S2` |
| `S2` | server | 1.5+ | Padding of the handshake response message |
| `S3` / `S4` | server | 2.0+ | Padding of the cookie / transport messages |
| `H1`–`H4` | server | 1.5+ | Message header values. 2.0+ also accepts a range (`1200-1400`); ranges must not overlap |
| `HeaderProtectionKey` | server | 3.0+ | Encrypts packet headers. Requires each of S1–S4 ≥ 12 |
| `ContentPaddingAddition` | client | 3.0+ | Extra random bytes per data packet (`10-40`) |
| `RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts` | client | 3.0+ | Override WireGuard's fixed timings; ranges allowed. Empty = protocol default. Keep RekeyAfterTime under 180: the server keeps WireGuard's timers and drops the session at 180 s (the form warns) |
| `RandomTrailers` | server | **3.1** | Appends a random number of bytes to packets; mirrored to both ends. Keep S1–S4 equal with it: otherwise amneziawg-go drops some data packets as false handshakes ([#186](https://github.com/amnezia-vpn/amneziawg-go/issues/186)). Randomize draws them equal while the switch is on, and the form warns with the estimated loss. Without the switch, equal S1–S4 get a warning instead: initiation and response keep WireGuard's 56-byte size difference |
| `DisableCookies` | server | **3.1** | Suppresses handshake cookie replies. Off by default because cookies mitigate handshake floods |
| `MTU` | — | all | Interface MTU (1280–1440) |
| `AllowedIPs` | client | all | What the device sends through the tunnel, set per client: `0.0.0.0/0` (the default) is all IPv4, a narrower list is split tunnelling. Adding `::/0` sends IPv6 in too, where the server drops it and apps fall back to IPv4; a Linux device with IPv6 switched off cannot bring `::/0` up |
| `Endpoint` | server | all | The detected public IP, or the server's **endpoint host** (a DNS name or IPv4). With a dynamic DNS name, a new public IP needs no re-import |

What hides what: **I1–I5** (a protocol's shape) and **Jc/Jmin/Jmax** (random junk,
different every handshake) precede the handshake; **S1–S3** and **H1–H3** disguise the
handshake messages; **S4** and **H4** (and on AWG 3.x `ContentPaddingAddition`) every data
packet; AWG 3.x **header protection** and 3.1 **random trailers** both; the 3.x timings
when rekeys happen. I1–I5 and Jc work together: the I-packets give the first packets a
familiar shape, and Jc keeps the burst before each handshake from being the same sizes
every time. The UI shows only the fields the selected
protocol supports and validates the constraints above as you type: I1–I5 tags as
the daemon parses them, uint16/uint32 bounds, and warnings for equal message sizes,
H values in WireGuard's own 1–4 (without header protection), AWG 3.x timers that
fight each other, and AWG 3.x without a header protection key (it then works like
2.0).

Every new server gets its own random parameters, drawn by the server
(`/api/generate`, ported from [AmneziaWG Architect](https://github.com/Vadim-Khristenko/Any-Tech-ARCHITECT)):
four disjoint H ranges under 2³¹−1, S sizes that never make two message types the
same length, a small junk train (Jc 4–12, Jmax ≤ 160), and on AWG 3.x a header
protection key, content padding and timers that keep WireGuard's timer rules. With a
key, H stays four custom ranges: docs.amnezia.org suggests 1–4 there, since the cipher
hides the message type, and both work. **Randomize** draws a fresh set. In a client's
drawer, each group of client-side parameters has its own **Generate**: the junk packets
(Jc, Jmin, Jmax), I1–I5 (below), and on AWG 3.x the timers and content padding. One
group is redrawn without touching the others; Save keeps the values. All of them are
drawn by the server, and nothing is generated for a client unless you press Generate.

## 🔍 Logs, backup and debugging

```bash
# Web UI, nginx, supervisord and the Activity events, together
docker logs -f amnezia-web-ui
docker logs amnezia-web-ui 2>&1 | grep -E ' (ERROR|WARNING) |\[(error|warn)\]| (error|warning) [0-9]{3} '

# The AWG daemon's log (also ⋯ → AWG Logs)
docker exec amnezia-web-ui tail -f /var/log/amnezia/amneziawg-go.log

# Backup: config plus every generated .conf
docker cp amnezia-web-ui:/etc/amnezia ./amnezia-backup/

# Live VPN state, straight from the daemon
docker exec amnezia-web-ui awg show
docker exec amnezia-web-ui awg showconf wg-<server-id>

# Health and firewall checks
docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' amnezia-web-ui   # why unhealthy
curl -u admin:pass http://localhost:8080/api/system/status
curl -u admin:pass "http://localhost:8080/api/system/iptables-test?server_id=<server-id>"
```

Server ids are 6 characters (e.g. `a1b2c3`); the interface is `wg-<id>`. Restore by
putting `/etc/amnezia` back and restarting the container — the servers that were
running come back up on their own. The backup holds the settings and the hashed
sign-in (`.htpasswd`) too.

# Security
The app is exposed directly on 80 or custom port with basic authentication.

> [!IMPORTANT]
> I strongly recommend protecting endpoints with firewall and/or nginx authentication.
> Basic auth alone is not strong enough and can be bruteforced.

A new volume signs in with `admin` / `changeme`, and the panel shows a **Default
password** banner (and the log a warning) until it is changed. Change it in
**⚙ → Access** (the current password is required); it is stored hashed (SHA-512 crypt)
in `/etc/amnezia/.htpasswd`, so it survives image updates. The tab that saves it stays
signed in; every other browser is asked for the new password. Setting `NGINX_PASSWORD` instead pins it (the field turns read-only),
which also works as a recovery: set it, restart, sign in, remove it. Prefer the
drawer for a compose file kept in git.

> [!NOTE]
> There is no possibility to protect the built-in nginx with allow ip rule, because when run in docker with bridge mode docker doesn't pass the real client ip into the container. External proxy or additional container is required to perform client ip check.

# Support
The NO support provided as well as no regular updates are planned. Found issues can be fixed if free time permits.
