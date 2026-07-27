from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_identity import (
    canonical_evidence_id,
    canonical_evidence_records,
    canonical_evidence_ref,
    canonical_evidence_set_digest,
    canonicalize_evidence_refs,
    original_ref_for_canonical,
)


def _record(
    *,
    ref: str,
    content: str = "Acme turns every training ride into a reward.",
    url: str = "https://acme.example/proof?utm_source=test",
    metadata: dict | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        ref=ref,
        source="exa",
        evidence_type="external_proof.external_mentions",
        content=content,
        url=url,
        confidence="low",
        metadata={"source_class": "external_proof", **(metadata or {})},
    )


def test_evidence_identity_ignores_positional_ref_tracking_and_volatile_metadata() -> None:
    first = _record(
        ref="raw_inputs.2.exa.mentions.3",
        metadata={"score": 0.2, "published_date": "2026-07-25", "result_group": "mentions"},
    )
    second = _record(
        ref="raw_inputs.7.exa.news.0",
        url="https://ACME.example/proof?utm_source=other",
        metadata={"score": 0.9, "published_date": "2026-07-26", "result_group": "news"},
    )

    assert canonical_evidence_id(first) == canonical_evidence_id(second)
    assert canonical_evidence_ref(first) == canonical_evidence_ref(second)


def test_evidence_identity_changes_when_material_content_changes() -> None:
    first = _record(ref="raw_inputs.0", content="Acme rewards every training ride.")
    second = _record(ref="raw_inputs.1", content="Acme rewards every completed purchase.")

    assert canonical_evidence_id(first) != canonical_evidence_id(second)


def test_canonical_records_deduplicate_and_restore_current_provenance() -> None:
    duplicate_late = _record(ref="raw_inputs.9")
    duplicate_early = _record(ref="raw_inputs.1")
    distinct = _record(ref="raw_inputs.2", content="Independent proof of Acme adoption.")
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[duplicate_late, distinct, duplicate_early],
    )

    records = canonical_evidence_records(pack.evidence)
    alias = canonical_evidence_ref(duplicate_late)

    assert len(records) == 2
    assert canonicalize_evidence_refs(["raw_inputs.9"], pack) == [alias]
    assert original_ref_for_canonical(alias, pack) == "raw_inputs.1"


def test_evidence_set_digest_is_order_and_ref_invariant() -> None:
    first = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            _record(ref="raw_inputs.0"),
            _record(ref="raw_inputs.1", content="Independent adoption proof."),
        ],
    )
    second = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            _record(ref="raw_inputs.8", content="Independent adoption proof."),
            _record(ref="raw_inputs.9"),
        ],
    )

    assert canonical_evidence_set_digest(first) == canonical_evidence_set_digest(second)
