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
- Only transactions matching at least one configured category are uploaded.
  Unmatched source transactions are omitted and never stored.
- Normal refreshes are incremental. A classification or statistics rule change
  creates a new rule version and requires a full rebuild.
- When active is the editing base and rule semantics are unchanged, budget limit,
  category name, icon, and display-order changes take effect immediately without
  reclassifying transactions. If pending is the editing base, those edits remain
  isolated with pending and become visible only when a successful full rebuild
  activates the complete configuration atomically. Classification instructions,
  lookback, enablement, category-set, timezone, currency, and aggregation changes
  create a pending configuration.
- The first configuration becomes active immediately, but the first transaction
  sync is still a full rebuild because no live transaction set exists yet.
- The service has one global user dataset. Configuration contains neither a
  user/scope key nor a persistent configured-account list.
- The uploader enumerates the connected accounts at the start of each run and
  submits their stable source IDs. The service freezes that set for commit
  validation. Optional account names are display labels only and never identity
  keys. If the set differs from the previous committed run, a full rebuild is
  required.
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
separate `budget:read`, `budget:refresh`, and `budget:config` scopes; it never
gives ChatGPT the master API key. `budget:config` permits the guarded
`update_config` tool, which requires a complete replacement configuration and
the current pending-or-active `config_hash`. This public hash covers the complete
editable snapshot, including budget and presentation fields, and changes after
every successful edit. The tool is intended only for a user's direct settings
request; transaction, merchant, account-label, and other financial-source
content must never trigger it. OAuth credentials are opaque,
stored only as keyed digests, and remain valid across container restarts. The
owner approves the initial link on a private consent page using the master key
or an optional separate `OAUTH_CONSENT_SECRET`. Deployments that use only
role-specific API keys must configure the separate consent secret. The
container needs outbound HTTPS access to `chatgpt.com` to validate ChatGPT's
client metadata during authorization.

Existing OAuth tokens keep their original scopes after an upgrade. A connection
authorized only for read/refresh cannot use a refresh token to add
`budget:config`; reconnect and approve the new scope explicitly before asking
ChatGPT to change settings. No additional environment variable is required.

The REST configuration API uses the same compare-and-set rule. Read
`GET /v1/config`, use `pending` as the editing base when present and otherwise
`active`, then send its `config_hash` in `If-Match` with the complete
`PUT /v1/config` replacement. Only the first configuration omits `If-Match`.
The hash covers every editable field, so even a budget-only change invalidates a
stale writer.

## Refresh integrity protocol

Large refreshes use three steps:

1. Create a refresh run with its rule version, mode, exact account-ID set, and
   idempotency key.
2. Upload numbered, checksummed batches. Retrying the same batch is safe only
   when its idempotency key and body hash are unchanged.
3. Complete the run with the intended batch count and exact completed-account
   set. The service verifies that every declared contiguous batch arrived, then
   commits the staged rows and refresh revision in one database transaction.

Incremental absence never deletes an existing transaction. Deletion requires an
explicit source-provided `pending_source_id` replacement, or a complete full
rebuild of the single user's data.
Database uniqueness constraints prevent duplicate runs, batches, transactions,
and category assignments.

The commit proves that the API received every batch and account completion the
uploader declared. It cannot independently prove that the financial source
returned every bank transaction; that responsibility remains at the source-reading
boundary.

Each uploaded transaction uses `account_id + source_id` as its stable identity.
`account_name`, `name`, and `merchant` are optional display/classification data and
never identity keys. `pending_source_id` is accepted only as an explicit source
link from a posted transaction to its earlier pending identity; the service never
guesses that link from amount, merchant, or date. A transaction's `categories`
array may contain multiple configured category keys. Uploaded `amount` and
`currency` must be normalized to the configuration's display currency; an
approximate but consistent conversion is sufficient for this personal dashboard.

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
uploader that exercises the same config, staged-batch, completed-account, and
atomic-commit flow intended for the AI uploader. The demo sends 18 matching
transactions and covers multi-label classification, pending spending, and a
partial refund without using any real financial data.

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
| `OAUTH_FORM_ACTION_ORIGINS` | Comma-separated exact HTTPS OAuth callback origins; defaults to `https://chatgpt.com` |
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
