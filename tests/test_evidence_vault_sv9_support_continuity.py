import hashlib
import re
import unicodedata

import pytest

from src.services import evidence_vault_sv9_support_continuity as continuity
from src.sv9 import incremental_planner as ip
from tests.test_sv9_judgment_memory import _hash, _judgment


_WORK = _hash(0x5001)
_HOME = _hash(0x5002)
_EXA = _hash(0x5003)


def _words(count, prefix="w"):
    return " ".join(f"{prefix}{index}" for index in range(count))


def _fingerprint(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _row(ref, content, *, source, source_class="owned_copy", evidence_id=None, fingerprint=None):
    return {
        "evidence_ref": ref,
        "evidence_fingerprint": fingerprint or _fingerprint(content),
        "evidence_id": evidence_id or hashlib.sha256(f"{source_class}|{source}|{content}".encode()).hexdigest(),
        "source_identity_id": source,
        "source_class": source_class,
        "content": content,
    }


def _pair(row):
    return {key: row[key] for key in ("evidence_ref", "evidence_fingerprint")}


def _support(tile, *rows):
    return _judgment(tile_id=tile, component_key=dict(ip._REGISTRY)[tile], evidence=[_pair(row) for row in rows])


def _shingles(*texts):
    result = set()
    for text in texts:
        words = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
        result |= {" ".join(words[index : index + 3]) for index in range(len(words) - 2)}
    return result


def _digest(shingles):
    return hashlib.sha256("\n".join(sorted(shingles)).encode("utf-8")).hexdigest()


def _carry(prior, historical, current):
    return continuity.build_support_continuity_carry_map(prior_judgments=prior, historical=historical, current=current)


def test_exact_pairs_are_never_carried():
    row = _row("raw_inputs.0.chunk.0", _words(30), source=_WORK)
    assert _carry([_support("M1", row)], [row], [dict(row, evidence_ref="raw_inputs.0.chunk.0")]) == []


def test_canonical_tier_carries_a_moved_ref_for_owned_and_external_records():
    work = _row("raw_inputs.3.subpage.2.chunk.4", _words(30), source=_WORK)
    mention = _row(
        "raw_inputs.5.exa.mentions.7", "Primary was named a top studio.", source=_EXA, source_class="external_proof"
    )
    moved_work = dict(work, evidence_ref="raw_inputs.4.subpage.1.chunk.2")
    moved_mention = dict(mention, evidence_ref="raw_inputs.6.exa.mentions.1")

    carried = _carry([_support("M1", work), _support("A1", mention)], [work, mention], [moved_mention, moved_work])

    assert carried == [
        {"tile_id": "M1", "source": _pair(work), "tier": "canonical", "targets": [_pair(moved_work)]},
        {"tile_id": "A1", "source": _pair(mention), "tier": "canonical", "targets": [_pair(moved_mention)]},
    ]


def test_canonical_tier_requires_exactly_one_current_identity_match():
    mention = _row(
        "raw_inputs.5.exa.mentions.7", "Primary was named a top studio.", source=_EXA, source_class="external_proof"
    )
    duplicates = [dict(mention, evidence_ref=f"raw_inputs.6.exa.mentions.{index}") for index in (1, 2)]
    assert _carry([_support("M1", mention)], [mention], duplicates) == []


def test_owned_page_similarity_carries_a_rechunked_page_to_every_current_record():
    page = _words(120)
    words = page.split()
    historical = [
        _row("raw_inputs.1.subpage.0.chunk.0", " ".join(words[:40]), source=_WORK),
        _row("raw_inputs.1.subpage.0.chunk.1", " ".join(words[40:80]), source=_WORK),
        _row("raw_inputs.1.subpage.0.chunk.2", " ".join(words[80:]), source=_WORK),
    ]
    current = [
        _row("raw_inputs.2.subpage.0.chunk.0", " ".join(words[:70]), source=_WORK),
        _row("raw_inputs.2.subpage.0.chunk.1", " ".join(words[70:]) + " new closing line", source=_WORK),
    ]
    prior = [_support("M1", historical[1]), _support("A1", historical[0], historical[1])]

    carried = _carry(prior, historical, current)

    source_shingles = _shingles(*(row["content"] for row in historical))
    target_shingles = _shingles(*(row["content"] for row in current))
    similarity = {
        "source_shingle_digest": _digest(source_shingles),
        "target_shingle_digest": _digest(target_shingles),
        "intersection": len(source_shingles & target_shingles),
        "union": len(source_shingles | target_shingles),
    }
    assert 10 * similarity["intersection"] >= 8 * similarity["union"]
    targets = sorted(
        (_pair(row) for row in current), key=lambda row: (row["evidence_ref"], row["evidence_fingerprint"])
    )
    assert carried == [
        {
            "tile_id": "M1",
            "source": _pair(historical[1]),
            "tier": "owned_page_similarity",
            "targets": targets,
            "similarity": similarity,
        },
        {
            "tile_id": "A1",
            "source": _pair(historical[0]),
            "tier": "owned_page_similarity",
            "targets": targets,
            "similarity": similarity,
        },
        {
            "tile_id": "A1",
            "source": _pair(historical[1]),
            "tier": "owned_page_similarity",
            "targets": targets,
            "similarity": similarity,
        },
    ]
    assert all(type(value) is int for value in (similarity["intersection"], similarity["union"]))


def test_truncated_page_capture_stays_coverage_loss():
    words = _words(339).split()
    historical = [_row("raw_inputs.0.chunk.0", " ".join(words), source=_HOME)]
    current = [_row("raw_inputs.0.chunk.0", " ".join(words[:67]), source=_HOME)]
    assert len(_shingles(historical[0]["content"])) == 337 and len(_shingles(current[0]["content"])) == 65
    assert _carry([_support("M1", historical[0])], historical, current) == []


def test_externals_never_use_owned_page_similarity():
    mention = _row("raw_inputs.5.exa.mentions.7", _words(60), source=_EXA, source_class="external_proof")
    rewritten = _row("raw_inputs.6.exa.mentions.7", _words(60) + " w60", source=_EXA, source_class="external_proof")
    assert _carry([_support("M1", mention)], [mention], [rewritten]) == []
    owned = dict(mention, source_class="owned_copy")
    assert (
        _carry([_support("M1", owned)], [owned], [dict(rewritten, source_class="owned_copy")])[0]["tier"]
        == "owned_page_similarity"
    )


@pytest.mark.parametrize("historical_count", [0, 2])
def test_ambiguous_or_missing_historical_rows_fail_closed(historical_count):
    work = _row("raw_inputs.3.chunk.4", _words(30), source=_WORK)
    moved = dict(work, evidence_ref="raw_inputs.4.chunk.2")
    historical = [dict(work, evidence_id=_hash(index)) for index in range(historical_count)]
    assert _carry([_support("M1", work)], historical, [moved]) == []


def test_both_sides_need_twenty_shingles_and_string_content():
    short = _row("raw_inputs.0.chunk.0", _words(21), source=_WORK)
    assert len(_shingles(short["content"])) == 19
    assert (
        _carry(
            [_support("M1", short)], [short], [dict(short, evidence_ref="raw_inputs.1.chunk.0", evidence_id=_hash(7))]
        )
        == []
    )
    structured = _row("raw_inputs.0.chunk.0", _words(30), source=_WORK)
    current = dict(structured, evidence_ref="raw_inputs.1.chunk.0", evidence_id=_hash(7), content={"text": _words(30)})
    assert _carry([_support("M1", structured)], [structured], [current]) == []


@pytest.mark.parametrize(("source_words", "target_words", "carried"), [(52, 42, True), (51, 41, False), (27, 22, True)])
def test_integer_threshold_edges_at_exactly_eighty_percent(source_words, target_words, carried):
    source = _row("raw_inputs.0.chunk.0", _words(source_words), source=_WORK)
    target = _row("raw_inputs.1.chunk.0", _words(target_words), source=_WORK)
    result = _carry([_support("M1", source)], [source], [target])
    if not carried:
        assert result == []
        return
    similarity = result[0]["similarity"]
    assert (similarity["intersection"], similarity["union"]) == (target_words - 2, source_words - 2)
    assert 10 * similarity["intersection"] == 8 * similarity["union"]


@pytest.mark.parametrize(
    ("intersection", "union", "expected"), [(20, 25, True), (19, 24, False), (40, 50, True), (39, 49, False)]
)
def test_similarity_threshold_uses_integers_only(intersection, union, expected):
    assert continuity.meets_similarity_threshold(intersection, union) is expected


def test_shingles_normalize_unicode_and_case_before_hashing():
    fancy = _row("raw_inputs.0.chunk.0", "Ｗe Build BRANDS for Ambitious Teams " + _words(25), source=_WORK)
    plain = dict(
        fancy, evidence_ref="raw_inputs.1.chunk.0", content=fancy["content"].lower().replace("ｗ", "w") + " extra"
    )
    plain["evidence_fingerprint"], plain["evidence_id"] = _fingerprint(plain["content"]), _hash(7)
    [entry] = _carry([_support("M1", fancy)], [fancy], [plain])
    assert entry["similarity"]["source_shingle_digest"] == _digest(_shingles(fancy["content"]))
    assert entry["similarity"]["target_shingle_digest"] == _digest(_shingles(plain["content"]))
    assert entry["similarity"]["intersection"] == entry["similarity"]["union"] - 1


def test_output_does_not_depend_on_input_order():
    words = _words(90).split()
    historical = [
        _row("raw_inputs.1.chunk.0", " ".join(words[:45]), source=_WORK),
        _row("raw_inputs.1.chunk.1", " ".join(words[45:]), source=_WORK),
        _row(
            "raw_inputs.5.exa.mentions.7", "Primary was named a top studio.", source=_EXA, source_class="external_proof"
        ),
    ]
    current = [
        _row("raw_inputs.2.chunk.0", " ".join(words[:30]), source=_WORK),
        _row("raw_inputs.2.chunk.1", " ".join(words[30:]), source=_WORK),
        dict(historical[2], evidence_ref="raw_inputs.6.exa.mentions.1"),
    ]
    prior = [_support("A1", historical[1]), _support("M1", historical[0], historical[2])]

    forward = _carry(prior, historical, current)

    assert forward == _carry(list(reversed(prior)), list(reversed(historical)), list(reversed(current)))
    assert [(row["tile_id"], row["source"]["evidence_ref"]) for row in forward] == [
        ("M1", "raw_inputs.1.chunk.0"),
        ("M1", "raw_inputs.5.exa.mentions.7"),
        ("A1", "raw_inputs.1.chunk.1"),
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda historical, current: current.append(dict(current[0])),
        lambda historical, current: historical[0].__setitem__("evidence_fingerprint", "not-a-digest"),
        lambda historical, current: current[0].__setitem__("evidence_id", "not-a-digest"),
        lambda historical, current: current.__setitem__(0, "not-a-row"),
    ],
)
def test_malformed_rows_fail_closed(mutate):
    work = _row("raw_inputs.3.chunk.4", _words(30), source=_WORK)
    prior = [_support("M1", work)]
    historical, current = [dict(work)], [dict(work, evidence_ref="raw_inputs.4.chunk.2")]
    mutate(historical, current)
    with pytest.raises(continuity.EvidenceVaultSv9SupportContinuityError):
        _carry(prior, historical, current)


def test_rule_version_and_tiers_are_published():
    assert continuity.SUPPORT_CONTINUITY_RULE_VERSION == "evidence-vault-sv9-support-continuity-rule-v1"
    assert continuity.SUPPORT_CONTINUITY_TIERS == frozenset({"canonical", "owned_page_similarity"})
    assert continuity.MIN_PAGE_SHINGLES == 20
