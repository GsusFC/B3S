# B3S Agent Instructions

## Scope guard

Complete the current task with the minimum sufficient change.

## Before editing

- Read the relevant code, tests, configuration, and owning documentation directly. Do not work from search snippets or guesses.
- Inspect the worktree. Preserve unrelated user changes and do not revert, overwrite, or reformat them.
- If the requirement is ambiguous or the premise is unverified, resolve that before building on it.
- State a minimal plan:
  - **Outcome** — the exact behavior requested
  - **Non-goals** — what this task will not do
  - **Files** — the smallest set expected to change
  - **Proof** — the check that will prove the change works
- Start with one implementation path. Split work only when the task has genuinely independent parts.

## While editing

- Reuse existing code, helpers, patterns, contracts, and test setup before adding anything new.
- Fix bugs at the root cause. Do not stack patches around a wrong premise.
- Add an abstraction, adapter, or configuration layer only for a second real caller, an existing architectural boundary, or an explicit requirement in this task.
- Preserve behavior outside the requested change.
- Do not design for rare or future cases nobody asked about.
- Do not refactor adjacent code unless it blocks the requested change. Report worthwhile cleanup separately.
- Remove code you replace. Keep an old path only when compatibility is an explicit requirement.

## B3S invariants

- B3S is an evidence lab, not a scorer with a crawler. Preserve the evidence-first flow described in `README.md`.
- A block may be `detected` only with content and evidence references. Ungrounded detections must not reach scoring as grounded evidence.
- `verified_absent` is derived deterministically from acquisition evidence. An LLM cannot self-certify absence.
- Keep immutable captures separate from versioned evaluations. Re-analysis must reuse frozen raw input when the contract requires it.
- Preserve traceability across evidence, claims, tiles, evaluations, scores, and publication decisions.
- Systems explicitly marked `shadow`, `lab`, `preview`, diagnostic, or non-authoritative must not affect public scoring, canonical selection, or publication unless the task explicitly changes that authority.
- Preserve the contracts and authority boundaries of Phase Zero, Phase One, and Phase Two. If the owning contract is unclear, identify it before editing.
- Scoring authority, canonical report selection, evidence acceptance, and publication behavior must fail closed when required evidence or review is missing.
- Policies and thresholds belong in their existing versioned contract or policy-data mechanism. Do not hide policy changes inside unrelated implementation code.
- Prefer frozen fixtures and deterministic replay over live crawling or provider calls. Do not spend provider credits or refresh external evidence unless the task requires live acquisition.

## Pause and confirm

Read-only discovery is always allowed. If the task has not already authorized it, get approval before:

- Materially expanding the scope or touching unrelated files
- Adding a dependency, framework, service, pipeline, or new test infrastructure
- Changing a public API, schema, migration, storage format, wire format, or persisted contract
- Changing scoring authority, canonical selection, publication gates, or a shadow/non-authoritative boundary
- Deleting or overwriting user data, discarding uncommitted work, rewriting history, or dropping data
- Keeping two implementations of the same behavior alive
- Running a live crawl, external acquisition, deployment, or operation that consumes provider credits

## Testing

- Run the narrowest existing tests that exercise the changed behavior.
- Extend the most relevant existing test before creating a new test file.
- For a bug fix, add the narrowest regression test when feasible unless existing coverage already reproduces the failure.
- Add a test for changed user-observable behavior when existing coverage does not protect it.
- Each new test must protect a clear acceptance criterion, invariant, or regression risk.
- Do not backfill unrelated coverage or introduce test infrastructure for this task alone.
- Do not use passing tests as justification for extra abstractions or scope.
- For evidence-flow changes, prefer the relevant frozen envelope or fixture before any live scan.
- The strategic benchmark in CI is informative until its fixture/checker contract is recalibrated. Do not expand an unrelated task to repair it.

Canonical repository checks:

```bash
python -m ruff check .
python -m pytest -q
```

Use the repository environment when available, for example:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/path_to_relevant_test.py -q
```

Run the full suite for broad or cross-cutting changes. A narrow change may finish with narrower checks when they prove the acceptance criteria; report what was and was not run.

## Project sources of truth

- `README.md` — product thesis, evidence-first architecture, setup, and runtime overview
- The relevant file under `docs/` — contract-specific behavior and authority
- `.github/workflows/ci.yml` — canonical CI checks
- `DESIGN.md` — active UI and visual-system rules

Read the source that owns the behavior instead of copying its details into new documentation or code comments.

## If the plan grows

Stop when the work starts adding future-use layers, workaround stacks, unrelated cleanup, or tests for unstated behavior.

Explain why the original scope is insufficient, propose the smallest revised plan, and confirm any expanded scope before continuing.

## Done means

- The requested behavior works and the acceptance criteria are met
- Relevant checks pass, with the exact commands and results reported
- Every touched file is necessary and the diff contains nothing unrelated
- No debug code, backup copies, dead paths, or scratch files remain
- Assumptions, limitations, and unverified runtime behavior are stated plainly
