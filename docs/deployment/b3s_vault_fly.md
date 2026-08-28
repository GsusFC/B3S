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

## Current state: operational Vault beta

`fly.vault.toml` requires PostgreSQL and enables persisted Vault capture; SV9 judgment memory runs only after Flow/SV9 publication and cannot change it.
Verified-raw remains false with an empty socket; no worker, diagnostics, role, secret, or provisioning is enabled.

The release fails closed: build identity; migration URL environment; target-profile migration; then exact head-`031` verification.
The profile reads endpoint, database, user, project, branch, and server port on the DDL connection before its lock; production stays SELECT-only.

## Provisioning order

1. Confirm the approved `b3s-vault` Neon target; never use production or a `--database-url` override.
2. Provision Vault `B3S_DATABASE_URL` through Fly secret management; the release passes it only as `B3S_MIGRATION_DATABASE_URL`.
3. The separately authorized exact-SHA release applies immutable migrations `001`–`031`, verifies head `031`, and provisions neither roles nor workers.
4. Copy provider secrets without printing; set dedicated `B3S_EVIDENCE_ADJUDICATION_TOKEN` and `B3S_EVIDENCE_REVIEWER_ID`, never reusing `B3S_SCANNER_API_TOKEN`.
5. Create the `b3s_vault_data` volume in `cdg`.
6. After authorization, validate and deploy only the clean detached full SHA:

   ```bash
   DEPLOY_SHA="<authorized-full-40-char-SHA>" && git checkout --detach "$DEPLOY_SHA" && observed_head="$(git rev-parse HEAD)" && test "$observed_head" = "$DEPLOY_SHA" && worktree_status="$(git status --porcelain)" && test -z "$worktree_status" && fly config validate -a b3s-vault -c fly.vault.toml && fly deploy --remote-only -a b3s-vault -c fly.vault.toml --build-arg B3S_BUILD_SHA="$DEPLOY_SHA"
   ```

7. Verify `/health`, deployed commit, storage mounts, migration head, and database isolation before field scans.

Do not run a Vault deployment with the default `fly.toml`: that file targets
production.

## Deployment and rollback NO-GO

A deploy is **NO-GO** while the target cannot be attested, head `031` cannot be verified, or Vault-only flags drift. A release failure leaves the existing Machine in place.

No older image is assumed compatible with head `031`; this runbook authorizes
no image-only rollback. Prefer a reviewed forward fix. Any coordinated
image-plus-database restore requires separate authorization and proof against a
preserved restore anchor. Never down-migrate in place and never point
`b3s-vault` at production as a shortcut. Preserve the database branch and Fly
volume, stop new writes, and reconcile from the declared anchor.
