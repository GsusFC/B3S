# B3S vault on Fly

`b3s-vault.fly.dev` is the isolated environment for validating persistent
evidence memory and reviewed shadow scoring. It must not share writable
storage or a PostgreSQL connection with `b3s.fly.dev`.

## Isolation contract

- Fly app: `b3s-vault`
- Fly config: `fly.vault.toml`
- Fly volume: `b3s_vault_data`
- PostgreSQL: a dedicated Neon branch or database
- Runtime authority: disabled by the existing shadow-mode contracts
- Reviewer credential: dedicated to Vault and different from the scanner token
- Production app: never targeted by vault commands

The vault intentionally runs the same application image and scanner behavior
as production. Its separation comes from infrastructure and data, not from a
second scanner implementation.

The vault permits a 90-second visual capture budget for field diagnostics;
production remains at 60 seconds. Both values use the same bounded capture
path and are controlled by `BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS`.

The current database branch is `b3s-vault`, derived once from the Neon
`production` branch. Provider credentials for LLM, Exa, and Firecrawl may be
shared so field scans remain comparable. `B3S_DATABASE_URL`, the Fly volume,
`B3S_SCANNER_API_TOKEN`, and `B3S_EVIDENCE_ADJUDICATION_TOKEN` must remain
vault-specific. Set `B3S_EVIDENCE_REVIEWER_ID` to the stable identity written
to review events. The adjudication and scanner tokens must never be equal.

The protected browser session uses the adjudication token only to authenticate
and sign a short-lived cookie. The token itself is not stored in the cookie.
Reviewer routes are disabled unless `BRAND3_ENVIRONMENT=vault`; accepted,
disputed, and rejected decisions remain append-only, non-authoritative shadow
events.

## Provisioning order

1. Create a Neon branch or database isolated from production.
2. Set `B3S_DATABASE_URL` on `b3s-vault` to that isolated connection.
3. Copy the required provider secrets to `b3s-vault` without printing them.
4. Set a dedicated `B3S_EVIDENCE_ADJUDICATION_TOKEN` and the stable
   `B3S_EVIDENCE_REVIEWER_ID`; do not reuse `B3S_SCANNER_API_TOKEN`.
5. Create the `b3s_vault_data` volume in `cdg`.
6. Validate and deploy with the explicit vault config:

   ```bash
   fly config validate -a b3s-vault -c fly.vault.toml
   fly deploy --remote-only -a b3s-vault -c fly.vault.toml
   ```

7. Verify `/health`, the deployed commit, storage mounts, and database
   isolation before running field scans.

Do not run a vault deployment with the default `fly.toml`: that file targets
the production app.
