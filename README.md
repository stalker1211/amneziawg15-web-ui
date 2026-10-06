# AmneziaWG Web UI

A web panel for [AmneziaWG](https://github.com/amnezia-vpn/amneziawg-go): WireGuard
with obfuscation that DPI cannot easily recognise. Servers, clients and their configs
are fully managed from the browser with a clean GUI: live traffic per client, history
for 1, 6 and 24 hours, configs as QR codes, a mark on every device whose config is
out of date, an activity log, and the reason a device cannot connect. All in one
Docker container.

Current version: **2.8**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="screenshot-light.png">
  <img src="screenshot.png" alt="Web UI screenshot" width="50%"/>
</picture>

## 🚀 Quick start

```bash
docker run -d --name amnezia-web-ui \
  --cap-add NET_ADMIN --device /dev/net/tun \
  --sysctl net.ipv4.ip_forward=1 \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  -p 8080:80 -p 51820:51820/udp \
  -v amnezia-data:/etc/amnezia \
  --restart unless-stopped \
  stalker1211/amneziawg15-web-ui:latest
```

Open `http://<host>:8080` (`admin` / `changeme`), change the password in **⚙ → Access**,
add a server. The first server gets port 51820; publish a UDP port for each further
one. Compose and variables: [Docker Deployment](#-docker-deployment).

## ✨ Features

- Servers and clients: create, start/stop, rename, suspend, delete
- Configs as QR code, `.conf` or text
- AWG 1.5, 2.0, 3.0, 3.1; random parameters per server
- I1–I5 packets shaped like QUIC or DNS
- Live traffic per client; graphs for 1 h, 6 h, 24 h; server totals
- Re-import marks for outdated configs
- Old config / Maybe blocked: why a device can't connect (experimental)
- Activity log, also in `docker logs`
- Docker health check on drift
- Split tunnelling, DNS endpoint, NAT, LAN blocking
- Panel settings (sign-in, log levels, GeoIP) in **⚙**; any can be pinned by a variable
- No CDN, dark theme, phone width

## 🩺 Connection analyzer (experimental)

Says why a client's device cannot connect, on the client's row. Experimental: tested
against real daemons in containers, not yet over time on real networks, so a verdict
may be missed or wrong.

- **Old config** (red): the device knocks with parameters the server no longer uses
  (S1, H1, the header protection key, random trailers). The panel reads the handshake
  with the server's key, so it names the client even when its Re-import mark was
  cleared by a QR the device never scanned.
- **Maybe blocked** (orange): the server reads and answers the device's handshakes,
  and none completes, so packets are lost on the way.

The pill opens the evidence: what the device sent against what the server expects,
from where, since when. A verdict ends when the device connects.

Off by default, per server: switch it on in the server's **⚙ → Networking →
Connection analyzer**. The radar icon after the status pill shows whether it is on.
It reads only a failing device's handshake packets (a capture of at most 10 s), never
the tunnel's traffic; idle, it costs about 0.2 % of a CPU core. It needs `CAP_NET_RAW`,
which Docker grants by default. A device whose packets never reach the server looks
switched off, and is not detected.

### What it can and cannot see

The analyzer reads the device's **handshake initiation**, the first packet it sends,
so it sees only the parameters that packet carries. The other server-side parameters
pad or label packets it does not read: the server's response and cookie reply travel
to the device, and data packets are not inspected. A device left on an old value of
one of those is diagnosed less precisely, or not at all:

| Parameter on the device differs | What happens to the device | What the panel shows |
| --- | --- | --- |
| `S1`, `H1`, `HeaderProtectionKey`, `RandomTrailers` | The server cannot read its handshake and never answers | **Old config**, naming the parameter |
| `S2`, `H2` | The server answers, the device cannot read the answer and keeps retrying | **Maybe blocked**; the dialog adds that the server's parameters changed after its last handshake |
| `S4`, `H4` | The handshake completes on the device, but the server drops all its data, so nothing passes | **Nothing** (at most, rarely, Maybe blocked) |
| `S3`, `H3` | Nothing while the server is idle: a cookie reply is sent only under a handshake flood | Nothing; under load the device may fail to connect undiagnosed |
| `Jc`, `Jmin`, `Jmax`, `I1`–`I5`, 3.0's client timings and padding | Nothing: they live only in the client's config, and the server accepts any | Nothing (there is nothing to diagnose) |

In practice the gap is narrow: **Randomize** redraws S1–S4 and H1–H4 together, so a
change made with it always moves S1 and H1, and a device left behind shows as **Old
config**. Only a hand edit of S2–S4 or H2–H4 alone is diagnosed as above. In that case
the client's **Re-import** mark still flags it, unless the config was shown again
since (a QR displayed counts as issued, scanned or not). The Activity log keeps every
parameter change, old → new.

## 📈 Traffic and Activity

Download (↓) and upload (↑) are from the device's side. The traffic history lives in
the panel's memory (every 7 s for an hour, per minute for a day), so a restart starts
it over. A server's totals are its interface's kernel counters since it started, so
clients deleted since still count.

Activity lists what happened, newest first: changes to servers, clients and settings
(old → new, never a key), clients online and offline, Old config / Maybe blocked and
recovery, health problems, egress changes, failed sign-ins. Open it from the header's
clock button, or a server's ⋯ for that server alone. The last ~1000 are kept in memory;
each is also a JSON line in `docker logs` ([Logging](#-logging)).

## 📊 Obfuscation Parameters

- **Server-side**: identical on both ends, written into the server config *and* every
  client config, so changing one means clients must re-import. The server settings
  say how many devices a change affects before you save.
- **Client-side**: may differ per client; only in client configs.

| Parameter | Side | Protocol | Notes |
| --- | --- | --- | --- |
| `Jc` | client | 1.5+ | Junk packets sent before each handshake (4–12 typical; 0 sends none) |
| `Jmin` / `Jmax` | client | 1.5+ | Junk packet size range; `Jmin` ≤ `Jmax`, keep `Jmax` < MTU or packets fragment |
| `I1`–`I5` | client | 1.5+ | Custom signature packets (below). Empty values are omitted |
| `S1` | server | 1.5+ | Padding of the handshake initiation message. `S1 + 56 ≠ S2` |
| `S2` | server | 1.5+ | Padding of the handshake response message |
| `S3` / `S4` | server | 2.0+ | Padding of the cookie / transport messages |
| `H1`–`H4` | server | 1.5+ | Message header values. 2.0+ also accepts a range (`1200-1400`); ranges must not overlap |
| `HeaderProtectionKey` | server | 3.0+ | Encrypts packet headers. Requires each of S1–S4 ≥ 12 |
| `ContentPaddingAddition` | client | 3.0+ | Extra random bytes per data packet (`10-40`) |
| `RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts` | client | 3.0+ | Override WireGuard's fixed timings; ranges allowed. Empty = protocol default. Keep RekeyAfterTime under 180: the server drops the session at 180 s (the form warns) |
| `RandomTrailers` | server | **3.1** | Appends a random number of bytes to packets. Keep S1–S4 equal with it, or amneziawg-go drops some data packets as false handshakes ([#186](https://github.com/amnezia-vpn/amneziawg-go/issues/186)); Randomize draws them equal and the form warns. Without it, equal S1–S4 get a warning instead |
| `DisableCookies` | server | **3.1** | Suppresses handshake cookie replies. Off by default because cookies mitigate handshake floods |
| `MTU` | — | all | Interface MTU (1280–1440) |
| `AllowedIPs` | client | all | What the device sends through the tunnel: `0.0.0.0/0` (the default) is all IPv4, a narrower list is split tunnelling. `::/0` sends IPv6 in too, where the server drops it |
| `Endpoint` | server | all | The detected public IP, or the server's **endpoint host** (a DNS name or IPv4). With a dynamic DNS name, a new public IP needs no re-import |

What hides what: **I1–I5** (a protocol's shape) and **Jc/Jmin/Jmax** (random junk)
precede the handshake; **S1–S3** and **H1–H3** disguise the handshake messages; **S4**,
**H4** and on 3.x `ContentPaddingAddition` every data packet; 3.x **header protection**
and 3.1 **random trailers** both. The form shows only the selected protocol's fields
and checks them as you type.

Every new server draws its own random parameters (ported from
[AmneziaWG Architect](https://github.com/Vadim-Khristenko/Any-Tech-ARCHITECT)): four
disjoint H ranges, S sizes that never make two message types the same length, a small
junk train, and on 3.x a header protection key, content padding and timers.
**Randomize** draws a fresh set. In a client's drawer each group (junk, I1–I5, 3.x
timers and padding) has its own **Generate**. Nothing is saved until **Save**, and
nothing is generated for a client by itself.

### I1–I5 (custom signature packets)

Sent before every handshake, client-side only. `I1` is the primary packet (empty `I1`
skips the chain); `I2`–`I5` follow in order. The server's I1–I5 are defaults for new
clients only; changing them leaves existing clients alone.
[Reference](https://github.com/amnezia-vpn/amneziawg-go#custom-signature-packets).

Tags: `<b 0x[hex]>` static bytes, `<r [size]>` random bytes, `<rd [size]>` random
digits, `<rc [size]>` random letters, `<t>` unix timestamp (4 bytes). Example:
`I1 = <b 0xf6ab3267fa><t><r 10>`. A packet larger than the MTU fragments, which can
look suspicious to DPI.

**Generate** fills them with a shape:

| Shape | I1 | I2 | I3–I5 |
|---|---|---|---|
| **QUIC Initial** | a QUIC client Initial, 1200–1252 bytes as browsers send it | a second Initial | short packets, as a QUIC connection carries next |
| **DNS query** | an A query for the host (a common name when left empty) | the AAAA query | — |
| **Random** | random bytes and tags | the same | the same |

What a real client picks per connection (connection ids, a DNS transaction id) is a
random tag, so it changes on every handshake. Wireshark reads the packets as QUIC and
DNS, but a DPI that decrypts QUIC Initials finds them broken. A note says when the
server's port is not the protocol's usual one (UDP 443, 53). For other shapes the
drawer links to [AmneziaWG Architect](https://architect.vai-rice.space).

Long I1–I5 can make a config too large for one QR code; then use **Download .conf**.

## 🐳 Docker Deployment

Image: https://hub.docker.com/r/stalker1211/amneziawg15-web-ui. `:latest` is the
newest build, `:2.8` and so on pin a release; the version under the page heading shows
which one runs. A plain `docker run` is in [Quick start](#-quick-start).

### Environment variables

**Settings**, each also in **⚙**. A set variable is copied into the stored settings at
every boot and makes the field read-only; remove it and the last value stays, editable.
An invalid value is logged and ignored.

| Variable | Default | Setting |
|----------|---------|---------|
| `NGINX_USER` / `NGINX_PASSWORD` | `admin` / `changeme` | The Basic Auth sign-in, stored hashed in `/etc/amnezia/.htpasswd`; see [Security](#security) |
| `ENABLE_GEOIP` | `1` | Country and city of endpoint, public and egress IPs (asks ipapi.co) |
| `AWG_LOG_LEVEL` | `error` | The VPN daemon's log: `off`, `error`, `debug` |
| `LOG_LEVEL` | `INFO` | The web panel's log, and which requests nginx logs |
| `TRUSTED_PROXIES` | `172.17.0.0/16` | Reverse proxies whose `X-Forwarded-For` names the client (IPs or CIDRs, comma-separated; empty trusts none) |

**Deployment.**

| Variable | Default | Description |
|----------|---------|-------------|
| `NGINX_PORT` | `80` | Port nginx listens on inside the container |
| `WAN_IF` | *(auto)* | Outbound interface for NAT and forwarding; detected from the default route |
| `AWG_LOG_FILE` | `/var/log/amnezia/amneziawg-go.log` | Where the daemon's log goes |

Retired, ignored with a warning: `API_TOKEN`, `AUTO_START_SERVERS`, `ALLOWED_ORIGINS`,
`DEFAULT_MTU`, `DEFAULT_SUBNET`, `DEFAULT_PORT`, `DEFAULT_DNS`, `ENABLE_NAT`,
`BLOCK_LAN_CIDRS` (a new server starts from your newest one). `SYS_MODULE` is not
needed: the daemon runs in userspace.

### Docker Compose

```yaml
services:
  amnezia-web-ui:
    image: stalker1211/amneziawg15-web-ui:latest
    container_name: amnezia-web-ui
    ports:
      - "8080:80/tcp"
      - "51820:51820/udp"
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

### Local build

`./run.sh` builds the image and (re)starts a local container. `BUILD=0` skips the
build, `INTERACTIVE=1` runs it in the foreground, `ENTRYPOINT=/bin/sh INTERACTIVE=1`
gives a shell.

## 📝 Logging

Everything but the AWG daemon's log goes to the container's output, so `docker logs`
(and Promtail/Loki) see it all: the web UI, nginx, supervisord and the Activity events.
Both levels are in **⚙ → Logging**.

**AWG daemon** (`amneziawg-go`): `off`, `error` (the default) or `debug`, which adds
every handshake and "Received message with unknown type" (a client with outdated
parameters, or a scanner). Read when a server starts, so the drawer offers to restart
the running ones. `AWG_LOG_LEVEL` (`verbose` = `debug`, `silent` = `off`); the file is
`AWG_LOG_FILE`. Shown per server in **⋯ → AWG Logs**.

**Web UI**: `ERROR`/`WARNING`/`INFO` (default)/`DEBUG`, applied at once. `LOG_LEVEL`.

```
2026-08-07 17:49:02 INFO    [services.amnezia_manager] Server myvpn started successfully
```

**nginx**: one line per request, its level from the status (`error` 5xx, `warning`
other 4xx, `debug` the rest and 401s, which every browser gets first). Which requests
are logged follows the Web UI level:

| Web panel level | Requests logged |
|---|---|
| Debug | every request |
| Info, Warnings | the failed ones: 4xx (but 401) and 5xx |
| Errors | 5xx |

`/static/` and the health check's `/status` are never logged. nginx's own warnings and
errors follow in its format, and stay in `/var/log/nginx/error.log` too.

```
2026-10-05T18:04:21+00:00 debug 200 POST /api/servers/a1b2c3/stop 192.168.1.50 user=admin bytes=21
2026/10/05 18:04:21 [error] 41#41: *11 user "admin": password mismatch, client: 192.168.1.50, ...
```

**Behind a reverse proxy**, every request comes from the proxy's address. List it in
**⚙ → Access → Trusted proxies** (`TRUSTED_PROXIES`) and nginx takes the client from
`X-Forwarded-For`. Trust only proxies that set that header themselves. Saving reloads
nginx; an open page reconnects within ~10 s.

**supervisord**: process starts and exits. `reaped unknown pid … (exit status 0)` is a
daemon ending when its server stops.

Docker keeps a container's output unbounded unless told otherwise (about a hundred
lines a day besides the events); `--log-opt max-size=10m --log-opt max-file=3` caps it.

**Activity events**: one JSON line each, `"src": "awg-webui"`, with a `level`: `error`
for `health.problem`; `warning` for `auth.fail`, `egress.change`, `client.old_config`
and `client.maybe_blocked` (so a Loki alert can catch a device that cannot connect);
`info` for the rest.

```
{"src": "awg-webui", "level": "info", "seq": 42, "ts": "2026-10-02T19:32:52Z", "kind": "change", "event": "client.params", "server_id": "abc123", "server": "home", "client_id": "cl1", "client": "iphone", "detail": {"changes": [{"field": "MTU", "old": 1420, "new": 1380}]}}
```

In Grafana: `{container="..."} | json | src="awg-webui"`. Or have Promtail rewrite each
event into a plain line with a `level` label:

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

`down`/`up` are the device's (`sent_bytes` is the server's tx). Keep Promtail's
`positions.filename` on persistent storage, or a lost one re-reads the whole log.

The other lines take the same shape (`webui [services.amnezia_manager] Server myvpn
stopped successfully`) with a second `match` after the first (the regex has no `$`: the
`docker` stage leaves the newline on):

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

## 🔧 API Endpoints

Every `/api/*` route is behind nginx Basic Auth ([Security](#security)); the web UI
listens on 127.0.0.1 only, so nothing reaches it around nginx. Scripts use the same
credential (`curl -u`). **Mutating requests must send `Content-Type:
application/json`** (`415` otherwise): this stops another site's page from driving the
API with your cached credential.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/servers` | everything the page shows: servers with live status, their clients and each client's traffic (bytes and bit/s over the last 7 s, endpoint, handshake age, `diagnosis`), and a running server's `totals` since its interface came up (`received_bytes`, `sent_bytes`, `since`) |
| GET | `/api/events` | live updates, a Server-Sent Events stream: `traffic_update` (every 7 s per running server: its time `at`, the same per-client shape as `traffic` above, and its `totals`), `server_status` (after a start or stop), `activity` (each new event, as in `/api/activity`), `ping` (every 15 s when idle) |
| GET | `/api/activity` | the Activity events kept in memory: `{"events": [...newest first], "since": "<boot time>"}` |
| GET | `/api/servers/<id>/traffic?range=1h\|6h\|24h` | the traffic history per client, in bit/s: every 7 s for `1h` (the default), per minute for `6h` and `24h`; `null` where there is no data; totals in bytes; `since`, when the history started |
| POST | `/api/servers` | create (`name` required; `protocol`, `port`, `subnet`, `mtu`, `dns`, `endpoint_host`, `auto_start`, `enable_nat`, `block_lan_cidrs`, `connection_analyzer`, `transport_params`, `client_defaults`) |
| DELETE | `/api/servers/<id>` | delete server and its clients |
| POST | `/api/servers/<id>/start` \| `/stop` | bring the interface up/down |
| GET | `/api/servers/<id>/info` | summary (status, keys, params, client count) |
| GET | `/api/servers/<id>/config` \| `/config/download` | the generated `.conf` |
| POST | `/api/servers/<id>/transport-params` | protocol + S1–S4 / H1–H4 / `HeaderProtectionKey` / AWG 3.1 toggles; restarts if running |
| POST | `/api/servers/<id>/networking` | NAT / LAN-block / `connection_analyzer`; a NAT or LAN change reapplies iptables and, on a running server, re-checks the egress |
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

```bash
curl -u admin:pass -H 'Content-Type: application/json' \
  -d '{"name":"My VPN","protocol":"AWG 3.0","subnet":"10.10.0.0/24","port":51820}' \
  http://localhost:8080/api/servers
```

## 🏗️ Architecture

One container, three kinds of process under supervisord: **nginx** (Basic Auth,
reverse proxy), the **Flask web UI** (127.0.0.1:5000), and one **`amneziawg-go`**
daemon per server. State lives in `/etc/amnezia/web_config.json`; the WireGuard
`.conf` files under `/etc/amnezia/amneziawg/` are generated from it, never parsed back.

```
web-ui/
├── app.py          Flask entrypoint
├── core/           request guards, live updates, settings, logging
├── routes/         the /api routes
├── services/       the manager, traffic history, Activity, Connection analyzer
├── templates/      page shell
└── static/js/      the page: state, forms, dialogs, charts
```

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

Server ids are 6 characters (e.g. `a1b2c3`); the interface is `wg-<id>`. To restore,
put `/etc/amnezia` back and restart the container: the servers that were running come
back up. The backup holds the settings and the hashed sign-in too.

# Security

The panel is exposed on its port behind HTTP Basic Auth.

> [!IMPORTANT]
> Basic Auth alone can be brute-forced. Protect the port with a firewall or a reverse
> proxy of your own.

A new volume signs in with `admin` / `changeme`, and the panel shows a **Default
password** banner until it is changed in **⚙ → Access** (the current password is
required). It is stored hashed (SHA-512 crypt) in `/etc/amnezia/.htpasswd` and survives
image updates; other browsers are asked for the new one. `NGINX_PASSWORD` pins it
instead (the field turns read-only), which also works as a recovery: set it, restart,
sign in, remove it.

> [!NOTE]
> The built-in nginx cannot filter by client IP: in Docker's bridge mode the
> container does not see the real address. Do it in a reverse proxy in front.

# Support

No support is provided and no regular updates are planned. Issues may be fixed if time
permits.
