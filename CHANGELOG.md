# CHANGELOG

## Version 2.7.2 (2026-10-05)

### Logging
- **nginx's request lines follow the Web panel level** (⚙ → Logging, applied at once):
  Debug logs every request, Info and Warnings only the failed ones (4xx but 401, and
  5xx), Errors only 5xx. A successful request (and a 401) is now `debug`, not `info`.
- **`/static/` is no longer logged** (a dozen lines per page load), beside `/status`.

### Access
- **Trusted proxies** (⚙ → Access, or `TRUSTED_PROXIES`): the reverse proxies whose
  `X-Forwarded-For` names the client, so the request lines and failed sign-ins show who
  connected rather than the proxy. Default `172.17.0.0/16`, Docker's default bridge (as
  before, when it was fixed); a `/0` is refused. Saving reloads nginx; an open page
  reconnects within ~10 s.
- **`/status` answers only the container itself:** a trusted proxy forwarding
  `X-Forwarded-For: 127.0.0.1` used to pass its localhost-only rule.

## Version 2.7.1 (2026-10-05)

### Logging
- **The whole log in `docker logs`:** the web UI's log, nginx's requests and errors
  and supervisord now go to the container's output beside the Activity events, so
  Promtail/Loki (or any log driver) see the panel, not only its events. Before, all but
  the events stayed in files inside the container, lost when it was re-created. The
  AWG daemon's log stays in its file, where ⋯ → AWG Logs reads it.
- **nginx's access lines carry a level** (`error` for 5xx, `warning` for 4xx, `info`
  otherwise and for a 401), and the Docker health check's `/status`, 95 % of the old
  access log, is left out. nginx's error log also stays in its file, where the panel
  reads failed sign-ins.
- **No `CRIT Supervisor is running as root` at every start** (`user=root` is set).
- The README shows a Promtail `match` that rewrites all of these into one shape with
  `src` and `level` labels, like the events.
- Removed: `/var/log/webui/` and `/var/log/supervisor/` (nothing writes there now).

### Build
- **Every Python package pinned:** `requirements.txt` is now generated
  (`uv pip compile`) from `requirements.in` and pins Flask's six dependencies too, so
  a rebuild never changes a version by itself and the image runs the versions the
  tests ran. MarkupSafe 3.0.3 → 3.0.4.

## Version 2.7 (2026-10-02)

### Activity
- **The Activity view:** what happened in the panel, newest first, with a heading per
  day. Opened from the header's clock button, or from a server's ⋯ (then only that
  server, which a select changes; deleted servers stay listed). Filters: All, Changes,
  Sessions, Health and Sign-ins, each with its count. New events arrive live.
- **What is recorded:** *changes*: a server created, deleted, started, stopped,
  renamed or edited, a client added, deleted, suspended, resumed, renamed or edited,
  the settings and the sign-in changed, with old → new for short values (MTU, port,
  DNS, AllowedIPs, S/H/J, the timers) and field names alone for the rest, never a
  key, a password or I1–I5's content. *Sessions*: a client online (endpoint, and
  country with GeoIP on) and offline (how long, bytes each way). *Health*: a problem
  that turns the container unhealthy appearing or clearing, and a server's egress IP
  changing. *Sign-ins*: failed sign-ins from nginx's log, one entry per address per
  minute with a count and the user typed. Kept in memory (the last ~1000); a restart
  starts the list over.
- **Events in `docker logs`:** each event is also one JSON line on the container's
  output, marked `"src": "awg-webui"`, beside supervisord's own lines; the panel's
  log stays in its files. Loki/Grafana: `{container="..."} | json | src="awg-webui"`.
  In the API: `GET /api/activity` (`events`, newest first, and `since`, the boot), and
  an `activity` event on the live stream.
- **A level on each event in `docker logs`:** the JSON line also carries `level` —
  `error` for `health.problem`, `warning` for `auth.fail` and `egress.change`, `info`
  for the rest — so Grafana colours the events like any other log instead of showing
  them as unknown. `kind` stays the category; the panel and `/api/activity` are
  unchanged.
- **Online means a handshake within 300 s**, for every protocol, on the page, in the
  history and in the session events.

### Parameters
- **Random trailers without packet loss:** with trailers on and S1–S4 different, the
  daemon takes some data packets for handshakes and drops them (amneziawg-go#186;
  28 % of pings in a measured case). Randomize with the trailers switch on now draws
  one S for all four (15–32), and the form warns when they differ, with an estimate
  of the share lost. The reverse warns too: equal S1–S4 without trailers keep
  WireGuard's handshake size gap. Existing servers are left as they are.
- **A timer warning:** an AWG 3.x client's RekeyAfterTime at 180 s or more warns. The
  server keeps WireGuard's timers and drops the session at 180 s, so traffic stops for
  about 15 s until the client handshakes again. Generate never draws one.

### Interface
- **Names:** a server's ⋯ "Logs" reads "AWG Logs", and the settings' "VPN daemon" log
  level reads "AWG daemon".

### Image
- **No git in the provenance:** a published image no longer names the build's git
  remote and commit in its provenance attestation. Tags pushed before keep theirs
  until rebuilt.

### Tooling
- **The release checker** adds an AWG 3.1 server with random trailers and Generate's
  S1–S4, through which 300 back-to-back pings at 56 and 1100 bytes must lose none,
  and its Activity smoke check runs on its own data.

## Version 2.6 (2026-10-01)

### Traffic
- **Server totals:** beside a running server's traffic graph, a box with what it has
  carried since it started (↓ and ↑) and when that was ("since 09:14", or a date; the
  uptime on hover). They are the kernel's counters for the server's interface, read
  every 7 s with nothing stored, so a client suspended or deleted since still counts,
  and a stop and start (or a change that restarts the server) begins them again. They
  leave out WireGuard's framing, so they run a little under the sum of the clients'
  rows, more so for upload (mostly small packets). In the API: `totals` on each server
  in `/api/servers` and in each `traffic_update`, `null` for a stopped server. A click
  on the box opens the Traffic view, as a click on the graph does.

### Interface
- **Notices readable in the light theme:** the default-password banner, the drawer's
  callouts and "Worth a look" (amber), the "things to fix" boxes (red) and the QR view's
  re-import notice (rose) were too pale on white; they are a shade stronger. The dark
  theme is unchanged.

### Signature packets
- **Generate I1–I5 from a profile:** the client drawer's I1–I5 section has a shape
  (QUIC Initial, DNS query, Random), a host for DNS, and **Generate**, which fills the
  fields; Save keeps them. QUIC: two client Initials of 1200–1252 bytes (RFC 9000's
  floor, which servers enforce, and what browsers send), then three short-header
  packets. DNS: an A and an AAAA query for the host, with EDNS0 and a client cookie.
  Connection ids, transaction ids, cookies and payload are random tags, redrawn before
  every handshake, rather than static bytes that would repeat from one handshake to
  the next. A note says when the server's port does not match the protocol (443 for
  QUIC, 53 for DNS). New and existing clients are left as they are. In the API:
  `POST /api/generate` with `signature_profile`, `server_id` and `host` answers
  `signature_packets`, `signature_host` and `signature_notes`.

### Client parameters
- **Generate per group:** the client drawer's junk row (Jc, Jmin, Jmax) and, on AWG 3.x,
  its timers and content padding each have a **Generate**, beside the one for I1–I5. Each
  redraws its own group only, from the server's generator (timers that keep WireGuard's
  rules, Jmax at least 64 above Jmin); nothing is saved until Save. A server switched to
  AWG 3.x keeps no 3.x client defaults of its own, so its new clients get timers this way.

### Tooling
- **The release checker, `tests/release_check.py`**: the image from the tree, checked
  end to end in one command (about 3 minutes). It builds the image, checks it carries the tree, boots it with no
  bind mount, and checks the sign-in, a server per protocol, a restart, a real client
  tunnel to every server (its `.conf` as issued, in a second container, with I1–I5 from
  Generate) and the smoke tests. It reports and publishes nothing.
- **Local Docker only:** `run.sh`, `publish_dockerhub.sh` (when it builds) and the
  release checker say which Docker they use and stop unless it is a local socket, so a
  Docker context or `DOCKER_HOST` pointing at another host cannot build or run there.

## Version 2.5 (2026-10-01)

Fewer moving parts. Upgrading needs no action, with one change to note: Block LAN
now keeps a server's clients off the panel too (below). `ALLOWED_ORIGINS` and the
new-server defaults (`DEFAULT_MTU` and the rest) can be dropped from a compose file;
one that still sets them, or `AUTO_START_SERVERS`, logs a warning. Reload any panel
tab left open across the upgrade: the old page keeps retrying `/socket.io/`, which
now asks for the password (on an iPad, a prompt). The health check is stricter (see
Health): an `unhealthy` container after the upgrade names what drifted in
`docker inspect`.

### Traffic history and graphs
- **The last hour at a glance:** each running server's card has a band with its last
  hour (download above a line, upload below) and its rates now; each online client's
  row has, under its ↓/↑ totals, its last hour as a sparkline and its rate now; the
  status strip has a fourth tile with every running server's traffic.
- **A Traffic view per server** (the band, or Traffic in ⋯): 1 h, 6 h or 24 h; the
  totals and the peak; a row per client with when it was online, suspended or offline
  on the same time axis, and its totals. The crosshair's readout lists every client
  with traffic at that moment; hovering a client draws its share, a click (or a tap)
  keeps it. On a phone, a drag moves the crosshair.
- **Kept in memory:** the 7 s loop already reads every peer's counters, so the panel
  keeps every 7 s for the last hour and a per-minute sum for 24 hours, without writing
  anything. A restart of the panel or the container starts the history over. A day
  of 50 busy clients holds about 17 MB.
- **↓ and ↑ are from the device's side:** ↓ is what the server sent the device (its
  download), ↑ what it received. Until 2.5 the rows' ↓ showed the server's received
  bytes, the device's upload. The API keeps the daemon's `received_*`/`sent_*`, and
  `api_status.py` still prints awg's rx and tx.
- **API:** each client's `traffic` gains `received_bps` and `sent_bps` (over the last
  tick), `traffic_update` gains `at`, and `GET /api/servers/<id>/traffic?range=1h|6h|24h`
  returns the history.

### Live updates: Server-Sent Events instead of Socket.IO
- **One auth model.** The page's live updates are `GET /api/events`, a stream the
  server keeps open (`text/event-stream`), behind nginx's Basic Auth like every other
  request. The browser sends its cached sign-in with it, which iPadOS Safari did not
  do for Socket.IO's WebSocket handshake; so `/socket.io/` and its exception go: the
  year-long session cookie, the secret key on the volume (`.flask_secret_key`, removed
  at the first boot) and its rotation on a password change.
- **`ALLOWED_ORIGINS` is retired:** the stream is same-origin like any request, so a
  reverse proxy needs nothing set. A set one logs a warning.
- **The browser reconnects by itself:** `app.js` traded 174 lines of Socket.IO
  reconnect machinery for 80 of stream handling. A hidden tab closes its stream and opens a new one with a resync
  when shown (on a plain-HTTP address the browser allows six connections per host,
  and a stream holds one). A stream silent for 45 s is reopened: the server sends a
  `ping` every 15 s when idle. A 401 on the stream (the password changed in another
  browser) leads to the sign-in once, not in a loop.
- **Fewer dependencies:** `flask-socketio`, `python-socketio`, `python-engineio`,
  `simple-websocket`, `wsproto`, `h11` and `bidict` are gone, and the vendored
  `socket.io` client (150 KB) with them. nginx proxies no WebSocket any more.
- Flask runs on Werkzeug's threaded server, as Flask-SocketIO ran it: one thread per
  request and per open stream.

### Outbound HTTPS on the standard library
- **The public IP, the egress probe and GeoIP** use one small helper over Python's
  `http.client` instead of `requests`, so `requests`, `urllib3`, `idna`,
  `charset-normalizer` and `certifi` are gone: 73 packages in the image, from 85 in
  2.4. Certificates are checked against Alpine's `ca-certificates`, renewed by every
  image build, not a bundle frozen at a pin.
- **`scripts/api_status.py` needs nothing installed** (standard library only, still
  run through its uv shebang). `--refresh-egress` works again: its POST had no JSON
  content type and has got 415 since mutations must send JSON; the refreshed probe
  keeps its geo label. A client without telemetry shows `-` instead of `0 B`, and a
  panel older than 2.4 is named as such. Tested now, from a recorded payload.

### Block LAN covers the panel
- **With Block LAN on, a server's clients cannot open the panel** through the tunnel
  (at the server's tunnel address, or the container's own). It already kept them off
  private ranges, but that rule filters traffic passing through the container, and
  the panel is the container itself. **This changes an upgrade:** Block LAN is on by
  default, so a device that opened the panel through its tunnel no longer can; turn
  Block LAN off for that server to allow it again. The LAN and a reverse proxy are
  unaffected. The ⚙ firewall check now expects one more rule per Block LAN server.

### The egress, kept current
- **A server's egress is checked again by itself** after it starts (the boot
  restore included) and after its NAT or Block LAN switch changes while it runs,
  and the card updates live. Until now only ↻ checked it, so after a NAT change,
  which moves where the clients' traffic exits, or a restart, the card could show an
  old exit.
- **The server settings drop the Public IP row:** it is the same for every server
  and the status strip shows it; the Endpoint field shows it too, and with an
  endpoint host set, its hint names the detected public IP.

### Health
- **`unhealthy` now means a VPN is down,** not only the panel: `/status` answers 503
  when a server that should be running (it was last started) has no interface, from
  60 s after the container starts, and the Docker health check's log names it
  (`docker inspect`). Starting the server again, or stopping it in the panel, makes
  the container healthy again.
- **`unhealthy` also means a VPN has drifted from the panel:** a running server whose
  VPN daemon lets in a device the panel suspended or deleted (or lacks one it added),
  or whose firewall rules are not the number its NAT and Block LAN switches call for
  (a lost Block LAN rule, or a NAT rule left behind). Restarting the server in the
  panel puts either right. Whether the internet is reachable is deliberately not
  checked: nothing in the container can fix that.

### New servers start from the newest one
- **The ⚙ New servers section is gone,** and its six settings with it. The New
  server form starts from your newest server: its MTU, DNS, NAT and LAN blocking
  (the port and subnet are the next free ones, as before), and says which server it
  copied. The first server starts from the built-in values (MTU 1280, DNS
  8.8.8.8 and 1.1.1.1, NAT and LAN blocking on). The section's subnet was never used
  by the form, and every field could be changed there anyway.
- **`DEFAULT_MTU`, `DEFAULT_SUBNET`, `DEFAULT_PORT`, `DEFAULT_DNS`, `ENABLE_NAT` and
  `BLOCK_LAN_CIDRS` are retired:** a set one logs a warning. Their stored values are
  dropped at the first boot; existing servers keep their own MTU, DNS, NAT and LAN
  blocking.

### Housekeeping
- **`AUTO_START_SERVERS` is gone:** a restart always brings back each server as it
  was last left; a container that still sets the variable (even to `false`) logs a
  warning, as one with `API_TOKEN` does.
- **`run_tests.sh` lints first:** `ruff check` and `ruff format --check`, at a pinned
  ruff, before the suite (skipped where uv is missing, as inside the image). Markdown
  is left out of ruff.
- **A map at the top** of `amnezia_manager.py` and `app.js`: what is in the file, in
  what order.
- **The public IP, the egress probe and GeoIP** moved from `amnezia_manager.py` into
  `services/netinfo.py` (no behaviour change), and their tests into
  `tests/test_netinfo.py`.
- Python pins checked on 2026-09-30: all at their latest release.

## Version 2.4 (2026-09-30)

Panel settings in the panel, one parameter generator on the server, and a round of
fixes and hardening. Upgrading needs no action: the first boot copies today's
environment variables into the stored settings, so nothing changes until you trim
them. A new volume, or one that never set `NGINX_PASSWORD`, signs in with
admin/changeme and says so in a banner.

### Settings and behaviour
- **Panel settings drawer** (⚙ in the header): the sign-in (user name and password,
  the current password required), the daemon's and the panel's log levels, GeoIP,
  and the defaults for new servers. Stored in `web_config.json`; the sign-in hashed
  (SHA-512 crypt) in `/etc/amnezia/.htpasswd`, which nginx reads, so it survives
  image updates. `GET`/`POST /api/settings` never serve the password or its hash.
- **One rule for environment variables:** a set, non-empty variable pins its setting
  (copied in at every boot, read-only in the drawer); removing it keeps the last
  value, now editable. `NGINX_PASSWORD` doubles as recovery.
- **Default password banner** until admin/changeme is changed.
- **Changing the password keeps the tab working:** the tab that saves it stays signed
  in; any other open tab reloads into the browser's sign-in prompt on its next
  request. The page is served `no-store`.
- **Firewall rules in ⚙ → About:** each server's tagged iptables rules as they are
  now, against the number expected.
- **A restart brings back what was running:** each server's last start/stop state
  decides, so a server stopped in the panel stays stopped. `AUTO_START_SERVERS` is
  retired (`false` still means "start nothing at boot" for now).
- **Daemon log:** `off`, `error` (the new default: silent unless something fails) or
  `debug`. It reaches the daemon only; the drawer offers to restart the running
  servers, since the daemon reads its level when it starts.

### Split tunnelling and the endpoint
- **Split tunnelling per client:** each client's AllowedIPs is editable in its
  drawer and on `POST .../clients` (`allowed_ips`). The default stays `0.0.0.0/0`,
  so no device is flagged. Adding `::/0` sends IPv6 into the tunnel, where the
  server drops it and apps fall back to IPv4; a Linux device with IPv6 switched off
  cannot bring `::/0` up.
- **Endpoint host per server:** a DNS name (e.g. dynamic DNS) or IPv4 that client
  configs dial instead of the detected IP. With a name, a new public IP flags no
  client of that server. The settings drawer counts the re-imports before you save.

### Parameters
- **Every new server gets its own random parameters**, drawn by the server
  (`POST /api/generate`, ported from AmneziaWG Architect, MIT): four disjoint H ranges
  under 2³¹−1, S sizes that never make two message types the same length, a small
  junk train (Jc 4–12, Jmin 8–40, Jmax ≤ 160 instead of up to the MTU: junk precedes
  every handshake), and on AWG 3.x a header protection key, content padding and
  timers that keep WireGuard's timer rules. Until 2.4 the drawer used one fixed set
  unless Randomize was pressed, and the JS and Python generators disagreed.
- **Validation:** I1–I5 checked as amneziawg-go parses them (an unknown tag, `<c>`
  included, is refused); S1–S4, Jc, Jmin, Jmax at most 65535 and H at most 2³²−1;
  duplicate H refused on AWG 1.5 too; Jc 0 allowed (no junk). New warnings: two
  message types of the same length, H in WireGuard's own 1–4 without header
  protection, AWG 3.x timers that fight each other, a signature packet above the MTU,
  and AWG 3.x without a header protection key (it then works like 2.0; the warning
  names the S values that would block one).
- With a header protection key, H stays four custom ranges. docs.amnezia.org suggests
  1–4 there (the cipher hides the message type); both work, verified on live tunnels.

### Fixes
- **The API answered without a password on the container's own address.** Flask
  listened on `0.0.0.0:5000`; on a macvlan network that is the LAN. It listens on
  127.0.0.1 now, behind nginx.
- **A panel `LOG_LEVEL` hung every server start** (the daemon kept `awg-quick`'s
  output pipe open), including at boot. The daemon's level now reaches `awg-quick up`
  alone, and the panel's own `LOG_LEVEL` is its own again.
- **↻ during an outage wrote a wrong address into every client config** (the LAN
  address, or `YOUR_SERVER_IP`). Refresh is a POST that answers 502 and changes
  nothing when detection fails; detection is HTTPS only.
- A server on a subnet other than a /24 got an address outside it (`10.8.0.1` for
  `10.8.0.64/26`); it is the subnet's first host now.
- Stopping one server removed the forwarding rule every other server relied on;
  each server's iptables rules carry its own tag now.
- `is_valid_ip` accepted `+1.2.3.4` and `1_0.0.0.1`.
- The Logs view read the whole daemon log every 10 s; it reads the end.

### Look
- More contrast: muted text, edges and field borders darker in the light theme and
  lighter in the dark one; stopped and suspended rows dimmed less. Disabled fields
  are dashed, env-pinned ones carry a lock. The dark theme draws native controls
  (number spinners, select arrows) dark too.

### Live view
- One request draws the page (it used to be 1 + 2 per server, on every focus), and
  telemetry updates patch the rows in place every 7 s: keyboard focus, a text
  selection and an open ⋯ menu survive.
- Telemetry is one `awg show all dump` per tick (byte counts and handshake times
  instead of parsing text), and running state comes from sysfs. Stopped servers no
  longer log an error on every page load.
- GeoIP lookups leave the traffic loop, and a failed one is retried after 10 minutes,
  not a day. The QR code uses error correction M (L for large configs) instead of H:
  81 modules a side instead of 109 for a typical config.

### Security and housekeeping
- **`API_TOKEN` removed**: behind nginx it never admitted or refused anything. A
  container that still sets it logs a warning.
- **Content-Security-Policy `script-src 'self'`**, plus `frame-ancestors 'none'`,
  `nosniff` and `no-referrer`: the page has no inline script or handler left.
- The protocol table reaches the page as JSON from the backend; the JS copy is gone.
- `web_config.json` holds each client once and nothing derived; the traffic monitor
  never writes it. (The v2.1 rollback copy is gone; loading a 2.1 file still works.)
- Alpine pinned to 3.24; the unused `nftables` package, the GitHub Actions workflow
  and `SYS_MODULE` dropped.

## Version 2.3 (2026-09-28)

A redesigned panel, and the panel now tells you which devices hold a config that no
longer matches. Upgrading needs no action: devices are assumed up to date at the
upgrade. Rolling back to 2.2 or 2.1 keeps every server and client; they ignore the two
new per-client fields.

### Outdated-config flags
- **Re-import marks.** The server remembers which config each device last received
  (a fingerprint of the QR text, set when the QR is shown or the `.conf` downloaded)
  and marks a client **Re-import** when the config it would issue now is different:
  after a transport or protocol change (every client of that server), a change to
  that client's parameters, or a new public IP through ↻ (every client; this used to
  break every device silently). Reverting a change clears the mark; renaming,
  suspending, NAT/LAN and start/stop never set it.
- Each server card counts "N to re-import", and the settings drawer says before you
  save how many devices a change sends back to re-import.
- API: client payloads gain `config_outdated` and `config_issued_at`;
  `POST /api/servers/<sid>/clients/<cid>/issued` records a hand-out. The fingerprint
  itself never reaches the browser.

### UI
- **Page:** a status strip (public IP, servers running, clients online), one card per
  server with its facts on one line and a ⋯ menu (Logs, Full config, Rename, Delete),
  and client rows with endpoint and location, last handshake, ↓/↑ totals, a suspend
  switch, QR, Edit and ⋯ (Rename, Download .conf, Delete). Works down to phone width.
- **Forms in a side drawer** (new server, server settings, add and edit client),
  checked by the server as you type: problems and warnings appear under the fields,
  and Save is enabled only when something changed and nothing is invalid.
- **No more browser pop-ups:** toasts, an in-app confirm that names what is deleted,
  inline rename, and an in-app API-token prompt replace all 22 `alert`/`confirm`/
  `prompt` calls.
- **QR view:** larger, encodes the config without comments (smaller, scans more
  easily), with Copy config, Download .conf and QR image. There is deliberately no
  Share button: it would hand the private key to the OS share sheet.
- **Logs and Full config** as dialogs whose code boxes follow the theme; this fixed
  the last dark-mode gaps. Light theme: grey page, white cards; one purple primary
  button style; a sun/moon theme button.

### Backend
- **`POST /api/validate`**, a dry run of each form, so the validation rules live only
  in Python; the JS copy of them (and its drift) is gone. The transport and client
  warnings moved from JS to Python.
- A non-numeric MTU on create is a 400 with a message instead of a 500.

### Development
- `tests/demo_server.py` serves the real panel with example data and no container,
  for UI work, the smoke tests and screenshots. The smoke tests drive the drawer and
  dialogs and fail on any native pop-up. 288 tests, 90% of the backend.
- **CVE scan before every publish.** `publish_dockerhub.sh --publish` builds into the
  local image store, scans both platforms with grype and pushes only if no fixable
  HIGH/CRITICAL is found (unfixed findings are listed as suppressed); `--scan` does
  just the build and scan. The build pulls fresh bases and rebuilds the runtime
  stage, whose `apk upgrade` now takes every Alpine fix, not only expat and zlib.

## Version 2.2.1 (2026-09-28)

Release tooling only; the image is the same as 2.2 apart from its build label.

- **Publishing is a dry run by default.** `publish_dockerhub.sh` and `publish_github.sh`
  build or push only with `--publish`; `--dry-run` is gone. Release:
  `git tag -a v2.3 -m 2.3 && ./publish_dockerhub.sh --publish`.
- **`publish_dockerhub.sh --publish` runs only on master** or on a clean release commit
  (rebuilding an older release), so work on a branch never reaches `:latest`.
- **`publish_github.sh` refuses any remote that is not on github.com**, so it cannot
  overwrite the full history on another remote with the copy stripped of private docs.

## Version 2.2 (2026-09-26)

A simplification and hardening release: less code, a much larger test suite written
*before* the refactors so each one was pinned, no private keys in API responses, and a
self-contained UI (no CDN). The look is unchanged. Upgrading
needs no action; rolling back to 2.1 keeps every server and client (verified on a real
volume, including changes made on 2.2).

### Security
- **Private keys stay on the server.** `GET /api/servers`, the client lists, create-server
  and the client-mutation responses no longer include `server_private_key`,
  `client_private_key` or `preshared_key` — the UI never used them, and create-server's
  response was even logged to the browser console. Configs that need the keys (QR,
  `.conf` downloads, raw server config) are still rendered server-side.
- **No third-party requests.** Tailwind used to be compiled in the browser by a script
  from `cdn.tailwindcss.com` that could not be SRI-pinned; socket.io and qrcode came from
  cdnjs. All three are gone from the page (see UI), so it works offline and nothing
  outside your network learns the panel's address.

### UI
- **Tailwind is compiled at image build** by the standalone CLI (`build_css.sh`,
  pinned v3.4.19, checksum-verified; no Node): 32 KB of CSS instead of a 407 KB compiler
  downloaded and run on every page load. socket.io and qrcode are served from
  `static/vendor/`, byte-identical to their releases.
- **Dark mode is `dark:` classes** next to each colour utility; the 62 hand-written
  `body.dark` overrides are gone and a test fails if a colour class lacks its partner.
  Screenshots of both themes at desktop and iPad widths match 2.1.
- The theme toggle is unchanged: light/dark, the OS preference picks the first one.

### Simpler
- **Each client is stored once**, in its server's list. The top-level `clients` map, the
  mirroring loops in every mutation and the per-client `server_name` copy are gone. On
  disk the v2.1 layout is still written (derived) so 2.1 can read it.
- **`GET /api/servers` is read-only.** It used to backfill defaults and geo labels into
  the stored config and save it on every poll; defaults now come from migration.
- **Legacy `obfuscation_*` fields** are no longer re-derived on every load.
- **Routes:** `server_or_404` / `client_or_404` and one error handler replace ~20
  copy-pasted not-found blocks and the try/excepts (`routes/servers.py` 412 → 344 lines).
- **eventlet is gone.** Socket.IO runs in threading mode with simple-websocket
  (eventlet, greenlet and dnspython leave the image).

### Fixes
- Invalid client params or a full subnet on add-client / client-params returned an
  HTML 500; they are now a JSON 400 with the reason.
- A legacy server without `mtu` rendered its server `.conf` with `DEFAULT_MTU` but raised
  on client-config generation until the first page load; migration now sets it.
- `save_config` writes are serialized and each uses its own temp file, created 0600 —
  before, the temp file was briefly created with default permissions.

### Tests
- 151 → 247 tests, backend coverage 66% → 89%. New: the real start/stop, live-reload,
  iptables and key-generation code under a fake `subprocess.run`; public IP, GeoIP and
  egress probe; a 404 sweep over every id-taking route; the exact API key sets the UI
  reads and no key in any JSON payload; v2.1 two-store migration; concurrent saves; a
  static no-shell guard; frontend checks (no foreign hosts, vendored hashes, dark pairs);
  the publishing rules below.
- `smoke_ui.js` also checks the theme toggle and that no request leaves the origin.

### Versioning and publishing
- **The version comes from git release tags** (`vX.Y[.Z]`, read by `version.sh`), no
  longer from the argument typed to `publish_dockerhub.sh` — `:2.1` had been published
  three times with different code. `:X.Y` is pushed only from a clean commit exactly on
  its tag. Every publish still updates `:latest`, except a rebuild of an older release,
  so `:latest` never goes backwards. A version argument must match the tag (if it
  does not, the error says where that tag is and what to run instead);
  `--dry-run` shows the plan.
  Release: `git tag -a v2.2 -m 2.2 && ./publish_dockerhub.sh`.
- **Build labels are consistent and never stale.** They reach the image as a build arg:
  publish writes `v2.2 build 20260926.1` (or `v2.2-3-gabc1234 build …` between
  releases), `run.sh` writes `<git version> (local)`, a plain build shows `dev`. Before,
  publish left `web-ui/BUILD` in the source tree, so later `run.sh` builds and
  bind-mounted dev containers showed the last release's label; without an argument the
  label read `vdev`.

## Version 2.1 (2026-08-15)

Adds **AmneziaWG 3.1** while keeping AWG 1.5, 2.0 and 3.0 configurations compatible.

### AWG 3.1
- **`RandomTrailers`** — appends a random number of bytes to packets to vary their wire size. The setting is mirrored into every client config.
- **`DisableCookies`** — optionally suppresses handshake cookie replies. It defaults off because cookies provide handshake-flood protection.
- Both options are available only for AWG 3.1, use checkboxes in the create/edit forms, and are rendered as canonical `on` / `off` values.

### Components
- `amneziawg-go` → `1b86b2a` (`v3.1.20260814`), including the fix for a runtime panic when `RandomTrailers` was used on handshake cookie messages.
- `amneziawg-tools` → `v3.1.20260812`. The daemon and tools pins remain paired because 3.1 adds new UAPI fields.

### Validation
- Added AWG 3.1 protocol-table, validation, leakage, mirrored-config and golden-file coverage (118 tests total).
- Verified a generated 3.1 server/client pair against the real daemon: `awg showconf` and live `awg show` both reported the two options enabled.
- Browser smoke coverage now checks all four protocols and the 3.1 controls in both themes.

### Fixes (2026-08-23)
- **iPadOS Safari repeatedly re-prompted for Basic Auth credentials.** WebKit does
  not reliably reattach a cached Basic Auth credential to a WebSocket upgrade
  handshake, and every Socket.IO reconnect attempt re-triggered the native
  credential dialog — not reproducible on macOS Safari. `/socket.io/` is no longer
  Basic-Auth-gated in nginx; instead Flask sets a persisted-secret-key session
  cookie on any request that already cleared Basic Auth on `/` or `/api/`, and the
  WebSocket `connect` handler requires that cookie. `/` and `/api/` are unaffected.

### Fixes (2026-09-10)
- **A stopped server logged an error every 7 seconds.** `get_server_status()` detects a
  stopped server by the absence of its interface, but asked for it via
  `run_command(["ip", "link", "show", ...])`, whose non-zero exit is logged at error
  level. `start_traffic_monitoring()` runs that check for every configured server on a 7s
  loop, so a single stopped server produced roughly 12k error lines a day and buried every
  real failure in the log. It now tests `/sys/class/net/<interface>` first and returns
  `stopped` when it is absent; the `ip link show` call and its `state UNKNOWN` check still
  run whenever the interface exists, so a present-but-down interface is still reported
  correctly and a genuine failure of that command is still an error.

### Components (2026-09-10)
- Go builder → `1.26.6`, `golang.org/x/crypto` → `v0.56.0`. Clears 1 critical, 8 high and
  2 medium advisories that `docker scout` reported against the Go stdlib and x/crypto;
  rescan of the rebuilt image is 0C/0H/0M. `x/net` and `x/sys` are unchanged — no
  advisories, and the pins are floors that minimal version selection only raises.
- `amneziawg-go` → `v3.1.20260828` (`b5928ef`), picking up two upstream fixes to features
  this UI exposes: UDP window handling for `RandomPaddingAddition`, and disabling the whole
  underload path when `DisableCookies` is set. Neither touches the UAPI surface, so
  `amneziawg-tools` stays at `v3.1.20260812` — still upstream HEAD — and the pair remains
  consistent.

### UI (2026-09-10)
- **Build label** under the page heading, e.g. `v2.1 build 20260910.1`, written to
  `web-ui/BUILD` by `publish_dockerhub.sh` (n = builds that day; a failed build still uses a
  number, so gaps are normal). Plain `docker build` or a bind-mounted source shows `dev`.

### Tooling (2026-09-20)
- **`scripts/api_status.py` is a uv PEP 723 script.** It declares `requests` in an inline
  `# /// script` block and runs through a `uv run --script` shebang, so it no longer fails
  with `ModuleNotFoundError` on a host whose `python3` has no `requests`. Run it directly;
  it needs `uv` on `PATH`.

### Components (2026-09-26)
- Go builder → `1.26.8` (net/http fixes in 1.26.7, runtime/compiler fixes in 1.26.8; no
  advisory was open against 1.26.6).
- `python-socketio` → `5.17.0`, `python-engineio` → `4.14.0`. 5.17.0 stops a client joining
  another client's sid room; this UI uses no rooms, so here it is hygiene, not a fix.
- Socket.IO browser client → `4.8.4` (bug fixes only).
- Unchanged on purpose: `amneziawg-go` / `amneziawg-tools` (both still upstream HEAD), and
  `eventlet` 0.41.1 — it caps `greenlet<3.4`, and 0.41.2 lifts that cap, so bumping it moves
  greenlet a major step for no gain.

### Security (2026-09-26)
- **The cdnjs scripts carry SRI hashes.** `socket.io.js` and `qrcode.min.js` now have
  `integrity` attributes, so a tampered CDN file is refused instead of running in the
  panel's origin. The hashes match cdnjs's published values, and `socket.io.js` is
  byte-identical to the npm release. `referrerpolicy="no-referrer"` also stops the panel's
  URL being sent to cdnjs. The Tailwind play CDN is generated per request and cannot be
  pinned this way.

### Tests (2026-09-26)
- **The auth guards moved from `app.py` to `core/guards.py`** (`install_guards()`), with no
  behaviour change. The tests previously exercised a hand-copied mirror of the CSRF and
  token checks; they now run the production code, and cover what had no tests at all: the
  persisted secret key, the `/socket.io/` session-cookie check, and `nginx.conf`'s
  per-location auth rules.
- Legacy migration is tested on a real v1.5.1 `web_config.json`, which must render the same
  bytes as a server created today; every mutation route is checked to keep the two client
  stores in sync after a save/load; `/api/system/awg-log` interface filtering is covered.
- 118 → 151 tests; backend coverage 52% → 66%.

### Tooling (2026-09-26)
- **`run_tests.sh` runs through uv** when it is on `PATH`
  (`uv run --with-requirements web-ui/requirements.txt`), so the host needs no `.venv` and
  the deps always match the image's pins. `--cov` adds a coverage report. Without uv — as
  inside the image — it still runs plain `python3`.

## Version 2.0 (2026-08-07)

Major version: tracks the **AmneziaWG 3.0** protocol generation. `amneziawg-go` and
`amneziawg-tools` are both upgraded to v3, which changes the UAPI wire format.
Existing AWG 1.5 / 2.0 servers and clients keep working — 3.0 features are opt-in.

### AWG 3.0
- **Header protection** (`HeaderProtectionKey`) — encrypts packet headers instead of only randomising them. Generated per server, mirrored into every client config. Requires each of S1–S4 to be ≥ 12 (enforced).
- **`ContentPaddingAddition`** — extra random bytes per data packet.
- **Tunable timings** — `RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts`, per client, range-valued (`22-30`).
- Protocol-specific fields appear only for protocols that support them.

### Components
- `amneziawg-go` → `08d68cd`, `amneziawg-tools` → `v3.0.20260805`. **These must match**: v1.x tools cannot configure a v3 daemon. Go builder pinned to `1.26.5`.

### Security
- **CVEs: 85 → 1** (12 critical → 0). Raised the `x/crypto` / `x/net` / `x/sys` pins (the old ones were *below* upstream's own requirements, so they did nothing), dropped `apache2-utils` in favour of `openssl passwd -apr1`, removed `setuptools`, added a pinned `requirements.txt`.
- **Shell execution removed** from the backend — all subprocesses use argv lists, and subnet/port from the API are validated. Closes a command-injection path via `subnet`.
- **CSRF**: a cross-site form POST could create and start a VPN server. Mutating `/api/` requests now require `Content-Type: application/json`.
- Config files and `web_config.json` are now mode `0600` and written atomically.

### Fixes
- Deleting one of two identically named clients no longer removes **both** `[Peer]` blocks.
- Client addresses are now derived from the server's subnet, so non-`/24` subnets allocate correctly, and both client stores are checked so a duplicate cannot be handed out. A full subnet raises instead of reusing an address.
- Creating a server rejects a port already in use or a subnet overlapping an existing server (previously only the browser warned).
- The GeoIP cache is bounded, so it no longer grows for the lifetime of the container.
- Adding/removing a client now hot-reloads the daemon based on the live interface state, not a cached status field that could be stale.
- Failed key generation raises instead of substituting unrelated random keys (a server that looked healthy but could never handshake).
- Status updates use Socket.IO background tasks, not threads that block eventlet.
- `/api/system/iptables-test` no longer reports "Not found" for rules that exist.
- The API token prompt no longer appears for nginx Basic Auth failures, where it could not help.
- Server and client names are sanitized before they reach a config file: a name containing a newline could previously end the `# Client:` comment and have the rest parsed as configuration.

### UI
- Long parameter help is collapsible, so dialogs fit the viewport with their buttons visible.
- Fixed dark-mode help text that was effectively invisible (1.25:1 contrast).

### Maintainability
- Modal dialogs moved to `static/js/modals.js`; the protocol table has one definition per side (`amnezia_manager.py` + `static/js/protocols.js`), so adding a generation is a two-file edit.
- `print()` replaced with `logging` — timestamps, levels and module names in the logs; `LOG_LEVEL` controls verbosity.
- Added `./run_tests.sh` (111 stdlib `unittest` tests, no new dependencies) plus browser smoke tests; CI runs the suite before building.
- Added `DEVELOPMENT.md` (architecture, state model, conventions, open items) and `CLAUDE.md`.

## Version 1.6.1 (2026-05-16)

- AWG 2.0 support: Updated `amneziawg-go` builder to latest commit (f4f4c99) with fixed S4 handling for keepalive packets. Updated awg-tools builder to latest release (tag v1.0.20260223).
- Image rebuilded and versions bumped to address CVEs.

## Version 1.6 (2026-03-29)

### New Features
- **Client suspend/reactivate**: Toggle client access without deleting — suspended clients have their `[Peer]` block removed from the WireGuard config and live-synced. Keys and settings are preserved for reactivation.
- **Smart IP allocation**: New clients get the first unused IP in the subnet instead of relying on client count. Prevents IP collisions when clients are deleted from the middle.
- **Smart port/subnet proposals**: "Create Server" dialog auto-proposes the next free port (starting 51820) and subnet (`10.10.X.0/24` pool), skipping values already used by existing servers.
- **Full AWG 2.0 support**: S3/S4 padding parameters (cookie and transport messages), H1–H4 header ranges (`x-y` syntax), protocol-aware validation and config generation — S3/S4 are included for AWG 2.0 servers and silently ignored for AWG 1.5. Clear separation of server transport params (S1–S4, H1–H4) and client-only params (Jc, Jmin, Jmax, I1–I5). README rewritten to match upstream `amneziawg-go` docs (corrected I1–I5 tag reference, removed obsolete `<c>` tag, added `<rd>`/`<rc>`).
- **Rename servers and clients**: Click the name in the Edit Config dialog to rename. No restart required — names are display-only metadata. Server renames propagate to all child clients.

### UI Redesign
- **Server controls**: Start/Stop buttons replaced with a compact on/off toggle switch next to the server name, with bold Running/Stopped status label.
- **Server icon buttons**: Add Client, Edit Config, View Logs, and Delete are now compact icon buttons in the top-right of the server card (removed the separate button row).
- **Client controls**: QR Code, Edit, and Delete redesigned as lightweight icon+label buttons with consistent styling. Delete uses icon-only with a visual divider.
- **Client suspend toggle**: Mini toggle switch per client — green = active, amber = suspended. Suspended clients have their row dimmed (`opacity-50`) and status dot turned gray.
- **Stopped server dimming**: Stopped server cards are dimmed (`opacity-60`) including all client rows.
- **Name coloring**: Server names in purple (`text-purple-600`), client names in sky blue (`text-sky-600`) — consistent across dashboard and edit config dialogs.
- **"New Server" button**: Purple pill with server+plus icon badge, shortened label.
- **QR Code button**: Amber/yellow color scheme.

### Dark Theme
- Added comprehensive dark mode CSS overrides for all new icon button styles (purple, slate, amber, sky, red, green backgrounds, text colors, and borders).
- Fixed dark mode for client edit config dialog — Protocol/Parameters and comment text now use classes with proper `body.dark` overrides instead of non-functional Tailwind `dark:` prefixes.

### Docker / Security
- Multi-stage Dockerfile hardened: removed `curl` and `py3-pip` from runtime, Python deps built in a venv builder stage, `expat`/`zlib` upgraded, vendored `pip`/`wheel` artifacts stripped from setuptools.
- Healthcheck switched from `curl` to Python `urllib` (no curl in runtime image).

### Bug Fixes
- Fixed UDP port publishing in `run.sh` — changed from single port 51820 to range 51820-51830 for multi-server support.
- Fixed light-theme readability of Protocol/Parameters text and comment descriptions in client edit config dialog.

## Version 1.5.1 (2026-02-26)

### New
- Added per-server egress/public IP probing from inside the container (source-bound probe), so each server card can show the final outbound external IP seen for that server.
- Added API endpoint `POST /api/servers/<server_id>/egress-ip` and persisted probe result metadata (`external_ip`, route details, timestamp, and errors).
- Added shared backend GeoIP lookup/cache path for all IP enrichment flows (client endpoint IP, server public IP, and server egress IP).
- Added GeoIP fields to server payloads for UI/CLI consumption: `public_ip_geo`, `public_ip_geo_country_code`, `egress_probe.external_ip_geo`, and `egress_probe.external_ip_geo_country_code`.

### Refactor / Cleanup
- Refactored backend structure by splitting monolithic logic into dedicated modules:
  - `web-ui/services/amnezia_manager.py`
  - `web-ui/routes/system.py`
  - `web-ui/routes/servers.py`
  - `web-ui/core/runtime.py`, `web-ui/core/helpers.py`
- Refactored frontend JavaScript into smaller modules:
  - `web-ui/static/js/api.js`
  - `web-ui/static/js/server-ui.js`
  - streamlined `web-ui/static/js/app.js`.
- General lint/style cleanup across Python files (manual fixes, formatting and line-length cleanup).

### UX
- Added inline per-server egress IP refresh action and clearer egress status rendering in the server card.
- Improved dark-theme styling for new egress UI controls.
- Server cards now show Geo metadata for both server public IP and egress IP (country flag + location when available).
- Header public IP badge now includes country flag (when GeoIP is available).

### Tooling
- `scripts/api_status.py` now prints `Server IP` and `Egress IP` on dedicated lines and includes optional Geo info for both.

## Version 1.5 (2026-01-16)

### Obfuscation
- Server config modal now allows editing server obfuscation parameters and applies changes by rewriting the server config and restarting the server when it is running.
- Added support for additional padding parameters `S3`/`S4` (AWG 2.0?) across UI + API + config generation.
- Due to observed connectivity issues on some AmneziaWG builds when `S3`/`S4` are set, the UI now leaves `S3`/`S4` EMPTY by default (empty means the line is omitted from configs). I did not figured out a way how to make it work on server side with current amneziawg-go implementation. Keep for the future. 
- Finding: `S1`/`S2` appear to be the only parameters whcih require exact match between server and client; other obfuscation parameters may be more tolerant depending on the AmneziaWG version.

### Container / Build
- Docker image now builds `amneziawg-go` and `amneziawg-tools` from source (multi-stage build with pinned refs by default).
- Added `wg`/`wg-quick` compatibility symlinks to `awg`/`awg-quick` inside the container.
- Container startup logs now print detected AmneziaWG binary paths/versions for easier debugging.
- Added optional userspace `amneziawg-go` internal logging:
  - Set `AWG_LOG_LEVEL=debug|verbose|error|silent` to enable (or `off`/empty to disable).
  - Logs are written to `/var/log/amnezia/amneziawg-go.log` (override with `AWG_LOG_FILE`).

### Monitoring / UX
- Added per-server “View Logs” with auto-refresh and interface-aware log filtering (includes related startup banner lines).

### Security
- Updated Go builder patch version used for `amneziawg-go` to reduce/avoid known Go stdlib CVEs.

## Version 1.4.3 (2026-01-15)

### Networking / IPTables
- NAT and forwarding rules now target the actual WAN interface instead of relying on the `eth+` wildcard.
- Auto-detect outbound interface from the default route (override with `WAN_IF` if detection fails).
- Optional LAN access blocking toggle via `BLOCK_LAN_CIDRS` (default: `1`) for common private ranges (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`).
- Per-server `ENABLE_NAT` and `BLOCK_LAN_CIDRS` settings (defaults still read from env).
- New API: `POST /api/servers/<server_id>/networking` applies networking changes and reapplies iptables when running.
- UI: Create-server checkboxes + server config modal toggles for NAT and LAN blocking.

### Tooling
- `scripts/api_status.py`: simplified output/logic and more robust handling of invalid responses; token auth continues to use `X-API-Token` (compatible with Nginx Basic Auth).

### Docs / Dev
- Documented `WAN_IF` and `BLOCK_LAN_CIDRS` environment variables in README.
- `run.sh`: default `ENABLE_NAT=1` in the example run command and clarified `API_TOKEN` usage.
- Updated screenshot.
- Docker Compose/run examples now omit IPv6 sysctls for IPv4-only setups.

### UX
- NAT / LAN Block statuses on the server's card
- Create-server modal uses JS validation only (native HTML validation disabled) to avoid the “invalid form control is not focusable” error.
- Header now shows Public IP and connection status on the same line, with a green/red status dot.
- Added dark theme toggle and refined dark mode contrast (dialogs, headers, refresh button).
- Rounded “pill” button styling applied across dialogs and controls.

## Version 1.4.2 - Security hardening + live monitoring (2026-01-14)

### Security / Auth
- Optional app-layer API token (`API_TOKEN`) enforcement for all `/api/*` endpoints.
- Token can be provided as `X-API-Token` (works alongside Nginx Basic Auth) or `Authorization: Bearer ...`.
- Socket.IO CORS can be restricted via `ALLOWED_ORIGINS` (default is same-origin).

### Monitoring / UX
- Live traffic updates pushed via Socket.IO so UI refresh is not required.
- Client rows show endpoint + latest handshake (and optional GeoIP enrichment when enabled).

### Tooling
- Added `scripts/api_status.py` CLI to quickly inspect server/client status from a terminal.

### Internal
- Reduced duplication in backend by centralizing config-value sanitization and server/client lookup helpers.

## Version 1.4.1 - Client endpoint IP + GeoIP flag

### New Features
- UI now displays connected client public endpoint (`IP:PORT`) from `awg show`.
- UI now displays `latest handshake` (from `awg show`) under the endpoint.
- Optional GeoIP enrichment for endpoints:
  - Shows a country flag and location label when available.
  - Controlled by `ENABLE_GEOIP` (set `0`/`false`/`no`/`off` to disable external lookups).

### API
- `/api/servers/<server_id>/traffic` now includes `endpoint`, `latest_handshake` and GeoIP fields (`geo`, `geo_country_code`) per client.

### Improvements
- Client config download no longer writes temp files to disk (streams from memory).
- IPTables scripts: safer quoting, safer `ENABLE_NAT` handling, and basic argument validation.

## Version 1.4.0 - AmneziaWG 1.5 protocol (I1–I5)

### New Features
- Added support for AmneziaWG 1.5 obfuscation parameters `I1`–`I5`.
- I1–I5 are treated as **client-only** parameters:
  - Server stores I1–I5 as defaults for **new** clients.
  - Each client can have its own I1–I5 values.
  - Existing clients are not modified when server defaults change.
  - Empty I values are omitted from generated client configs.

### API
- Added `POST /api/servers/<server_id>/i-params` to update server-level default I1–I5 (new clients only).
- Added `POST /api/servers/<server_id>/clients/<client_id>/i-params` to update a specific client’s I1–I5.
- Client creation (`POST /api/servers/<server_id>/clients`) accepts optional I1–I5 overrides via `i_params` (or `obfuscation_params`).

### UI/UX Improvements
- Servers list moved to a dedicated top section; server creation moved into a modal dialog.
- Added port/subnet conflict warnings when creating a server.
- Improved config modals rendering (HTML-escaping + better wrapping) to avoid broken layout on values containing `<...>`.
- QR generation hardened (use raw config text, escape modal title, try multiple error correction levels, show a clear error when payload is too large).
- I1–I5 editors use auto-growing textareas for long values.

### Networking
- IPTables: added a DROP rule for traffic from VPN subnet to `192.168.0.0/16` (isolate VPN clients from internal network).
- IPTables: NAT/MASQUERADE is now controlled by `ENABLE_NAT` (enabled by default when unset or `1`).

### Repository / Dev workflow
- Added `.gitignore` for editor/OS artifacts and Python bytecode.
- Added `run.sh` helper for repeatable local Docker build/run (idempotent container replacement).

## Version 1.3.2 - obfuscation adjustment

### Fix
Minor fixes for generation of obfuscations params.
Adjusted default MTU.

### Improvement
Now Jmin and Jmax can be set manually in the valid ranges.
Improved params generation validation.


## Version 1.3.1 - healthcheck

### Fix
Fixed healthcheck on custom port. Added `/status` endpoint for health check.

## Version 1.3.0 - Client traffic

### New Features
Enables monitoring of per-client traffic statistics on a given server and displays the current traffic usage in the UI. After server is stopped the data on the network adapters is reset.

- Backend: Added `get_traffic_for_server` method to parse `awg show <interface>` output and map traffic to clients by public key.
- Backend: Added `/api/servers/<server_id>/traffic` endpoint returning traffic info JSON.
- Frontend: Modified `loadServerClients` to fetch traffic and pass it to renderServerClients.
- Frontend: Updated `renderServerClients` to display received and sent traffic per client below client IP.

### API Endpoints Added

#### `/api/servers/<server_id>/traffic`
**Method**: GET<br>
**Description**: This endpoint returns traffic statistics for all clients connected to a specified server.<br>
**Response Format**:
```json
{
  "clientA": {
    "received": "2.45 MiB",
    "sent": "5.12 MiB"
  },
  "clientB": {
    "received": "0 B",
    "sent": "0 B"
  }
}
```
If the server is not found or no traffic data is available, the endpoint returns:
```json
{
  "error": "Server not found or no traffic data"
}
```
with HTTP status code 404.


## Version 1.2.0 - QR Code Feature Release

### New Features
- **QR Code Generation**: Added QR code support for client configurations
- **Clean Config Format**: Implemented clean config generation without comments for QR codes
- **Dual Config Views**: Toggle between clean (QR-ready) and full (with comments) config views
- **QR Code Download**: Export QR codes as PNG images
- **Enhanced UI**: Improved modal design with better layout and responsive design
- **Configuration Toggle**: Switch between clean and full configuration views

### API Endpoints Added

#### 1. `/api/servers/<server_id>/clients/<client_id>/config-both`
**Method**: GET<br>
**Description**: Returns both clean (without comments) and full (with comments) client configurations in a single request<br>
**Response Format**:
```json
{
  "server_id": "abc123",
  "client_id": "xyz789",
  "client_name": "Client Name",
  "clean_config": "[Interface]\nPrivateKey = ...",
  "full_config": "# AmneziaWG Client Configuration\n[Interface]\n...",
  "clean_length": 450,
  "full_length": 600
}
```
**Purpose**: Optimized endpoint for QR code generation that returns both versions to reduce API calls

#### 2. Enhanced `/api/servers/<server_id>/clients/<client_id>/config`
**Method**: GET<br>
**Description**: Now serves clean configuration (without comments) for direct download<br>
**Response**: `text/plain` WireGuard configuration file<br>
**Changes**: Updated to use the unified `generate_wireguard_client_config()` function with `include_comments=True` parameter

### Client Configuration Endpoints

| Endpoint | Method | Description | Response Format |
|----------|--------|-------------|-----------------|
| `/api/servers/<server_id>/clients/<client_id>/config` | GET | Download client config (with comments) | `text/plain` (.conf file) |
| `/api/servers/<server_id>/clients/<client_id>/config-both` | GET | Get both clean and full configs | JSON with `clean_config` and `full_config` |
| `/api/servers/<server_id>/clients/<client_id>` | DELETE | Delete client | JSON status |

### Server Configuration Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/servers/<server_id>/info` | GET | Get server info with config preview |
| `/api/servers/<server_id>/config` | GET | Get raw server config |
| `/api/servers/<server_id>/config/download` | GET | Download server config file |

### Improvements:
- socket.io connection improvements on custom ports

## Version 1.1.1
Fix:
* clients are not applied to the running server when added without restart.
* clients are not properly removed from server config when removed from the app

## Version 1.1
Add: nginx basic auth support

## Version 1.0
Initial release