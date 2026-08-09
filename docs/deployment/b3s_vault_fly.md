# B3S vault on Fly

`b3s-vault.fly.dev` is the isolated deployment target for validating persistent
evidence memory and reviewed Vault behavior. It must not share writable storage
or a PostgreSQL connection with `b3s.fly.dev`.

## Isolation contract

- Fly app: `b3s-vault`
- Fly config: `fly.vault.toml`
- Fly volume: `b3s_vault_data`
- PostgreSQL: a dedicated Neon branch or database
- Persistence authority: reviewed Vault journals may record accepted authority
  for their exact versioned subjects
- Runtime authority: no production, scanner, scoring, or presentation effect
- Reviewer credential: dedicated to Vault and different from the scanner token
- Production app: never targeted by Vault commands

The Vault runs the same application image and scanner behavior as production.
Its separation comes from infrastructure and data, not from a second scanner
implementation. Accepted Vault authority is durable history; it is not a C7
runtime cutover.

The Vault permits a 90-second visual capture budget for field diagnostics;
production remains at 60 seconds. Both use the same bounded capture path and
`BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS`.

The current database branch is `b3s-vault`, derived once from the Neon
`production` branch. Provider credentials for LLM, Exa, and Firecrawl may be
shared so field scans remain comparable. `B3S_DATABASE_URL`, the Fly volume,
`B3S_SCANNER_API_TOKEN`, and `B3S_EVIDENCE_ADJUDICATION_TOKEN` must remain
Vault-specific. Set `B3S_EVIDENCE_REVIEWER_ID` to the stable identity written
to review events. The adjudication and scanner tokens must never be equal.

The protected browser session uses the adjudication token only to authenticate
and sign a short-lived cookie; the token is not stored in the cookie. Reviewer
routes are disabled unless `BRAND3_ENVIRONMENT=vault`. The general evidence
review journals remain append-only and non-authoritative; operational Vault
adoption has its own explicit, versioned authority contract.

## Current state: dormant, no cutover

`fly.vault.toml` keeps `BRAND3_VAULT_C7_CUTOVER_ENABLED=false`, emergency deny
on, and the allowlist empty. It also disables verified-raw acquisition shadow
and leaves its worker socket blank. This deployment launches no acquisition
worker and does not enable the C7 runtime snapshot. The verified provenance,
shadow-readiness, and PostgreSQL repository code can therefore be exercised by
tests without claiming that operational C7 is active on Fly.

The release command verifies immutable build identity, then applies the
packaged PostgreSQL migrations `001` through `021` before replacing the app
Machine. Migrations `019` and `020` have role prerequisites documented in
[`evidence_vault_provenance_role_runbook_v1.md`](../evidence_vault_provenance_role_runbook_v1.md);
the Fly config does not provision those roles, logins, keys, or worker secrets.

## Provisioning order

1. Create a Neon branch or database isolated from production.
2. Satisfy the migration-role prerequisites, then set Vault's
   `B3S_DATABASE_URL` to that isolated connection.
3. Copy required provider secrets without printing them.
4. Set a dedicated `B3S_EVIDENCE_ADJUDICATION_TOKEN` and stable
   `B3S_EVIDENCE_REVIEWER_ID`; do not reuse `B3S_SCANNER_API_TOKEN`.
5. Create the `b3s_vault_data` volume in `cdg`.
6. Validate and deploy with the explicit Vault config:

   ```bash
   fly config validate -a b3s-vault -c fly.vault.toml
   fly deploy --remote-only -a b3s-vault -c fly.vault.toml
   ```

7. Verify `/health`, deployed commit, storage mounts, migration head, and
   database isolation before field scans.

Do not run a Vault deployment with the default `fly.toml`: that file targets
production. Deployment alone does not authorize C7 cutover.
