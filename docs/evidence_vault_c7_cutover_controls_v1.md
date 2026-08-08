# Evidence Vault operational C7 cutover controls v1

Status: implemented as a **default-deny Vault-only control plane**. This is not
cutover authorization and does not enable a deployment.

## Scope

These controls govern only the present-time effect of the Evidence Vault v2 C7
`all_of` authority. The legacy SV9 C7 tile, immutable reports, historical score,
API result payloads and Markdown exports are unchanged. Existing Vault packets,
adoption events and score evaluations also retain their original truthful
`production_runtime_effect=false` and `scanner_runtime_effect=false` fields.
A separate versioned runtime envelope is the only presentation authority.

## Decision contract

Operational C7 is effective only when every condition is true:

1. `BRAND3_ENVIRONMENT` is exactly `vault`;
2. `BRAND3_VAULT_C7_CUTOVER_ENABLED` is exactly `true`;
3. `BRAND3_VAULT_C7_EMERGENCY_DENY` is exactly `false`;
4. `BRAND3_VAULT_C7_ALLOWLIST` is a valid, non-empty comma-separated set of
   exact canonical domains; and
5. the requested canonical brand is an exact member.

Only lowercase `true` and `false` are valid booleans. A missing emergency switch
is engaged. A malformed emergency switch denies before all other reasons. A
missing master flag is off. Empty, partially malformed, wildcard, URL, IP,
Unicode, punycode, credential-bearing and port-bearing allowlists deny the
entire set; valid members are never salvaged from a malformed set. Membership
is exact, never a suffix or glob match.

The authorization parser is deliberately stricter than history grouping. It
accepts ASCII multi-label DNS identities and canonicalizes case, one leading
`www.`, and a trailing DNS dot. It rejects identities whose authority is
ambiguous.

## Effect boundaries

- **Scanner planning:** when denied, accepted operational C7 relations are
  excluded from incremental affected-tile derivation. Other Vault planning and
  the production legacy scanner are unchanged. Candidate packets remain
  complete 80-tile, non-authoritative artifacts. Controls are sampled again
  before persistence and return; a denied retry of a previously C7-bearing plan
  is explicitly blocked without a legacy fallback or another persistence write.
- **Lineage:** an accepted C7 row is insufficient. The repository walks exact
  immutable source packets and rederives exactly one C7 `all_of` group with
  exactly two frozen members (`owned_web` and `external_social_profile`), exact review events
  and current memory version. Missing or ambiguous lineage denies presentation.
- **Scoring/read:** denied requests perform no operational-memory read and no
  operational-score get-or-create. Allowed requests recheck controls before the
  score-write boundary and again before presentation, then bind the envelope to
  the memory version, evaluation identity and score-input fingerprint.
- **API:** `GET /api/v1/brands/{domain}/operational-c7` is authenticated,
  `no-store`, and returns a typed, brand-current projection. Denied, unavailable
  and non-allowlisted cases share the same 404 shape so the allowlist is not
  enumerable. Immutable scan result endpoints are not overlaid.
- **UI:** `/brand/{domain}` is `private, no-store`, renders the operational C7
  block only through the same guarded adapter, and labels it separately from
  historical SV9.

The emergency switch is read at effect boundaries rather than frozen at module
import. The raw allowlist is never returned by a decision, API payload or UI.

## Default state

`.env.example` and `fly.vault.toml` encode:

```text
BRAND3_VAULT_C7_CUTOVER_ENABLED=false
BRAND3_VAULT_C7_EMERGENCY_DENY=true
BRAND3_VAULT_C7_ALLOWLIST=
```

No C7 operational effect is therefore possible by default. No equivalent flags
are added to the production Fly configuration.

## Remaining NO-GO conditions

The control plane does not solve raw acquisition replay, seed/export v2 of
legacy lineage, the latest-capture watermark, runtime score separation, or the
offline adapter extraction. A brand must not enter the allowlist until its exact
accepted group has replayable capture lineage and those separate gates are
closed. Enabling flags, deploying Vault, marking a PR ready, merging, or touching
production require separate explicit authorization, backup and rollback plans.
