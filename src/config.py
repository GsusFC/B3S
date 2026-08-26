"""Configuration for Brand3 Scoring."""

import os
import json
from pathlib import Path

from src.api_key_pool import normalize_api_keys

# Try loading .env file
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

# API Keys. The singular variables remain the primary, backwards-compatible
# credentials. Plural variables append comma-separated keys for rotation.
FIRECRAWL_API_KEYS = normalize_api_keys(
    os.environ.get("FIRECRAWL_API_KEY", ""),
    os.environ.get("FIRECRAWL_API_KEYS", ""),
)
EXA_API_KEYS = normalize_api_keys(
    os.environ.get("EXA_API_KEY", ""),
    os.environ.get("EXA_API_KEYS", ""),
)
FIRECRAWL_API_KEY = FIRECRAWL_API_KEYS[0] if FIRECRAWL_API_KEYS else ""
EXA_API_KEY = EXA_API_KEYS[0] if EXA_API_KEYS else ""
SEARCHAPI_API_KEY = os.environ.get("SEARCHAPI_API_KEY", "")
HYPERBROWSER_API_KEY = os.environ.get("HYPERBROWSER_API_KEY", "")
HYPERBROWSER_API_URL = os.environ.get(
    "HYPERBROWSER_API_URL",
    "https://api.hyperbrowser.ai/api/web/fetch",
)
BRAND3_HYPERBROWSER_ENABLED = os.environ.get(
    "BRAND3_HYPERBROWSER_ENABLED",
    "false",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_GITHUB_PROOF_ENABLED = os.environ.get(
    "BRAND3_GITHUB_PROOF_ENABLED",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Single LLM provider. Defaults to Google AI Studio (OpenAI-compatible),
# but any OpenAI-compatible endpoint works by overriding BRAND3_LLM_BASE_URL
# + BRAND3_LLM_API_KEY. Accepts common Google/OpenRouter env names as fallback.
BRAND3_LLM_API_KEY = (
    os.environ.get("BRAND3_LLM_API_KEY")
    or os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("OPENROUTER_API_KEY", "")
)

# Scoring defaults
DEFAULT_NUM_EXA_RESULTS = 10
MAX_WEB_SCRAPE_CHARS = 50000
MAX_COMPETITORS = 5
MAX_COMPETITOR_SCRAPE_CHARS = 30000
BRAND3_DB_PATH = os.environ.get(
    "BRAND3_DB_PATH",
    str(Path(__file__).parent.parent / "data" / "brand3.sqlite3"),
)
B3S_DATABASE_URL = os.environ.get(
    "B3S_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://b3s:b3s@localhost:5433/b3s"),
)
BRAND3_CACHE_TTL_HOURS = int(os.environ.get("BRAND3_CACHE_TTL_HOURS", "24"))
# Owned-site captures must stay fresh: a client who changed their site and
# re-scans must not be served the previous run's capture. External perception
# sources (exa/social/competitors) change slowly and keep the global TTL.
BRAND3_CACHE_TTL_HOURS_BY_SOURCE = {
    "web": int(os.environ.get("BRAND3_CACHE_TTL_HOURS_WEB", "1")),
    "context": int(os.environ.get("BRAND3_CACHE_TTL_HOURS_CONTEXT", "1")),
    "hyperbrowser": int(os.environ.get("BRAND3_CACHE_TTL_HOURS_HYPERBROWSER", "1")),
    "github": int(os.environ.get("BRAND3_CACHE_TTL_HOURS_GITHUB", "24")),
}
BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED = os.environ.get(
    "BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED",
    "false",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_SEARCHAPI_FALLBACK_INTENTS = tuple(
    item.strip()
    for item in os.environ.get("BRAND3_SEARCHAPI_FALLBACK_INTENTS", "news").split(",")
    if item.strip()
)
BRAND3_VAULT_MIN_EXTERNAL_SOURCE_DOMAINS = max(
    1,
    int(os.environ.get("BRAND3_VAULT_MIN_EXTERNAL_SOURCE_DOMAINS", "3")),
)
BRAND3_NICHE_AUTO_APPLY_MIN_CONFIDENCE = float(
    os.environ.get("BRAND3_NICHE_AUTO_APPLY_MIN_CONFIDENCE", "0.65")
)
BRAND3_PROMOTION_MAX_COMPOSITE_DROP = float(
    os.environ.get("BRAND3_PROMOTION_MAX_COMPOSITE_DROP", "0")
)
BRAND3_PROMOTION_MAX_DIMENSION_DROPS = {
    "coherencia": 5.0,
    "presencia": 5.0,
    "percepcion": 5.0,
    "diferenciacion": 5.0,
    "vitalidad": 5.0,
}
BRAND3_MAGNETISM_RESEARCH_PACK_TLDR = os.environ.get(
    "BRAND3_MAGNETISM_RESEARCH_PACK_TLDR",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_BRAND_RESEARCH_GRAPH_PACK = os.environ.get(
    "BRAND3_BRAND_RESEARCH_GRAPH_PACK",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_BRAND_RESEARCH_VNEXT_PACK = os.environ.get(
    "BRAND3_BRAND_RESEARCH_VNEXT_PACK",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_CONTEXTDEV_VISUAL_ENRICHMENT = os.environ.get(
    "BRAND3_CONTEXTDEV_VISUAL_ENRICHMENT",
    "false",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED = os.environ.get(
    "BRAND3_VISUAL_SIGNATURE_SCAN_ENABLED",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_VISUAL_SIGNATURE_SKIP_MULTIMODAL = os.environ.get(
    "BRAND3_VISUAL_SIGNATURE_SKIP_MULTIMODAL",
    "false",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_MAGNETISM_EXTRACTOR_WEB_CHAR_LIMIT = int(
    os.environ.get("BRAND3_MAGNETISM_EXTRACTOR_WEB_CHAR_LIMIT", "16000")
)
_promotion_dimension_drops = os.environ.get("BRAND3_PROMOTION_MAX_DIMENSION_DROPS")
if _promotion_dimension_drops:
    try:
        BRAND3_PROMOTION_MAX_DIMENSION_DROPS.update(json.loads(_promotion_dimension_drops))
    except json.JSONDecodeError:
        pass

# LLM config (text + vision share the same provider by default)
DEFAULT_LLM_MODEL = "gemini-3.1-flash-lite"
DEFAULT_LLM_CHEAP_MODEL = "gemini-3.1-flash-lite"
DEFAULT_LLM_PREMIUM_MODEL = "gemini-3.1-flash-lite"
DEFAULT_VISION_MODEL = "gemini-2.5-flash"
LLM_BASE_URL = os.environ.get(
    "BRAND3_LLM_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai",
)
LLM_MODEL = os.environ.get("BRAND3_LLM_MODEL", DEFAULT_LLM_MODEL)
LLM_CHEAP_MODEL = os.environ.get("BRAND3_LLM_CHEAP_MODEL", DEFAULT_LLM_CHEAP_MODEL)
LLM_PREMIUM_MODEL = os.environ.get("BRAND3_LLM_PREMIUM_MODEL", DEFAULT_LLM_PREMIUM_MODEL)
VISION_MODEL = os.environ.get("BRAND3_VISION_MODEL", DEFAULT_VISION_MODEL)

# Visual Signature multimodal overrides. Default to the shared vision model and
# global LLM timeout; set the env vars to route to a different model or cap the
# per-call latency for Visual Signature specifically.
BRAND3_VISUAL_SIGNATURE_MODEL = os.environ.get(
    "BRAND3_VISUAL_SIGNATURE_MODEL",
    VISION_MODEL,
)
BRAND3_VISUAL_SIGNATURE_TIMEOUT_SECONDS = int(
    os.environ.get("BRAND3_VISUAL_SIGNATURE_TIMEOUT_SECONDS", "0")
)

# SV9 baldosas v3.1 model routing. Keep all text roles on the cheap Flash-Lite
# tier by default; specific roles remain overrideable for controlled bakeoffs.
SV9_FLOW_MODEL = os.environ.get("BRAND3_SV9_FLOW_MODEL", LLM_CHEAP_MODEL)
SV9_BASE_MODEL = os.environ.get("BRAND3_SV9_BASE_MODEL", LLM_CHEAP_MODEL)
SV9_REASONING_MODEL = os.environ.get("BRAND3_SV9_REASONING_MODEL", SV9_FLOW_MODEL)
SV9_EDITORIAL_MODEL = os.environ.get("BRAND3_SV9_EDITORIAL_MODEL", SV9_FLOW_MODEL)
SV9_ADJUDICATOR_MODEL = os.environ.get("BRAND3_SV9_ADJUDICATOR_MODEL", LLM_CHEAP_MODEL)
SV9_ADJUDICATOR_ESCALATION_MODEL = os.environ.get(
    "BRAND3_SV9_ADJUDICATOR_ESCALATION_MODEL",
    SV9_FLOW_MODEL,
)
SV9_FLOW_LABELING_MODEL = os.environ.get("BRAND3_SV9_FLOW_LABELING_MODEL", LLM_CHEAP_MODEL)
SV9_FLOW_GATE_AUTHORITY = os.environ.get("BRAND3_SV9_FLOW_GATE_AUTHORITY", "veto_only")
AUDIT_ANALYST_MODEL = os.environ.get("BRAND3_AUDIT_ANALYST_MODEL", LLM_CHEAP_MODEL)
CLIENT_TLDR_V2_MODEL = os.environ.get("BRAND3_CLIENT_TLDR_V2_MODEL", LLM_MODEL)
MAGNETISM_EXTRACTOR_MODEL = os.environ.get("BRAND3_MAGNETISM_EXTRACTOR_MODEL", LLM_PREMIUM_MODEL)
MAGNETISM_ANALYST_MODEL = os.environ.get("BRAND3_MAGNETISM_ANALYST_MODEL", LLM_PREMIUM_MODEL)
MAGNETISM_SYSTEM_READING_MODEL = os.environ.get("BRAND3_MAGNETISM_SYSTEM_READING_MODEL", LLM_PREMIUM_MODEL)
BRAND3_EVIDENCE_LLM_CLASSIFIER_ENABLED = os.environ.get(
    "BRAND3_EVIDENCE_LLM_CLASSIFIER_ENABLED",
    "false",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BRAND3_EVIDENCE_LLM_MODEL = os.environ.get("BRAND3_EVIDENCE_LLM_MODEL", LLM_CHEAP_MODEL)
BRAND3_EVIDENCE_LLM_BATCH_SIZE = int(os.environ.get("BRAND3_EVIDENCE_LLM_BATCH_SIZE", "4"))
BRAND3_EVIDENCE_LLM_TIMEOUT_SECONDS = int(os.environ.get("BRAND3_EVIDENCE_LLM_TIMEOUT_SECONDS", "20"))
BRAND3_EVIDENCE_LLM_MAX_ATTEMPTS = int(os.environ.get("BRAND3_EVIDENCE_LLM_MAX_ATTEMPTS", "2"))
BRAND3_EVIDENCE_LLM_NATIVE_STRUCTURED_OUTPUT = os.environ.get(
    "BRAND3_EVIDENCE_LLM_NATIVE_STRUCTURED_OUTPUT",
    "true",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Screenshot capture provider.
SCREENSHOT_PROVIDER = os.environ.get("SCREENSHOT_PROVIDER", "playwright").strip().lower() or "playwright"
BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS = max(
    1,
    int(os.environ.get("BRAND3_VISUAL_SCREENSHOT_TIMEOUT_SECONDS", "60")),
)

# Screenshots are evidence: they must outlive the OS temp dir cleanup.
BRAND3_SCREENSHOT_DIR = os.environ.get(
    "BRAND3_SCREENSHOT_DIR",
    str(Path(BRAND3_DB_PATH).parent / "screenshots"),
)


# The operational Vault planning/execution pipeline requires a second explicit
# deployment capability. Merely naming an environment "vault" must not change
# scanner persistence, interpretation, scoring, or report projection.
BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED = os.environ.get(
    "BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED",
    "false",
).strip().lower() == "true"
BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED = (
    os.environ.get("BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED", "false") == "true"
)

# Verified raw acquisition remains a Vault-only shadow path.  The web process
# receives only this public Unix-socket location; worker private-key and ingest
# DSN configuration deliberately do not exist in this module.
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED = os.environ.get(
    "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED",
    "false",
).strip().lower() == "true"
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH = os.environ.get(
    "BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH",
    "",
).strip()
# Owned-only analysis may continue scoring when Exa identity is absent, but the
# resulting snapshot can never satisfy the two-channel C7 readiness contract.
BRAND3_VAULT_VERIFIED_RAW_ALLOW_OWNED_ONLY_ANALYSIS = os.environ.get(
    "BRAND3_VAULT_VERIFIED_RAW_ALLOW_OWNED_ONLY_ANALYSIS",
    "false",
).strip().lower() == "true"
