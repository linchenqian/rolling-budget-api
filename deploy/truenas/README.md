# Deploy on TrueNAS SCALE

The recommended installation is the TrueNAS **Guided Custom App**: one API
container, one automatically created ixVolume mounted at `/data`, and SQLite in
that volume. It does not require creating a dataset, environment file, or
PostgreSQL container by hand.

This path targets TrueNAS SCALE 24.10 or newer. TrueNAS documents the guided
wizard under **Apps → Discover Apps → Custom App** in its
[Custom Apps guide](https://apps.truenas.com/managing-apps/installing-custom-apps/).

> Do not enter `latest` or the literal placeholder `<GUIDED_RELEASE_TAG>`.
> Wait for the first release that explicitly includes Guided App/SQLite support,
> then use that fixed release tag in TrueNAS.

## Guided installation at a glance

The complete field-by-field instructions are in
[`GUIDED_APP.md`](GUIDED_APP.md). These are the only values that require a
decision:

| Setting | Value |
| --- | --- |
| Image repository | `ghcr.io/YOUR_GITHUB_USERNAME/rolling-budget-api` |
| Image tag | A fixed Guided App release tag, for example `0.2.1` |
| Required environment variable | `API_KEY=<one long random secret>` |
| Container/host port | `8000` → `18080/TCP` |
| Custom User | Off (use the image's default root runtime) |
| Storage | One ixVolume mounted at `/data`, with Enable ACL off |
| Public hostname | `budget.example.com` |

All other environment settings have container defaults. In particular, the
default database is `sqlite:////data/budget.db` and the default CORS policy is
`*`. CORS can be narrowed later to the exact dashboard browser origin.

Keep **Privileged** off and do not add capabilities. Mount only the dedicated
ixVolume at `/data`; do not add host paths, TrueNAS system directories, or the
Docker socket. These limits prevent the container from receiving broad host
access and expose only its dedicated budget-data volume.

After the app reports Running, verify it from the TrueNAS network:

```sh
curl --fail http://TRUENAS_LAN_IP:18080/health/live
curl --fail http://TRUENAS_LAN_IP:18080/health/ready
```

Your public hostname requires a separate DNS record, TLS certificate, and reverse
proxy. The TrueNAS Portal field only creates a convenient link; it does not
provision DNS, HTTPS, or a proxy. Proxy HTTPS traffic to
`http://TRUENAS_LAN_IP:18080`, preserve the `Authorization` header and `OPTIONS`
requests, and do not expose port 18080 directly to the internet.

## ixVolume warning

The guided ixVolume is intentionally the simplest deployment, but TrueNAS
recommends ixVolumes primarily for testing and recommends Host Path storage for
long-lived production data. For this personal deployment:

- Never select **Remove ixVolumes** when deleting or reinstalling the app unless
  the budget database is intentionally being destroyed.
- Snapshot and replicate the Apps pool/ixVolume regularly, and test a restore.
- Take a fresh snapshot or backup before every image update.
- An app rollback can include ixVolume state, but rollback is not a substitute
  for an independent backup.

TrueNAS documents ixVolume rollback and deletion behavior in
[Managing Installed Apps](https://apps.truenas.com/managing-apps/managing-installed-apps/).

## Advanced: PostgreSQL with Compose YAML

The checked-in [`compose.yaml`](compose.yaml) remains available for an advanced,
multi-container deployment with a dedicated PostgreSQL service, host-path
datasets, and a one-shot migration service. It is not needed for the Guided
Custom App and should not be pasted into the guided form.

Choose this path only if PostgreSQL, independently managed database backups, or
host-path datasets are more important than the single-container setup. Before
using it:

1. Replace every `YOUR_POOL` with the actual pool name.
2. Replace `OWNER` with your GitHub username.
3. Replace `latest` in both API image references with the same tested release
   tag or digest.
4. Create the `config`, `postgres`, and `backups` host-path datasets referenced
   by the YAML.
5. Create the `postgres.env` and `app.env` files referenced by the YAML, using
   distinct production secrets and restrictive permissions.

Install that file through **Apps → Discover Apps → ⋮ → Install via YAML**. The
database must become healthy, the `migrate` service must exit with code 0, and
the API must become healthy. The PostgreSQL deployment's host paths, permissions,
logical backup, and rollback procedures must be managed separately; an app image
rollback does not roll a host-path database backward.

Official TrueNAS references:

- [Installing Custom Apps](https://apps.truenas.com/managing-apps/installing-custom-apps/)
- [App storage](https://apps.truenas.com/getting-started/app-storage/)
- [Managing Installed Apps](https://apps.truenas.com/managing-apps/managing-installed-apps/)
- [TrueNAS configuration backups](https://www.truenas.com/docs/scale/25.10/gettingstarted/configure/setupbackupscale/)
