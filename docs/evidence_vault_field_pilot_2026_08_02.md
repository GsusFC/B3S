# Evidence Vault field pilot — 2026-08-02

## Verdict

**GO for continued isolated Vault validation. NO-GO for production scoring
promotion.**

The pilot demonstrates that the Vault can persist exact human decisions,
reapply them only when the same candidate fingerprint is present, retain tile
history across scans, and keep both runtime authority and automatic scoring
effects disabled. It also found and fixed a real C6 evaluation defect without
deploying or changing the production scanner.

Production remained on commit `28560763f5e6765010259b44df9484d44f779447`.
The isolated `b3s-vault` deployment used
`db25b1df7c7a5e2ee3b387391877661729b16aa6` for the final field check.

## Scope

- Environments: `b3s-vault.fly.dev` for the pilot; `b3s.fly.dev` as an
  untouched production control.
- Brands: SoccerSolver and CausaPrima.
- Authority mode: shadow only.
- Human reviewer: `gsus`.
- Persisted human decisions: three.
- Production migrations, scoring promotion, and runtime activation: none.

## Human decisions

| Brand | Tile | Decision | Event ID | Result |
| --- | --- | --- | --- | --- |
| SoccerSolver | `coherencia.C6` | `rejected` | `4f668417-f89c-5b0d-bcbe-66187486e5a0` | Textual positioning alone does not prove visual/design-copy coherence. |
| SoccerSolver | `coherencia.C8` | `disputed` | `9ceff210-65d1-5aba-9c7d-2669b2eca5f0` | One owned-copy testimonial is insufficiently independent and attributed. |
| CausaPrima | `magnetism.MG3` | `accepted` | `00412469-5739-5bc9-b92a-f615ab703695` | Exact hook names identify a conflict-oriented offer for finance teams. |

All events retain `runtime_effect=false`, `authority=false`, and
`automatic_scoring_effect=false`.

An idempotent replay of the SoccerSolver C8 submission returned the original
event `9ceff210-65d1-5aba-9c7d-2669b2eca5f0` with `replayed=true`; it did not
append a duplicate event.

## Field scans

| Brand | Scan | Score | Visual acquisition | Comparison result |
| --- | --- | ---: | --- | --- |
| SoccerSolver | `d1866d9c39b0` | 61 | Missing; screenshot timeout | Pre-fix scan exposed the C6 textual false positive. Non-canonical acquisition regression. |
| CausaPrima | `76b281740607` | 91 | Blocked; coverage 0.55 | First scan under the current evaluation contract. Non-canonical acquisition regression. |
| CausaPrima | `c08a4847f063` | 91 | Missing; screenshot timeout | 80/80 tiles stable versus the preceding current-contract scan; no interpretation or evaluation change. |
| SoccerSolver | `794fea5b3e17` | 52 | Missing; screenshot timeout | Post-fix C6 field check. Non-canonical acquisition regression. |

The two CausaPrima scans provide the strongest stability result: visual
acquisition changed from blocked to missing, but semantically equivalent
evidence produced the same score, the same MG3 tile version and 80 stable
tiles. Acquisition-path noise did not manufacture a brand-memory change.

The canonical guard also behaved correctly. Acquisition regressions remained
non-canonical and did not replace the stronger canonical baseline.

## Defect found and fixed

The C6 contract requires `sin_evidencia` when the visual capture cannot be
evaluated. The visual signal layer already emitted this limitation, but
`tile_signal_worker` discarded all visual signals whenever capture status was
not `usable`. The LLM could then mark C6 `ok` from literal owned text such as
“We apply the scientific method to football,” although no visual evidence was
available.

The minimal correction does two things:

1. Persist unusable visual acquisition as `visual_capture_limitation`
   metadata rather than semantic brand evidence.
2. Emit one high-confidence `insufficient_evidence` signal for
   `coherencia.C6` when the visual packet is valid but unusable. No other tile
   receives this guard.

The post-fix field scan `794fea5b3e17` exercised exactly that path:

- evidence packet: `missing`;
- screenshot: `timeout`;
- first fold evaluable: `false`;
- C6 result: `sin_evidencia`;
- C6 evolution: `ok -> sin_evidencia` with impact `acquisition_gap`;
- C6 rejected event: applicable again with the original case and candidate
  fingerprint;
- unreviewed memory preview: 52 -> 58;
- human-reviewed shadow preview: 52 -> 52.

This proves the decision is attached to the exact recoverable evidence, not to
the tile name alone, and that rejected evidence cannot silently restore the
score.

## Validation

- Full local suite: 2,406 passed, 5 skipped.
- Focused C6/evaluator tests: 72 passed.
- Related shadow/service/visual/web tests: 83 passed.
- Ruff on changed files: passed.
- `git diff --check`: passed.
- Fly release command, rolling deployment, machine checks, and DNS checks:
  passed.
- Vault health SHA: `db25b1df7c7a5e2ee3b387391877661729b16aa6`.
- Production health SHA remained
  `28560763f5e6765010259b44df9484d44f779447`.

## Remaining blockers before promotion

- Visual capture remains operationally unstable on both pilot brands. The C6
  guard now fails closed, but capture reliability still needs improvement.
- SoccerSolver has three unreviewed recovery candidates: one A4 candidate and
  two PE6 evidence variants.
- The latest SoccerSolver scan is an acquisition regression, so its broad
  score movement is not evidence of a genuine brand change.
- Claim-level review is not yet one unified end-user workflow.
- Human decisions remain shadow-only by design. No policy currently authorizes
  them to change production scoring.

The next justified step is to continue the Vault pilot with comparable scans
and improve visual acquisition reliability. Production promotion should wait
until repeated comparable scans, candidate review coverage, and an explicit
authority policy all pass.
