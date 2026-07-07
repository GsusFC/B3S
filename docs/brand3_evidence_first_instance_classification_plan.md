# B3S — First-instance evidence classification hardening (implementation plan)

Status: ready to implement (C1 first; C2 after C1 lands)
Motivation: post-Toteemi analysis of the SV9 Flow evidence layer. First-instance
classification today is lexical and unipolar: source class comes from provider
names, intents are trusted from upstream payloads, confidence is mostly a fixed
"medium", and absence is a 6-word keyword miss on substring-matched URLs. That
produces both false positives (homonym noise counted as external proof, cheap
`verified_absent` claims) and false negatives (homepage truncated to 700 chars,
substring URL markers misfiring).

Each workstream below is an independent PR. Order: C1 → C2.

Out of scope for both workstreams:
- The LLM labeling pass (stance/polarity, semantic block relevance) — layer 3,
  design pending, do not start it.
- Detection gates, warn mode, scoring, rubric semantics.
- SearchAPI confidence policy beyond the identity rule below.
- Nav-vs-body text separation and boilerplate heuristics
  (`_strip_cross_page_boilerplate` stays as is).
- No new dependencies. No config changes.

Working-tree note: `evidence_worker.py`, `evidence_coverage.py`,
`source_policy.py` and their test files are clean; `web/templates/report.html.j2`
carries unrelated in-flight changes — C2 touches one line there, coordinate or
rebase when landing C2.

---

## C1 — Identity match for external evidence, homepage chunking, token-boundary surface markers

All changes in `src/sv9_flow/evidence_worker.py` plus
`tests/test_sv9_flow_evidence_worker.py`. Three independent sub-changes, one PR.

### C1a — `identity_match` on external-proof records

#### Problem (verified)

Exa/SearchAPI/GitHub results become `external_proof.<intent>` records with
medium/high confidence and count into `external_proof_count` without any check
that they concern the scanned brand. Additionally
`src/sv9_flow/evidence_worker.py:213-214` trusts the upstream `intent` string:
`intent == "owned_confirmation"` grants `source_class="owned_copy"` (owned
authority) with no domain validation.

What this rule can and cannot catch (state this in a code comment): it
separates "does not even mention the brand" (`none`) from "mentions the brand
name" (`brand_name`) from "is the brand's own domain" (`domain`). A true
homonym (different company, same name) still passes `brand_name` — resolving
that needs the layer-3 semantic pass and is out of scope here.

#### Change

1. New pure helper in `evidence_worker.py`:

   ```python
   def _identity_match(*, brand_name: str, scan_url: str, record_url: str, content: str) -> str
   ```

   Returns one of `"domain" | "brand_name" | "none" | "unverified"`.

   - `"domain"`: normalized host of `record_url` (lowercase, strip a leading
     `www.`) equals the normalized scan host, or ends with `"." + scan_host`.
     Empty/unparseable `record_url` or `scan_url` → never `domain`.
   - `"brand_name"`: normalize the brand name (lowercase, fold accents,
     collapse whitespace, strip a trailing legal-suffix token from:
     `s.l.`, `sl`, `s.a.`, `sa`, `inc`, `inc.`, `llc`, `ltd`, `gmbh`) and
     match it as a whole-word phrase (word boundaries on both ends) against
     the accent-folded, whitespace-collapsed content. For single-token brand
     names, additionally match the token inside `record_url` delimited by
     `[-/._]` (e.g. `trustpilot.com/review/toteemi.com`).
   - `"unverified"`: the normalized brand name has fewer than 3 characters
     (too generic to match safely) AND there is no domain match. Callers must
     NOT degrade confidence on `unverified`.
   - `"none"`: everything else.

2. Apply it in `_evidence_from_exa_payload`, `_evidence_from_searchapi_payload`
   and `_evidence_from_github_payload` (thread `brand_name` and scan `url`
   down from `build_evidence_pack_from_snapshot` — the private helper
   signatures change, that is fine):

   - Every external result record gets `metadata["identity_match"]`.
   - `identity_match == "none"` → force `confidence="low"` (overrides the
     Exa-score / GitHub-star / fixed-medium value). Never drop the record.
   - Exa `owned_confirmation` intent: `source_class="owned_copy"` ONLY when
     `identity_match == "domain"`. Otherwise `source_class="external_proof"`
     plus `metadata["intent_demoted"] = "owned_confirmation_without_domain_match"`
     (keep the original intent in `metadata["intent"]`).
   - GitHub repo records: match brand against `full_name` + description (the
     record content already contains both); `none` → `confidence="low"`.
   - Acquisition attempt / diagnostic records (`acquisition_metadata`):
     untouched — no identity fields.

#### Tests (extend `tests/test_sv9_flow_evidence_worker.py`)

- `_identity_match` matrix: exact host, `www.` host, subdomain
  (`blog.brand.com` vs `brand.com`) → `domain`; unrelated domain with brand
  phrase in content (accented brand "Café Río" matches content "cafe rio") →
  `brand_name`; single-token brand present only in URL path → `brand_name`;
  neither → `none`; 2-char brand without domain match → `unverified`.
- Exa result with no brand mention and foreign domain → record kept,
  `confidence == "low"`, `metadata["identity_match"] == "none"`.
- Exa `owned_confirmation` on the scan domain → `owned_copy`; on a foreign
  domain → `external_proof` + `intent_demoted` set.
- `unverified` does not degrade confidence.

### C1b — Chunk the homepage like subpages

#### Problem (verified)

`_evidence_from_web_payload` (`src/sv9_flow/evidence_worker.py:465`) emits a
single `homepage[:700]` record while subpages get up to 6×900-char chunks. A
single-page site is reduced to 700 characters total — strategy copy below that
ceiling is invisible to shortlists and the interpreter. Silent false negative:
blocks end as `insufficient_acquisition` with the copy sitting in the payload.

#### Change

- Chunk the homepage text with the existing `_chunk_text_by_section`
  (same `_MAX_WEB_SUBPAGE_CHUNKS` cap).
- Ref compatibility: the FIRST chunk keeps ref `raw_inputs.{index}` exactly as
  today; subsequent chunks use `raw_inputs.{index}.chunk.{n}` with `n`
  starting at 2. Chunk records carry the same url/metadata shape as today's
  homepage record (no `subpage_url`).
- `_strip_cross_page_boilerplate` keeps receiving the FULL homepage text as
  page one (unchanged behavior).
- Chunks flow through `_dedup_raw_input_records` like any raw-input record.

#### Tests

- Multi-section homepage beyond 900 chars → several records; first ref is
  exactly `raw_inputs.0`; content past the old 700-char ceiling is present in
  some record.
- One-page site (payload without the `## Subpage:` marker) → same as above
  (this is the Toteemi-class regression the change exists for).
- Short homepage → exactly one record, ref and content unchanged (no
  regression).

### C1c — Token-boundary strategic-surface markers

#### Problem (verified)

`_is_strategic_surface` (`src/sv9_flow/evidence_worker.py:816-818`) substring-
matches markers against the whole URL: "cultura" matches `/agricultura/`,
"about" matches `/about-cookies`, "jobs" matches `/steve-jobs-story`. A false
strategic surface emits absence records from an irrelevant page, which C2's
consumers escalate into absence claims. The plan that added the Spanish
markers already documented this risk class when excluding "mision".

#### Change

Rewrite `_is_strategic_surface`:

- Parse the URL with `urllib.parse`; take path segments only (ignore host,
  query, fragment — host inspection stays out of scope).
- Tokenize each segment on `[-_.]`, fold accents, lowercase.
- A segment matches when any of its tokens equals a marker token
  (markers deduped after accent-folding: about, company, careers, jobs,
  culture, values, mission, manifesto, nosotros, empleo, cultura, valores,
  quienes, filosofia, manifiesto) OR the segment starts with `sobre-` /
  `sobre_`.
- Segment blocklist wins over markers: if any token of the segment is in
  {cookie, cookies, privacy, privacidad, legal, terms, condiciones, aviso},
  that segment cannot match (other segments of the same path still can).
- Keep the deliberate exclusion of Spanish "mision" and its comment.
- Known accepted tradeoff (leave a short comment): person-name slugs like
  `/steve-jobs-story` still token-match "jobs"; C2's corroboration
  requirement is the mitigation, not URL parsing.

#### Tests

Extend the existing `_is_strategic_surface` matrix:

- Unchanged truths: `/sobre-toteemi/` True, `/quienes-somos/` True,
  `/filosofia/` True, `/manifiesto/` True, `/about` True, `/culture` True,
  `/misiones/` False.
- New: `/agricultura/` False, `/about-cookies` False (blocklist),
  `/cookies/about` False? — no: blocklist only vetoes its own segment, and
  `about` is a separate segment → True; assert that explicitly.
  `/our-values/` True, `/quiénes-somos` (accented) True, root `/` False,
  empty string False.

### Acceptance (C1)

- `make lint` clean.
- `./.venv/bin/python -m pytest tests/test_sv9_flow_evidence_worker.py -q` green.
- `make test` green.
- Diff touches ONLY `src/sv9_flow/evidence_worker.py` and
  `tests/test_sv9_flow_evidence_worker.py`.

---

## C2 — Graduated absence: `verified_absent` requires corroboration

### Problem (verified)

One keyword miss (6 terms) on ONE substring-matched page flips block coverage
to `verified_absent` (`src/sv9_flow/evidence_coverage.py:103-104`) — the
strongest negative claim in the system built on the weakest signal. A brand
expressing values as "lo que nos mueve" / "what drives us" gets a verified
negative. Consumers that act on it: `src/sv9/source_policy.py:82` (owned
expression caps) and `web/templates/report.html.j2:6` (badge "ausencia
verificada").

### Change

1. `evidence_worker.py`: absence records gain
   `metadata["absence_signal"] = "keyword_miss"` — label what the signal
   actually is.
2. `evidence_coverage.py`:
   - `BlockCoverageStatus` gains `"probable_absent"`.
   - In `block_coverage`: count DISTINCT checked surfaces per block from the
     block's absence records (distinct non-empty `url` /
     `metadata["checked_url"]`). ≥2 distinct surfaces → `verified_absent`;
     exactly 1 → `probable_absent`; 0 → unchanged fallthrough
     (`insufficient_acquisition`). Precedence relative to `positive_evidence`
     and `implied_not_explicit` unchanged.
   - `coverage_limitations`: emit `coverage:{block}_probable_absent` for the
     new status (keep stable ordering).
3. `src/sv9/source_policy.py`: `probable_absent` does NOT apply the
   `verified_absent` expression cap — policy treats it like
   `insufficient_acquisition`. The whole point of C2 is not to over-assert
   from a weak signal. Add/extend the source_policy test suite (locate via
   grep) asserting the cap fires on `verified_absent` and does not on
   `probable_absent`.
4. `web/templates/report.html.j2`: add
   `"probable_absent": ("warn", "ausencia probable")` next to the existing
   mapping. NOTE: this file carries unrelated in-flight edits — one-line
   change, coordinate/rebase.

### Tests

- Coverage matrix in the evidence-coverage test suite: two absence records for
  the same block from two distinct URLs → `verified_absent`; from one URL (or
  two records with the same URL) → `probable_absent`; none + strategic-surface
  attempt record → `insufficient_acquisition`.
- `coverage_limitations` emits the new string.
- Source-policy cap behavior as described above.

### Acceptance (C2)

- `make lint` clean; targeted suites green; `make test` green.
- Report template renders the new badge (existing template test suite if any).

---

## Explicitly deferred (layer 3 — design pending, do NOT implement)

A batched LLM labeling pass over the evidence pack emitting per-record
`{relevant_blocks, stance, identity_match, specificity}` — the change that
makes evidence bipolar (counter-evidence instead of negative-by-absence).
Decision gate: after C1+C2 land, measure how many blocks still end
`insufficient_acquisition` while relevant copy exists in the pack; that number
is the business case.
