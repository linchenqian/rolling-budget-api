# Dashboard demo

This Vite page is a narrow, embeddable budget table modeled on the selected
Embedded dashboard concept. It loads real data from
`GET /v1/dashboard/budgets`; no totals are hard-coded in the React app.

The demo server proxies `/api/*` to the local API and injects
`BUDGET_READ_API_KEY` (or the master `API_KEY`) on the server side. The key is
never compiled into the browser bundle. A production dashboard should use the same
server-side proxy pattern in production.

## Run the complete local flow

From the repository root, create a private `.env` from `.env.example`, replace
all placeholder secrets, and start an isolated Compose project:

```sh
docker compose -p rolling-budget-demo up --build -d
```

Load the local environment and run the synthetic uploader:

```sh
set -a
. ./.env
set +a
python3 scripts/demo_seed.py
```

The uploader uses the public API only. It creates five category rules, starts a
full refresh, uploads 61 synthetic transactions in three checksummed batches,
and commits the manifest. The dataset includes multi-label transactions,
pending spending, a partial refund, and skipped transactions.

Install and run the dashboard from this directory:

```sh
pnpm install
set -a
. ../../.env
set +a
pnpm run dev --host 127.0.0.1 --port 4173 --strictPort
```

Open `http://127.0.0.1:4173/`. The preview is intentionally loopback-only.

Stop the API stack while retaining its database with:

```sh
docker compose -p rolling-budget-demo down
```

Do not add `--volumes` unless deleting the entire demo database is intentional.

## Expected snapshot

The demo uses an explicit `as_of=2026-08-19` so its output stays reproducible.

| Category | Net spend | Budget | Notable behavior |
| --- | ---: | ---: | --- |
| Restaurant | $487.95 | $750 | Seven transactions, one pending, one partial refund |
| Dating | $75.76 | $500 | Two transactions also counted in Restaurant |
| Groceries | $312 | $600 | Four transactions, one pending |
| Coffee | $96 | $120 | Four transactions in a 14-day window |
| Entertainment | $265 | $200 | $65 over budget |

All merchant names, account identifiers, transaction identifiers, and
descriptions are synthetic.
