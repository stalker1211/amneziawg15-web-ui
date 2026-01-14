# CHANGELOG

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