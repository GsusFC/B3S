"""Pure tiered matcher that carries accepted SV9 support across a re-capture."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from src.sv9 import incremental_planner as ip
from src.sv9 import judgment_memory as jm


SUPPORT_CONTINUITY_RULE_VERSION = "evidence-vault-sv9-support-continuity-rule-v1"
CANONICAL_TIER = "canonical"
OWNED_PAGE_SIMILARITY_TIER = "owned_page_similarity"
SUPPORT_CONTINUITY_TIERS = frozenset({CANONICAL_TIER, OWNED_PAGE_SIMILARITY_TIER})
OWNED_COPY_SOURCE_CLASS = "owned_copy"
MIN_PAGE_SHINGLES = 20
# Jaccard >= 0.80 expressed as 10 * |A & B| >= 8 * |A | B| so signed payloads never carry floats.
_SIMILARITY_NUMERATOR, _SIMILARITY_DENOMINATOR = 8, 10
_SHINGLE_WORDS = 3
_WORD = re.compile(r"\w+")
_IDENTITY_KEYS = ("evidence_ref", "evidence_fingerprint", "evidence_id", "source_identity_id")


class EvidenceVaultSv9SupportContinuityError(ValueError):
    """The evidence rows cannot be matched safely; the caller must carry nothing."""


def _fail(message):
    raise EvidenceVaultSv9SupportContinuityError(message)


def meets_similarity_threshold(intersection, union):
    if type(intersection) is not int or type(union) is not int or intersection < 0 or intersection > union:
        return False
    return _SIMILARITY_DENOMINATOR * intersection >= _SIMILARITY_NUMERATOR * union


def build_support_continuity_carry_map(*, prior_judgments, historical, current):
    """Return one carried entry per accepted support pair that survives only under another pair.

    Tier order is exact, canonical identity, owned-page similarity; the first match
    wins and anything ambiguous carries nothing so the pair stays coverage loss.
    """
    historical_rows = [_row(raw, "historical") for raw in _rows(historical, "historical")]
    current_rows = [_row(raw, "current") for raw in _rows(current, "current")]
    current_pairs = set()
    current_by_identity, current_by_source = {}, {}
    for row in current_rows:
        pair = row["evidence_ref"], row["evidence_fingerprint"]
        if pair in current_pairs:
            _fail("current evidence contains duplicate pairs")
        current_pairs.add(pair)
        if row["evidence_id"] is not None:
            current_by_identity.setdefault((row["evidence_id"], row["source_identity_id"]), []).append(row)
        if row["source_identity_id"] is not None:
            current_by_source.setdefault(row["source_identity_id"], []).append(row)
    historical_by_pair, historical_by_source = {}, {}
    for row in historical_rows:
        historical_by_pair.setdefault((row["evidence_ref"], row["evidence_fingerprint"]), []).append(row)
        if row["source_identity_id"] is not None:
            historical_by_source.setdefault(row["source_identity_id"], []).append(row)
    pages = {}
    carried = []
    for judgment in _prior(prior_judgments):
        for item in judgment["supporting_evidence"]:
            pair = item["evidence_ref"], item["evidence_fingerprint"]
            if pair in current_pairs:
                continue
            rows = historical_by_pair.get(pair, [])
            if len(rows) != 1:
                continue
            match = _canonical(rows[0], current_by_identity) or _owned_page(
                rows[0], historical_by_source, current_by_source, pages
            )
            if match is not None:
                carried.append({"tile_id": judgment["tile_id"], "source": dict(item), **match})
    return sorted(
        carried,
        key=lambda row: (
            ip._ORDER[row["tile_id"]],
            row["source"]["evidence_ref"],
            row["source"]["evidence_fingerprint"],
        ),
    )


def _canonical(row, current_by_identity):
    if row["evidence_id"] is None:
        return None
    matches = current_by_identity.get((row["evidence_id"], row["source_identity_id"]), [])
    if len(matches) != 1:
        return None
    return {"tier": CANONICAL_TIER, "targets": [_pair(matches[0])]}


def _owned_page(row, historical_by_source, current_by_source, pages):
    source = row["source_identity_id"]
    if row["source_class"] != OWNED_COPY_SOURCE_CLASS or source is None:
        return None
    targets = current_by_source.get(source, [])
    if not targets:
        return None
    if source not in pages:
        pages[source] = _page_similarity(historical_by_source[source], targets)
    similarity = pages[source]
    if similarity is None:
        return None
    return {
        "tier": OWNED_PAGE_SIMILARITY_TIER,
        "targets": sorted(
            (_pair(target) for target in targets), key=lambda pair: (pair["evidence_ref"], pair["evidence_fingerprint"])
        ),
        "similarity": similarity,
    }


def _page_similarity(historical_rows, current_rows):
    source_shingles = _shingles(historical_rows)
    target_shingles = _shingles(current_rows)
    if len(source_shingles) < MIN_PAGE_SHINGLES or len(target_shingles) < MIN_PAGE_SHINGLES:
        return None
    intersection, union = len(source_shingles & target_shingles), len(source_shingles | target_shingles)
    if not meets_similarity_threshold(intersection, union):
        return None
    return {
        "source_shingle_digest": _digest(source_shingles),
        "target_shingle_digest": _digest(target_shingles),
        "intersection": intersection,
        "union": union,
    }


def _shingles(rows):
    result = set()
    for row in rows:
        if type(row["content"]) is not str:
            continue
        words = _WORD.findall(unicodedata.normalize("NFKC", row["content"]).casefold())
        result.update(
            " ".join(words[index : index + _SHINGLE_WORDS]) for index in range(len(words) - _SHINGLE_WORDS + 1)
        )
    return result


def _digest(shingles):
    return hashlib.sha256("\n".join(sorted(shingles)).encode("utf-8")).hexdigest()


def _prior(values):
    if type(values) is not list:
        _fail("prior judgments must be a JSON array")
    try:
        rows = [jm.validate_tile_judgment(value) for value in values]
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    if len({row["tile_id"] for row in rows}) != len(rows):
        _fail("prior judgments contain duplicate tiles")
    return sorted(rows, key=lambda row: ip._ORDER[row["tile_id"]])


def _rows(values, label):
    if type(values) is not list:
        _fail(f"{label} evidence must be a JSON array")
    return values


def _row(raw, label):
    if type(raw) is not dict or not set(_IDENTITY_KEYS) <= set(raw) or "content" not in raw:
        _fail(f"{label} evidence row fields do not match schema")
    try:
        row = {
            "evidence_ref": jm._text(raw["evidence_ref"]),
            "evidence_fingerprint": jm._sha(raw["evidence_fingerprint"]),
            "evidence_id": None if raw["evidence_id"] is None else jm._sha(raw["evidence_id"]),
            "source_identity_id": None if raw["source_identity_id"] is None else jm._sha(raw["source_identity_id"]),
        }
    except jm.JudgmentMemoryContractError as exc:
        _fail(str(exc))
    if (row["evidence_id"] is None) != (row["source_identity_id"] is None):
        _fail(f"{label} evidence row has a one-sided canonical identity")
    source_class = raw.get("source_class")
    if source_class is not None and type(source_class) is not str:
        _fail(f"{label} evidence row source class is invalid")
    return {**row, "source_class": source_class, "content": raw["content"]}


def _pair(row):
    return {"evidence_ref": row["evidence_ref"], "evidence_fingerprint": row["evidence_fingerprint"]}
