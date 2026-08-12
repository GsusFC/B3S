# Evidence Vault operational C7 cutover controls v1

Status: **retired**.

The C7-specific master switch, emergency deny, allowlist, runtime snapshot, and
separate API projection described by the former version of this document were
removed. They incorrectly allowed one scored tile to suppress or stop normal
Vault work.

C7 is now governed by the active
[`evidence_vault_c7_product_contract_v1.md`](evidence_vault_c7_product_contract_v1.md):
it remains a fully functional scored Coherencia tile and never acts as a special
operational blocker.

Existing migrations and historical artifacts remain immutable. The private
verified-raw shadow evaluator is a provenance diagnostic only; its result has no
scan, report, API, UI, deployment, or availability authority.
