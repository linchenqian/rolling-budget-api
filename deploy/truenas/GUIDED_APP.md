# Guided Custom App installation

This is the primary TrueNAS SCALE deployment path for Rolling Budget API. It
creates one container and one ixVolume from the TrueNAS UI. No dataset,
configuration file, or database container needs to be created manually.

The examples use the reserved documentation hostname `budget.example.com`.
Replace it with your own hostname during deployment.

## 1. Wait for the Guided App image

The image must be a release that explicitly includes all of the following:

- SQLite storage at `/data/budget.db`
- automatic database migrations before the API starts
- a default root runtime that can initialize the ixVolume without extra
  permission configuration
- a built-in health check

On the GitHub repository, publish a versioned release and wait for the
**Publish container** workflow to finish successfully. Confirm that the GHCR
package is public and that the fixed tag can be pulled without registry
credentials.

Use these image fields only after that release exists:

```text
Repository: ghcr.io/YOUR_GITHUB_USERNAME/rolling-budget-api
Tag:        0.3
```

The `0.3` minor channel receives `0.3.x` image updates through TrueNAS's built-in
Docker update check. Use a fixed patch tag such as `0.3.2` instead when automatic
update notifications are not wanted. Do not use `latest`.

## 2. Open the guided wizard

In TrueNAS, go to **Apps → Discover Apps → Custom App**. This opens the guided
Install Custom App form. Do not select **Install via YAML** for this path.

## 3. Fill in the fields

### Application Name

| Field | Value |
| --- | --- |
| Application Name | `rolling-budget-api` |
| Version | Keep the TrueNAS default |

### Image Configuration

| Field | Value |
| --- | --- |
| Repository | `ghcr.io/YOUR_GITHUB_USERNAME/rolling-budget-api` |
| Tag | `0.3` for built-in `0.3.x` update detection, or a fixed patch tag |
| Pull Policy | Only pull image if not present on host |

### Container Configuration

Leave **Entrypoint** and **Command** empty so the image can run migrations and
start the API itself.

| Field | Value |
| --- | --- |
| Timezone | `America/New_York` |
| Restart Policy | Unless Stopped |
| Disable Builtin Healthcheck | Off |
| TTY / Stdin | Off |

Add one required environment variable:

| Name | Value |
| --- | --- |
| `API_KEY` | A unique random secret of at least 32 characters |

To connect ChatGPT, add one more environment variable:

| Name | Value |
| --- | --- |
| `PUBLIC_BASE_URL` | Your public HTTPS origin, for example `https://budget.example.com` |

Do not append `/mcp` or a trailing slash. The service derives the MCP resource,
OAuth endpoints, issuer, and token audience from this exact origin. Omitting
`PUBLIC_BASE_URL` leaves the REST API enabled but disables remote MCP/OAuth.

Generate this key in a password manager or with `openssl rand -hex 32`. Do not
reuse a bank, GitHub, OpenAI, TrueNAS, or Wi-Fi credential, and do not paste the
key into source control or chat.

No other environment variable is required. The image supplies these defaults:

| Optional override | Default |
| --- | --- |
| `DATABASE_URL` | `sqlite:////data/budget.db` |
| `CORS_ALLOWED_ORIGINS` | `*` |
| `APP_ENV` | `production` |
| `LOG_LEVEL` | `INFO` |
| `AUTO_MIGRATE` | `1` |
| `PORT` | `8000` |
| `MAX_BATCH_ITEMS` | `250` |
| `MAX_REQUEST_BYTES` | `262144` |
| `STALE_AFTER_HOURS` | `36` |
| `MCP_MAX_REQUEST_BYTES` | `524288` |
| `OAUTH_CONSENT_SECRET` | Uses `API_KEY` when empty; required in advanced role-key-only mode |
| `OAUTH_FORM_ACTION_ORIGINS` | `https://chatgpt.com`; comma-separated exact HTTPS origins |
| `OAUTH_AUTHORIZATION_CODE_TTL_SECONDS` | `300` |
| `OAUTH_ACCESS_TOKEN_TTL_SECONDS` | `900` |
| `OAUTH_REFRESH_TOKEN_TTL_SECONDS` | `7776000` |

The CORS default allows a browser page from any origin to call the API, but it
does not bypass bearer authentication. Once the dashboard browser origin
is stable, optionally replace `*` with its exact origin, such as
`https://dashboard.example.com`. The origin is the page making the browser request,
not necessarily the API hostname.

Keep `AUTO_MIGRATE=1` for the guided deployment. If `PORT` is overridden, the
container port mapping and health check must use the same value.

### Security Context

| Field | Value |
| --- | --- |
| Privileged | Off |
| Capabilities | None |
| Custom User | Off |

Leave every other security-context override off. The image intentionally uses
its default root runtime for the simplest ixVolume setup. Do not enable
**Privileged**, add capabilities, mount host paths or TrueNAS system
directories, or expose the Docker socket. The only storage mount for this path
should be the dedicated ixVolume at `/data`.

### Network Configuration

Leave **Host Network** off. Add one port mapping:

| Field | Value |
| --- | --- |
| Container Port | `8000` |
| Host Port | `18080` |
| Protocol | TCP |

Port 18080 must be unused on the TrueNAS host. It should be reachable by the
reverse proxy but should not be forwarded directly from the internet router.

### Portal Configuration

The portal is only a link shown by TrueNAS. It does not configure DNS, TLS, or a
reverse proxy.

| Field | Value |
| --- | --- |
| Name | `Budget API` |
| Protocol | HTTPS |
| Use Node IP | Off |
| Host | `budget.example.com` |
| Port | `443` |
| Path | `/` |

### Storage Configuration

Click **Add** and configure one volume:

| Field | Value |
| --- | --- |
| Type | ixVolume (Dataset created automatically by the system) |
| Read Only | Off |
| Mount Path | `/data` |
| Dataset Name | `rolling-budget-data` |
| Enable ACL | Off |

No ownership or ACL entry needs to be added for this guided path. The container
creates the SQLite database and its journal files directly in `/data`.

### Resources

No GPU is required. One CPU and 512 MiB to 1 GiB of memory are sufficient for a
small personal deployment; increase them only if observed usage requires it.

## 4. Install and verify

Save the form and wait for the app to report **Running/Healthy**. From a trusted
machine on the same network, verify both endpoints:

```sh
curl --fail http://TRUENAS_LAN_IP:18080/health/live
curl --fail http://TRUENAS_LAN_IP:18080/health/ready
```

If readiness fails, open the app's **Workloads → Logs** view. The most likely
first-install causes are an image tag that does not exist, storage that is not
mounted at `/data`, or an invalid/missing `API_KEY`.

The dashboard endpoint also requires initial category configuration. A
`409 config_required` response from `/v1/dashboard/budgets` means networking,
TLS, and authentication can be working while application configuration is still
empty.

## 5. Configure your public hostname

Create DNS and TLS outside the Guided Custom App form:

1. Point `budget.example.com` at the HTTPS reverse proxy endpoint.
2. Obtain a valid certificate for `budget.example.com`.
3. Configure the reverse proxy upstream as
   `http://TRUENAS_LAN_IP:18080`.
4. Preserve `Authorization`, `Mcp-Protocol-Version`, and `Mcp-Session-Id` headers.
5. Allow `GET`, `POST`, `PUT`, `DELETE`, and `OPTIONS` as forwarded methods.
6. Disable caching for `/v1/*`, `/mcp`, `/.well-known/*`, `/oauth/*`, and health endpoints.
7. Expose only HTTPS port 443; keep port 18080 private to the LAN/proxy.

Verify the public TLS route without an API key:

```sh
curl --fail https://budget.example.com/health/live
curl --fail https://budget.example.com/health/ready
```

Every protected request uses the same value entered as `API_KEY`:

```text
Authorization: Bearer <API_KEY>
```

Never put that key in a URL query parameter. If a public browser bundle embeds
it, anyone who can load the page can extract it; the safer dashboard
integration keeps the key in its server-side proxy.

The ChatGPT MCP endpoint does not accept `API_KEY` as a bearer token. During the
one-time OAuth link, enter `OAUTH_CONSENT_SECRET` when configured, otherwise
enter `API_KEY`, only into the service's own HTTPS consent page. ChatGPT receives
limited, revocable OAuth tokens instead. The container also needs outbound DNS
and HTTPS access to `chatgpt.com` so it can validate ChatGPT's client metadata.

The consent page may request `budget:read`, `budget:refresh`, and
`budget:config`. The last scope allows ChatGPT to update categories or
classification rules only after a direct user request; it does not expose the
owner secret. After upgrading an existing installation that was linked with
read/refresh only, reconnect the MCP integration and explicitly approve
`budget:config`. Existing refresh tokens cannot add that permission, and no new
TrueNAS environment variable is required.

## 6. Use TrueNAS image updates

Keep **Apps → Settings → Check for Docker image updates** enabled. When the
configured `0.3` channel digest changes, the app can show **Update Available**.
Clicking Update pulls the new image and recreates the container while retaining
the `/data` mount. Keep the default pull policy; it does not need to be changed
to Always.

Before clicking Update, snapshot or back up the ixVolume. A Custom App
image-only update does not guarantee the same automatic rollback snapshot as a
catalog application upgrade. To return to an earlier image, edit the app to a
known fixed patch tag and redeploy, then restore the database only if its schema
also needs to move backward.

## 7. Protect the ixVolume

TrueNAS recommends ixVolumes mainly for testing and Host Path storage for
long-lived production data. This deployment accepts ixVolume for simplicity, so
the following safeguards are required:

- Do not select **Remove ixVolumes** when deleting or reinstalling the app unless
  permanent deletion is intended.
- Snapshot and replicate the Apps pool/ixVolume on a schedule.
- Create a fresh snapshot or backup before every image update.
- Test restoring the database rather than assuming a snapshot is usable.
- Keep an independent backup outside the TrueNAS system.

TrueNAS can include ixVolume state in app rollback snapshots, but a rollback is
not an independent backup and can also roll the database backward.
