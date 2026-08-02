# Visual capture resilience — 2026-08-02

## Scope

This change hardens the Visual Signature capture path without changing the
functional scanner's scoring contract. It is intended for the isolated Vault
deployment first; production remains on the existing capture behavior until a
field comparison is approved.

## Runtime behavior

- Playwright allocates a stable raw-viewport checkpoint before the child
  process starts. If structural capture exceeds the budget, the parent keeps
  the checkpoint only when it is a valid PNG.
- A recovered viewport is returned as `capture_recovery=raw_viewport_checkpoint`
  with `section_capture_status=timeout` and an explicit non-authoritative
  integrity note. The existing visual scoring path treats the limitation as
  unavailable visual analysis, so a partial artifact cannot silently create a
  positive score.
- Structural capture still derives its complete HTML section/segment plan. At
  runtime it captures at most five high-value segments (first viewport, hero,
  strategic labels, footer), preserves the complete plan in the manifest, and
  marks each segment with `selected_for_capture`.
- When no full-page master exists, sections fully contained in captured
  segments are persisted as `capture_source=page_segment`; the manifest is
  `partial`, never `complete`.
- The normalized evidence fingerprint includes recovery and section status, so
  a recovered/partial artifact cannot be confused with a complete capture of
  the same page.

## Guardrails

- No production route, database schema, scorer, or runtime authority flag is
  changed by this branch.
- The parent process validates the checkpoint with Pillow before returning it.
- The checkpoint and structural errors remain traceable in the screenshot
  diagnostic, Visual Signature shadow payload, and evidence contract.

## Verification

- Focused capture/evidence tests: 97 passed (including the wrapper used by the
  main scanner).
- Full repository suite before deployment: 2,409 passed, 5 skipped.
- Vault field validation is recorded below once the branch is deployed.

## Vault field validation

Pending deployment and live comparison. The acceptance checks are:

1. a timeout no longer discards a valid viewport when one was written;
2. a page with more than five structural segments reports a bounded partial
   manifest rather than claiming a full-page master;
3. complete short pages retain the existing `complete` status and artifacts;
4. the functional production app remains unchanged.
