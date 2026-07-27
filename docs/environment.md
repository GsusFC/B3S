# Brand3 Environment Guide

This document describes local environment configuration for Brand3. It is documentation only: changing these values changes local execution, but this file does not change runtime behavior.

## Basic Usage

Use `.env.example` as the committed template and `.env` as your local private file.

```bash
cp .env.example .env
```

Put real provider keys only in:

```text
/Users/gsus/Antigravity/Brand3/brand3/.env
```

Do not paste real keys in chat, docs, tests, screenshots, commits, PR descriptions, or `.env.example`.

Rules:

- Commit `.env.example`.
- Never commit `.env`.
- Never commit `.env.local`.
- Never paste real provider keys, team tokens, or cookie secrets into docs, examples, logs, tests, screenshots, or PR descriptions.
- Use deployment platform secrets or GitHub Actions secrets for shared/deployed environments.

## Required For Provider-Backed Scoring

These are required when running full provider-backed scoring or collection flows.

| Variable | Purpose | Secret |
| --- | --- | --- |
| `FIRECRAWL_API_KEY` | Web scrape, social, and optional screenshot provider paths | Yes |
| `EXA_API_KEY` | Search, mentions, competitor/context discovery | Yes |
| `BRAND3_LLM_API_KEY` | OpenAI-compatible LLM provider key for scoring/report narrative/vision paths | Yes |

Fallback LLM key names accepted by `src/config.py`:

| Variable | Purpose | Secret |
| --- | --- | --- |
| `GEMINI_API_KEY` | Fallback LLM provider key | Yes |
| `GOOGLE_API_KEY` | Fallback LLM provider key | Yes |
| `OPENROUTER_API_KEY` | Fallback LLM provider key | Yes |

`BRAND3_LLM_API_KEY` takes precedence over the fallback names.

## Search Enrichment Lab Provider Keys

These variables are for isolated Lab/bake-off work only. They should not be wired into canonical Brand3 scoring, Brand Audit, Magnetism, Research Pack, public routes, or production API without an explicit promotion decision.

### Services With Existing Brand3 Key Slots

These are already part of the current Brand3 provider-backed setup:

| Variable | Provider | Current Role | Secret |
| --- | --- | --- | --- |
| `FIRECRAWL_API_KEY` | Firecrawl | Current web scrape/social/optional screenshot provider | Yes |
| `EXA_API_KEY` | Exa | Current search, mentions, competitor/context discovery provider | Yes |
| `SEARCHAPI_API_KEY` | SearchAPI.io | Optional vertical external-proof fallback; only used when `BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED=true` | Yes |
| `GITHUB_TOKEN` | GitHub REST API | Optional higher rate limit for repository proof; unauthenticated requests work for observed public repos | Yes |
| `BRAND3_GITHUB_PROOF_ENABLED` | Brand3 flag | Enables repository proof for GitHub links observed on owned pages; defaults to `true` | No |
| `BRAND3_LLM_API_KEY` | OpenAI-compatible LLM provider | Current scoring/report narrative/vision LLM key | Yes |

### New Lab Provider Key Slots

| Variable | Provider | Intended Lab Role | Secret |
| --- | --- | --- | --- |
| `PARALLEL_API_KEY` | Parallel | Shadow search/research comparison; already used by the Parallel shadow collector and bake-off script | Yes |
| `TAVILY_API_KEY` | Tavily | Search/extract/crawl/map comparison for agent-oriented research | Yes |
| `LINKUP_API_KEY` | Linkup | AI/RAG-oriented search and extraction comparison | Yes |
| `BRAVE_SEARCH_API_KEY` | Brave Search API | Independent web index and LLM-context search comparison | Yes |
| `SERPAPI_API_KEY` | SerpApi | SERP fallback/comparison for Google-like market visibility | Yes |
| `PERPLEXITY_API_KEY` | Perplexity Sonar | Cited answer/source discovery comparison; summaries must not be treated as final evidence | Yes |
| `CONTEXT_DEV_API_KEY` | Context.dev | Existing experimental visual/context enrichment scripts | Yes |
| `TINYFISH_API_KEY` | Tinyfish | Existing experimental fetch/probe scripts | Yes |

Lab outputs should normalize provider responses into source observations and write only to experimental artifacts or explicitly experimental database namespaces/tables.

Provider selection rule:

- If a Lab provider key is present in `.env`, that provider is eligible for Lab evaluation.
- If a Lab provider key is absent or blank, treat that provider as intentionally discarded for the current Lab cycle, not as a setup error.
- Context.dev is strategically interesting for identity discovery and Visual Signature enrichment, but should remain inactive unless credits/cost make testing viable.
- Google Search direct integrations and Gemini Deep Research are intentionally excluded from the Search Enrichment Lab for now. This does not remove Gemini as the configured LLM provider for analysis/narrative paths.

## Optional LLM And Vision Configuration

These values are safe to commit as placeholders or documented defaults, but not as part of a private `.env` if they reveal private infrastructure.

| Variable | Default / Example | Purpose |
| --- | --- | --- |
| `BRAND3_LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai` | OpenAI-compatible provider base URL |
| `BRAND3_LLM_MODEL` | `gemini-3.1-flash-lite` | Default scoring model |
| `BRAND3_LLM_CHEAP_MODEL` | `gemini-3.1-flash-lite` | High-volume extraction / low-risk checks |
| `BRAND3_LLM_PREMIUM_MODEL` | `gemini-3.1-flash-lite` | Final narrative / complex validation |
| `BRAND3_AUDIT_ANALYST_MODEL` | `BRAND3_LLM_CHEAP_MODEL` | Brand Audit executive analyst pass |
| `BRAND3_CLIENT_TLDR_V2_MODEL` | `BRAND3_LLM_MODEL` | Client-facing TLDR v2 prose |
| `BRAND3_MAGNETISM_EXTRACTOR_MODEL` | `BRAND3_LLM_PREMIUM_MODEL` | Magnetism layer extraction |
| `BRAND3_MAGNETISM_ANALYST_MODEL` | `BRAND3_LLM_PREMIUM_MODEL` | Magnetism analyst TLDR pass |
| `BRAND3_MAGNETISM_SYSTEM_READING_MODEL` | `BRAND3_LLM_PREMIUM_MODEL` | Magnetism system reading |
| `BRAND3_SV9_BASE_MODEL` | `BRAND3_LLM_CHEAP_MODEL` | SV9 base components and Pass 1 detection fallback |
| `BRAND3_SV9_REASONING_MODEL` | `BRAND3_LLM_PREMIUM_MODEL` | SV9 Magnetism and Coherencia reasoning components |
| `BRAND3_SV9_EDITORIAL_MODEL` | `BRAND3_LLM_MODEL` | SV9 founder-facing editorial messages and executive reading |
| `BRAND3_VISION_MODEL` | `gemini-2.5-flash` | Screenshot/vision analysis |
| `BRAND3_LLM_CALL_TIMEOUT_SECONDS` | `35` | Per LLM call timeout in `src/features/llm_analyzer.py` |
| `BRAND3_EVIDENCE_LLM_CLASSIFIER_ENABLED` | `false` | Enables the evidence vNext LLM semantic classifier in shadow mode only |
| `BRAND3_EVIDENCE_LLM_MODEL` | `gemini-3.5-flash` | Evidence vNext semantic classifier model |
| `BRAND3_EVIDENCE_LLM_BATCH_SIZE` | `4` | Evidence observations per classifier call |
| `BRAND3_EVIDENCE_LLM_TIMEOUT_SECONDS` | `20` | Timeout for one evidence semantic classifier call |
| `BRAND3_EVIDENCE_LLM_MAX_ATTEMPTS` | `2` | Attempts per evidence classifier batch before returning a shadow error |
| `BRAND3_EVIDENCE_LLM_NATIVE_STRUCTURED_OUTPUT` | `true` | Uses Gemini native structured output for the evidence classifier when the configured base URL is Google Gemini |

## Screenshot Provider

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCREENSHOT_PROVIDER` | `playwright` | Screenshot capture provider. `playwright` is the local default; `firecrawl` is an optional legacy/provider path. |

Visual Signature capture scripts may produce local screenshots and manifests under `examples/visual_signature/**`. Those artifacts must be classified before being committed.

## SQLite And Local Database

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAND3_DB_PATH` | `data/brand3.sqlite3` | SQLite database path for local scoring, web requests, runs, scores, features, evidence, and cache tables |

Policy:

- `data/` is local runtime state and is ignored.
- `*.sqlite`, `*.sqlite3`, `*.db`, WAL, and SHM files are ignored.
- Do not commit local DB files.
- If future tests require synthetic SQLite fixtures, explicitly allowlist those fixture paths in `.gitignore` instead of weakening local DB protection.

Example future allowlist if needed:

```gitignore
!tests/fixtures/**/*.sqlite3
!examples/fixtures/**/*.sqlite3
```

## FastAPI/Jinja App Variables

These variables are read by `web/config.py` with the `BRAND3_` prefix.

| Variable | Default / Example | Purpose |
| --- | --- | --- |
| `BRAND3_ENVIRONMENT` | `development` | App environment. In `production`, required team/cookie secrets are enforced. |
| `BRAND3_BASE_URL` | `http://127.0.0.1:8000` | Base URL used by local/deployed app surfaces |
| `BRAND3_DISABLE_LITESTREAM` | `false` | Set to `true` only for disposable staging apps that should start without SQLite replication |
| `BRAND3_TEAM_TOKEN` | local generated value | Token used by team unlock flow |
| `BRAND3_COOKIE_SECRET` | local generated value, 32+ chars | Secret for signed team cookies |
| `BRAND3_MAX_CONCURRENT_ANALYSES` | `2` (`1` on Fly) | Queue worker concurrency |
| `BRAND3_ANALYSIS_TIMEOUT_SECONDS` | `600` | Analysis timeout for queued web requests |
| `BRAND3_REQUEUE_IN_FLIGHT_ON_STARTUP` | `true` (`false` on Fly) | Requeue or interrupt running web/scanner jobs after process restart |
| `BRAND3_RATE_LIMIT_PER_IP` | `5` | Per-IP analyze request limit |
| `BRAND3_RATE_LIMIT_WINDOW_HOURS` | `24` | Rate-limit window |
| `BRAND3_RATE_LIMIT_BYPASS_IPS` | `127.0.0.1,::1` locally | Comma-separated IPs that bypass local rate limiting |

`BRAND3_TEAM_TOKEN` and `BRAND3_COOKIE_SECRET` are secrets. Do not commit real values.

## Brand3 Scoring Tuning Variables

These variables are used by scoring, collection, learning, or calibration paths.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAND3_CACHE_TTL_HOURS` | `24` | Collector/cache TTL |
| `BRAND3_NICHE_AUTO_APPLY_MIN_CONFIDENCE` | `0.65` | Niche auto-apply threshold |
| `BRAND3_PROMOTION_MAX_COMPOSITE_DROP` | `0` | Learning/promotion gate |
| `BRAND3_PROMOTION_MAX_DIMENSION_DROPS` | built-in per-dimension defaults | JSON object override for dimension drop gates |
| `BRAND3_SOCIAL_TIMEOUT_SECONDS` | `25` | Social collection timeout |
| `BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS` | `60` | Hard budget for viewport, structural captures, atlas generation, and provider fallback |
| `BRAND3_BLOCKED_DOMAINS` | empty | Optional comma-separated URL validation blocklist for the web app |

Changing these can affect scoring behavior or local run behavior. Do not change defaults in code or shared docs without explicit review.

## Visual Signature Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAND3_VISUAL_SIGNATURE_ROOT` | `examples/visual_signature` | Read-only artifact root used by the local Visual Signature platform routes |

Visual Signature guardrails:

- Visual Signature is evidence-only.
- Visual Signature must not mutate scoring tables or `dimension_scores`.
- Read-only platform routes must not execute provider calls.
- Generated Visual Signature artifacts should be classified before being committed.

## Output And Cache Notes

Local/generated paths:

- `output/**`: per-run JSON and rendered report outputs.
- `data/**`: local SQLite database and related files.
- `validation-*/`: local validation runs.
- `*.log`: local logs.
- `.pytest_cache/`, `.sentrux/`, `.cache/`: local caches.
- `playwright-report/`, `test-results/`, `blob-report/`: local browser/test outputs.

Policy:

- These paths are ignored by default.
- Do not commit local runtime output.
- If a generated artifact is needed for a PR, classify it as fixture, audit export, or local output, and include a regeneration command or rationale.

## Production / Shared Environments

For production-like environments:

- Set `BRAND3_ENVIRONMENT=production`.
- Provide `BRAND3_TEAM_TOKEN`.
- Provide `BRAND3_COOKIE_SECRET` with at least 32 characters.
- Store provider keys and app secrets in the deployment platform, not in Git.
- Keep `BRAND3_BASE_URL` aligned with the deployed URL.
