# Rolling Budget API

A small, self-hosted API for rolling-window budget tracking. An AI-powered
uploader classifies bank transactions; SQLite (personal mode) or PostgreSQL
(advanced mode) stores validated results; an existing dashboard such as Kitchen
Calendar reads pre-aggregated budget rows.

This repository intentionally contains no bank connector, financial exports, or
real transaction fixtures.

## Status

Personal MVP. Releases publish multi-architecture container images, but the API
and database migration history should still be treated as pre-1.0 interfaces.

## Behavior

- Each category has its own inclusive rolling window, budget, and classification
  instructions. Dates are evaluated in the active rule set's local timezone.
- Pending transactions count toward spend.
- Refunds reduce the original transaction's budget impact.
- A transaction may belong to multiple categories. Category totals can overlap;
  they must not be summed to produce an overall total.
- Transactions classified as `SKIP` are used to prove refresh coverage but are
  not retained in the live transaction table.
- Normal refreshes are incremental. A classification, statistics, or account
  scope rule change creates a new rule version and requires a full rebuild.
- Stable source account IDs are part of the versioned configuration. A refresh
  must cover exactly that account set; display labels are never identity keys.
- A full rebuild is staged and atomically activated. A failed rebuild leaves the
  previous rule version and live dashboard data intact.

## Architecture and trust boundaries

```text
ChatGPT --OAuth 2.1 / MCP-------> API ----> SQLite / PostgreSQL
Local uploader ------API key----> API
Existing dashboard ---API key---> API
```

The API, not the dashboard, calculates rolling dates, refund adjustments, and
category totals. A single master `API_KEY` is enough for a personal deployment.
Optional read, write, and admin keys preserve least-privilege access when the
dashboard and uploader should not share full control.

Browser CORS is open by default, but every protected endpoint still requires a
Bearer API key. Do not embed the master key in public JavaScript: put the API
behind HTTPS and let the dashboard call it through a server-side proxy, or
configure a separate revocable read-only key.

Setting `PUBLIC_BASE_URL` enables a Streamable HTTP MCP endpoint at `/mcp` for
ChatGPT. The remote MCP uses OAuth authorization-code flow with PKCE and issues
only `budget:read` and `budget:refresh` scopes; it never gives ChatGPT the
master API key or permission to change configuration. OAuth credentials are
opaque, stored only as keyed digests, and remain valid across container
restarts. The owner approves the initial link on a private consent page using
the master key or an optional separate `OAUTH_CONSENT_SECRET`. Deployments that
use only role-specific API keys must configure the separate consent secret. The
container needs outbound HTTPS access to `chatgpt.com` to validate ChatGPT's
client metadata during authorization.

## Refresh integrity protocol

Large refreshes use three steps:

1. Create a refresh run with its rule version, mode, scope, source manifest, and
   idempotency key.
2. Upload numbered, checksummed batches. Retrying the same batch is safe only
   when its idempotency key and body hash are unchanged.
3. Complete the run. The service verifies contiguous batches, counts, checksums,
   and scope before committing live rows and the source cursor in one database
   transaction.

Incremental absence never deletes an existing transaction. Deletion requires an
explicit `SKIP`/tombstone, or a complete full rebuild of the declared scope.
Database uniqueness constraints prevent duplicate runs, batches, transactions,
and category assignments.

The manifest proves that the API received and committed the uploader's declared
payload. It cannot prove upstream account completeness unless the financial data
source itself supplies a trustworthy cursor, item count, or equivalent coverage
signal.

## Run locally with Docker Compose

Requirements: Docker Engine with Compose v2.

```sh
cp .env.example .env
# Edit .env and replace every placeholder with a unique secret.
docker compose up --build -d
docker compose ps
```

The default API address is `http://127.0.0.1:8080`; PostgreSQL has no host port.
The migration container exits successfully after `alembic upgrade head`, while
the API and database remain running.

Health endpoints:

- `GET /health/live` checks that the process is alive.
- `GET /health/ready` checks database connectivity. Compose starts the API only
  after the one-shot migration has succeeded.

Application endpoints are under `/v1`. A dashboard integration reads
`GET /v1/dashboard/budgets` using `Authorization: Bearer <read-key>`.

Stop the stack with `docker compose down`. The named PostgreSQL volume is kept;
do not add `--volumes` unless you deliberately intend to erase local data.

## End-to-end local demo

The repository includes an API-backed dashboard prototype and a synthetic
uploader that exercises the same config, batch, checksum, manifest, and commit
flow intended for the AI uploader. The demo sends 61 transactions in three
batches and covers multi-label classification, pending spending, a partial
refund, and skipped transactions without using any real financial data.

See [`demo/dashboard/README.md`](demo/dashboard/README.md) for the isolated
Compose project, uploader, and loopback-only preview instructions. The browser
page calls the live dashboard endpoint through a server-side read-key proxy;
the credential is not embedded in the JavaScript bundle.

## Configuration

| Variable | Purpose |
| --- | --- |
| `API_KEY` | Master Bearer key with read, write, and admin access; the only required setting in personal mode |
| `DATABASE_URL` | Defaults to `sqlite:////data/budget.db`; set a PostgreSQL URL for advanced mode |
| `BUDGET_READ_API_KEY` | Optional read-only dashboard key |
| `BUDGET_WRITE_API_KEY` | Optional refresh-run and batch upload key |
| `BUDGET_ADMIN_API_KEY` | Optional rule and administrative key |
| `CORS_ALLOWED_ORIGINS` | Defaults to `*`; optionally set comma-separated exact browser origins |
| `APP_ENV` | Runtime environment, normally `production` on TrueNAS |
| `LOG_LEVEL` | Application log level, normally `INFO` |
| `MAX_BATCH_ITEMS` | Maximum transactions accepted in one upload batch; default `250` |
| `MAX_REQUEST_BYTES` | Maximum declared HTTP request size; default `262144` bytes |
| `STALE_AFTER_HOURS` | Age after which dashboard freshness is reported stale; default `36` |
| `PUBLIC_BASE_URL` | Public HTTPS origin that enables ChatGPT OAuth and `/mcp`; omit to disable remote MCP |
| `OAUTH_CONSENT_SECRET` | Optional owner-only consent password; defaults to `API_KEY`, and is required in role-key-only mode |
| `OAUTH_AUTHORIZATION_CODE_TTL_SECONDS` | Authorization-code lifetime; default `300` |
| `OAUTH_ACCESS_TOKEN_TTL_SECONDS` | MCP access-token lifetime; default `900` |
| `OAUTH_REFRESH_TOKEN_TTL_SECONDS` | Rotating refresh-token lifetime; default `7776000` |
| `MCP_MAX_REQUEST_BYTES` | MCP JSON-RPC envelope limit; default `524288` bytes |

For backward compatibility, an existing deployment may omit `API_KEY` only when
all three role-specific keys are configured. Every configured key must be unique
and at least 24 characters.

`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` configure the database
container. The password embedded in `DATABASE_URL` must match and must be URL
encoded if it contains reserved URL characters.

## TrueNAS SCALE

The primary TrueNAS path uses the guided Custom App form, one `/data` ixVolume,
and one required `API_KEY`; no YAML or manually-created files are needed. The
complete UI fields, domain setup, backup notes, and the optional PostgreSQL YAML
path are in [`deploy/truenas/README.md`](deploy/truenas/README.md).

Publishing a GitHub Release runs the included workflow to build provenance and
SBOM-enabled `linux/amd64` and `linux/arm64` images in GHCR. A fixed patch tag
provides reproducible installs; a minor channel tag such as `0.3` lets a Guided
Custom App use TrueNAS's built-in Docker image update detection for later
`0.3.x` releases without following `latest` across feature lines.

## Privacy and logs

- Never commit `.env`, database dumps, bank exports, account identifiers, or real
  transaction JSON. The ignore files provide defense in depth, not permission to
  place sensitive files in this directory.
- API logs should contain request/run IDs, states, and aggregate counts only—not
  authorization headers, transaction bodies, merchant descriptions, or account
  identifiers.
- Only synthetic fixtures belong in public tests.
- Before making a fork public, scan the entire Git history for secrets and
  financial data. If anything sensitive ever entered history, rotate it before
  publishing; deleting the latest file is not sufficient.

See [SECURITY.md](SECURITY.md) for vulnerability and accidental exposure
reporting.

## License

[MIT](LICENSE)
