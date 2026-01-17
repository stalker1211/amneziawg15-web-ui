# CHANGELOG

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