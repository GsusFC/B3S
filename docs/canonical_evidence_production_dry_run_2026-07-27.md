# Production canonical-evidence dry-run — 2026-07-27

## Verdict

The read-only run confirms that temporal instability is not isolated to
Ticketeame. It also shows that activating `repeated` is a material product
change, so deployment must remain separately reviewable and reversible.

No production report, comparison, selection, or configuration was mutated
during this run.

## Scope

- Immutable reports read from the Fly PostgreSQL history: **112**
- Brand histories: **67**
- Repeated histories: **23**
- Single-scan histories: **44**
- Repeated histories whose visible report would change: **20**
- Repeated histories already showing the selected report: **3**

Among the 20 changed selections:

- the latest score was lower than the baseline in **8** histories;
- the latest score was higher in **11** histories;
- the score was equal in **1** history;
- score delta range, latest minus selected: **-34 to +17**.

This is expected under a truth-preserving policy: unexplained positive drift is
also drift. The policy does not choose the highest score; it chooses the
evidence baseline until a later promotion rule establishes a real change.

## Classifications

Across all 112 scans:

- `provisional`: 65
- `acquisition_regression`: 27
- `invalid`: 11
- `evaluation_drift`: 8
- `candidate`: 1

Historical reports currently use `shadow`/non-final reliability extensively,
so the selected references are usually provisional rather than canonical.

## Ticketeame

| Date | Report | Score | Classification | Decision |
| --- | --- | ---: | --- | --- |
| 2026-07-24 | `b43a17aeb14c` | 56 | `provisional` | selected baseline |
| 2026-07-25 | `afc52ca72d2c` | 47 | `candidate` | material evidence changed; do not auto-replace |
| 2026-07-26 | `e8b79509c38b` | 40 | `evaluation_drift` | equivalent evidence to the previous scan, different interpretation/evaluation |
| 2026-07-27 | `e38b2328997e` | 35 | `invalid` | broken evaluation plus external evidence not reacquired |

Under `repeated`, the brand page selects report `b43a17aeb14c` and score **56**.
The newest run remains visible in history and directly addressable.

The last run is not evidence that the brand deteriorated: it lost external
sources, reduced independent external coverage, and left Coherencia
`not_evaluated`.

## Rollout decision encoded in the branch

- Local/default mode: `observe`.
- Fly manifest: `repeated`.
- Single-scan histories remain unchanged.
- Rollback: set `B3S_CANONICAL_ENFORCEMENT_MODE=observe`.
- No deployment was performed as part of this dry-run.

The unresolved policy question is candidate promotion. The initial version
intentionally quarantines material changes instead of guessing that one new
capture is sufficient to replace the baseline.
