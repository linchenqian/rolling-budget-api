# Security Policy

## Supported versions

Until the first stable release, only the latest published version is supported.
Security fixes will be released as a new tagged container image and source tag.

## Report a vulnerability privately

Use GitHub's private vulnerability reporting feature for this repository when it
is available. If it is not enabled, contact the maintainer privately through the
address listed on their GitHub profile. Do not open a public issue containing an
exploit, bearer key, deployment address, account identifier, merchant data, or
transaction sample.

Include the affected version, impact, minimal reproduction using synthetic data,
and any suggested mitigation. Do not access data that is not yours or perform
destructive testing.

## Accidental secret or financial-data exposure

If a credential or real financial record is committed or posted publicly:

1. Revoke or rotate the exposed credential immediately.
2. Disable public network access to the affected deployment if compromise is
   possible.
3. Preserve minimal audit evidence without copying transaction bodies.
4. Remove the material from the entire Git history and published artifacts.
5. Treat deletion alone as insufficient because forks, caches, and clones may
   retain the data.

API logs and security reports must redact authorization headers, API keys,
database URLs, account identifiers, merchant descriptions, and transaction
bodies.

## Deployment expectations

- Terminate HTTPS before the API and restrict it to a trusted LAN, VPN, or
  explicit source allowlist.
- Keep PostgreSQL on the private container network with no host port.
- Use different read, write, and admin API keys and rotate them independently.
- Store secrets outside Git with restrictive permissions.
- Back up and test restore procedures before upgrades.
- Pin production images to a reviewed tag or digest and run database migrations
  as the supplied one-shot service.
