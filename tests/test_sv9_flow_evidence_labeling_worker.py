import json
import logging

import pytest

from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_labeling_worker import (
    EVIDENCE_LABELING_VERSION,
    _LABEL_SCHEMA,
    label_evidence_pack,
)

_ROOT_ARRAY_SCHEMA_ERROR = "$: expected object"


def _f820_shaped_root_array(rows):
    """Build a root array shaped like the scan f820 drift: complete label items without the object envelope.

    The real rejected payload was never persisted, so the items are synthetic and derived from the
    label schema's required fields.
    """

    item_fields = _LABEL_SCHEMA["properties"]["labels"]["items"]["required"]
    items = [
        {
            "ref": row["ref"],
            "relevant_blocks": ["mission"],
            "stance": "supports",
            "identity_match": "domain",
            "specificity": "explicit",
        }
        for row in rows
    ]
    assert all(sorted(item) == sorted(item_fields) for item in items)
    return items


class StubLabelingLLM:
    api_key = "test"

    def __init__(self, labels):
        self.labels = labels
        self.calls = []

    def _call_json(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        rows = json.loads(user)["records"]
        return {
            "labels": [
                {**label, "ref": rows[index]["ref"]}
                for index, label in enumerate(self.labels)
                if index < len(rows)
            ]
        }


class ArtifactCachingLabelingLLM:
    api_key = "test"
    model = "label-cache-test"

    def __init__(self):
        self.cache = {}
        self.provider_calls = []

    def _cache_get(self, key, response_type):
        assert response_type == "json"
        return self.cache.get(key)

    def _cache_save(self, key, response_type, value):
        assert response_type == "json"
        self.cache[key] = value

    def _call_json(self, system, user, **kwargs):
        payload = json.loads(user)
        self.provider_calls.append(payload)
        return {
            "labels": [
                {
                    "ref": row["ref"],
                    "relevant_blocks": ["mission"],
                    "stance": "supports",
                    "identity_match": "domain",
                    "specificity": "explicit",
                }
                for row in payload["records"]
            ]
        }


class FailingArtifactCachingLabelingLLM(ArtifactCachingLabelingLLM):
    def _call_json(self, system, user, **kwargs):
        self.provider_calls.append(json.loads(user))
        self.last_failure_reason = "transport_error"
        return {}


class SchemaRejectedLabelingLLM(ArtifactCachingLabelingLLM):
    """Labeler whose client rejected the parsed provider answer against the label schema.

    Mirrors the client contract after a schema rejection: an empty result, the failure reason
    and its recorded detail, and the parsed value kept on `last_rejected_payload`.
    """

    def __init__(self, rejected_payload, *, schema_error=_ROOT_ARRAY_SCHEMA_ERROR):
        super().__init__()
        self.rejected_payload = rejected_payload
        self.schema_error = schema_error
        self.last_failure_reason = None
        self.call_failures = []
        self.last_rejected_payload = None

    def _call_json(self, system, user, **kwargs):
        payload = json.loads(user)
        self.provider_calls.append(payload)
        self.last_failure_reason = "schema_validation_error"
        self.call_failures.append({"reason": "schema_validation_error", "error": self.schema_error})
        self.last_rejected_payload = self.rejected_payload(payload["records"])
        return {}


class RaisingLabelingLLM:
    api_key = "test"

    def __init__(self, exc: Exception):
        self.exc = exc

    def _call_json(self, system, user, **kwargs):
        raise self.exc


class RaisingCacheLabelingLLM(ArtifactCachingLabelingLLM):
    def _cache_get(self, key, response_type):
        raise OSError("artifact cache unavailable")


def test_label_evidence_pack_enriches_metadata_without_changing_pack_shape() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="We turn every training ride into a reward loop.",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            )
        ],
    )
    llm = StubLabelingLLM(
        [
            {
                "ref": "raw_inputs.0",
                "relevant_blocks": ["mission"],
                "stance": "supports",
                "identity_match": "domain",
                "specificity": "implied",
            }
        ]
    )
    debug = label_evidence_pack(pack, llm=llm)

    assert debug["status"] == "labeled"
    assert debug["records_labeled"] == 1
    assert pack.schema_version == "brand-evidence-pack-v1"
    assert pack.evidence[0].metadata["relevant_blocks"] == ["mission"]
    assert pack.evidence[0].metadata["stance"] == "supports"
    assert pack.evidence[0].metadata["identity_match_llm"] == "domain"
    assert pack.evidence[0].metadata["specificity"] == "implied"
    assert pack.evidence[0].metadata["semantic_labeling_version"] == EVIDENCE_LABELING_VERSION
    prompt = json.loads(llm.calls[0]["user"])
    assert prompt["owned_identity_context"] == [
        {
            "url": "",
            "content": "We turn every training ride into a reward loop.",
        }
    ]


def test_long_single_line_record_exposes_tail_to_labeler() -> None:
    tail = "Our mission is to help every team ship a reliable product."
    record = EvidenceRecord(
        "raw_inputs.0", "web", "raw_input",
        "Early homepage copy. " + ("filler " * 8_000) + tail,
        metadata={"source_class": "owned_copy", "identity_match": "domain"},
    )
    pack = BrandEvidencePack("Acme", "https://acme.example", [record])
    llm = ArtifactCachingLabelingLLM()

    label_evidence_pack(pack, llm=llm)

    assert record.metadata["relevant_blocks"] == ["mission"]



def test_label_evidence_pack_skips_when_llm_unavailable() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Mission copy.",
            )
        ],
    )

    debug = label_evidence_pack(pack, llm=None)

    assert debug["status"] == "skipped"
    assert debug["reason"] == "missing_llm_api_key"
    assert pack.evidence[0].metadata == {}


def test_label_evidence_pack_logs_identity_divergence_without_overwriting_deterministic_label() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="exa",
                evidence_type="external_proof.profile",
                content="Different company with the same name.",
                metadata={"source_class": "external_proof", "identity_match": "brand_name"},
            )
        ],
    )

    debug = label_evidence_pack(
        pack,
        llm=StubLabelingLLM(
            [
                {
                    "ref": "raw_inputs.0",
                    "relevant_blocks": [],
                    "stance": "neutral",
                    "identity_match": "none",
                    "specificity": "incidental",
                }
            ]
        ),
    )

    assert debug["records_labeled"] == 1
    assert pack.evidence[0].metadata["identity_match"] == "brand_name"
    assert pack.evidence[0].metadata["identity_match_llm"] == "none"
    assert debug["identity_divergences"] == [
        {
            "ref": "raw_inputs.0",
            "deterministic_identity_match": "brand_name",
            "llm_identity_match": "none",
        }
    ]


def test_label_cache_reuses_equivalent_records_across_ref_and_order_changes() -> None:
    def record(ref: str, content: str, *, score: float) -> EvidenceRecord:
        return EvidenceRecord(
            ref=ref,
            source="exa",
            evidence_type="external_proof.external_mentions",
            content=content,
            url=f"https://proof.example/{content[-1]}",
            confidence="high" if score > 0.5 else "low",
            metadata={
                "source_class": "external_proof",
                "identity_match": "domain",
                "score": score,
                "result_group": "mentions",
            },
        )

    first = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            record("raw_inputs.2.exa.mentions.0", "Mission proof A", score=0.1),
            record("raw_inputs.2.exa.mentions.1", "Mission proof B", score=0.9),
        ],
    )
    second = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            record("raw_inputs.8.exa.news.4", "Mission proof B", score=0.2),
            record("raw_inputs.8.exa.news.7", "Mission proof A", score=0.8),
        ],
    )
    llm = ArtifactCachingLabelingLLM()

    first_debug = label_evidence_pack(first, llm=llm)
    second_debug = label_evidence_pack(second, llm=llm)

    assert first_debug["provider_records"] == 2
    assert second_debug["artifact_cache_hits"] == 2
    assert second_debug["provider_records"] == 0
    assert len(llm.provider_calls) == 2
    assert all(record.metadata["relevant_blocks"] == ["mission"] for record in second.evidence)


def test_label_cache_invalidates_records_when_owned_identity_context_changes() -> None:
    llm = ArtifactCachingLabelingLLM()
    shared = EvidenceRecord(
        ref="raw_inputs.0",
        source="web",
        evidence_type="raw_input",
        content="Acme helps teams close faster.",
        url="https://acme.example",
        metadata={"source_class": "owned_copy", "identity_match": "domain"},
    )
    first = BrandEvidencePack("Acme", "https://acme.example", [shared])
    label_evidence_pack(first, llm=llm)

    second = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            EvidenceRecord(
                ref="raw_inputs.9",
                source="web",
                evidence_type="raw_input",
                content=shared.content,
                url=shared.url,
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            ),
            EvidenceRecord(
                ref="raw_inputs.10",
                source="web",
                evidence_type="raw_input",
                content="Acme publishes a new independent adoption proof.",
                url="https://acme.example/proof",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            ),
        ],
    )

    debug = label_evidence_pack(second, llm=llm)

    assert debug["artifact_cache_hits"] == 0
    assert debug["provider_records"] == 2
    assert len(llm.provider_calls) == 3
    assert len(llm.provider_calls[-1]["records"]) == 1


def test_label_cache_does_not_store_neutral_artifacts_after_provider_failure() -> None:
    llm = FailingArtifactCachingLabelingLLM()
    pack = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Acme helps teams close faster.",
                url="https://acme.example",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            )
        ],
    )

    debug = label_evidence_pack(pack, llm=llm)

    assert debug["status"] == "failed"
    assert debug["reason"] == "evidence_labeling_provider_failed:transport_error"
    assert llm.cache == {}
    assert "relevant_blocks" not in pack.evidence[0].metadata


def _owned_copy_pack() -> BrandEvidencePack:
    return BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Acme helps teams close faster.",
                url="https://acme.example",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            )
        ],
    )


def test_label_evidence_pack_repairs_f820_shaped_root_array_envelope(caplog) -> None:
    llm = SchemaRejectedLabelingLLM(_f820_shaped_root_array)
    pack = _owned_copy_pack()

    debug = label_evidence_pack(pack, llm=llm)

    assert debug["status"] == "labeled"
    assert debug["reason"] == ""
    assert debug["records_labeled"] == 1
    assert debug["artifact_cache_hits"] == 0
    assert debug["artifact_cache_misses"] == 1
    assert debug["provider_records"] == 1
    assert debug["provider_call_count"] == 1
    assert len(llm.provider_calls) == 1
    record = pack.evidence[0]
    assert record.metadata["relevant_blocks"] == ["mission"]
    assert record.metadata["stance"] == "supports"
    assert record.metadata["identity_match_llm"] == "domain"
    assert record.metadata["specificity"] == "explicit"
    assert record.metadata["semantic_labeling_version"] == EVIDENCE_LABELING_VERSION
    assert [value["label"]["relevant_blocks"] for value in llm.cache.values()] == [["mission"]]
    assert llm.last_failure_reason is None
    warnings = [entry for entry in caplog.records if entry.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "root-array envelope" in warnings[0].getMessage()
    assert "items=1" in warnings[0].getMessage()
    assert "Acme helps teams close faster." not in caplog.text


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param(
            lambda item: {key: value for key, value in item.items() if key != "specificity"},
            id="missing_required_field",
        ),
        pytest.param(lambda item: {**item, "confidence": "high"}, id="unexpected_field"),
        pytest.param(lambda item: {**item, "relevant_blocks": "mission"}, id="wrong_field_type"),
        pytest.param(lambda item: [item["ref"]], id="non_object_item"),
    ],
)
def test_label_evidence_pack_keeps_failing_closed_for_invalid_root_array_items(drift, caplog) -> None:
    llm = SchemaRejectedLabelingLLM(lambda rows: [drift(item) for item in _f820_shaped_root_array(rows)])
    pack = _owned_copy_pack()

    debug = label_evidence_pack(pack, llm=llm)

    assert debug["status"] == "failed"
    assert debug["reason"] == (
        "evidence_labeling_provider_failed:schema_validation_error:"
        "schema_path=$;schema_category=expected_type"
    )
    assert debug["records_labeled"] == 0
    assert debug["provider_call_count"] == 0
    assert len(llm.provider_calls) == 1
    assert llm.cache == {}
    assert llm.last_failure_reason == "schema_validation_error"
    assert "relevant_blocks" not in pack.evidence[0].metadata
    assert "root-array envelope" not in caplog.text


def test_label_evidence_pack_does_not_repair_non_list_rejected_payload() -> None:
    llm = SchemaRejectedLabelingLLM(
        lambda rows: {"label": _f820_shaped_root_array(rows)[0]},
        schema_error="$: missing required field(s): labels",
    )
    pack = _owned_copy_pack()

    debug = label_evidence_pack(pack, llm=llm)

    assert debug["status"] == "failed"
    assert debug["reason"] == (
        "evidence_labeling_provider_failed:schema_validation_error:"
        "schema_path=$;schema_category=missing_required"
    )
    assert debug["records_labeled"] == 0
    assert llm.cache == {}
    assert llm.last_failure_reason == "schema_validation_error"
    assert "relevant_blocks" not in pack.evidence[0].metadata


def test_label_evidence_pack_fails_closed_when_provider_omits_a_record() -> None:
    pack = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Acme helps teams close faster.",
                url="https://acme.example",
                metadata={
                    "source_class": "owned_copy",
                    "identity_match": "domain",
                },
            )
        ],
    )

    debug = label_evidence_pack(pack, llm=StubLabelingLLM([]))

    assert debug["status"] == "failed"
    assert debug["reason"].startswith(
        "evidence_labeling_provider_incomplete:"
    )
    assert debug["records_labeled"] == 0
    assert "semantic_labeling_version" not in pack.evidence[0].metadata


def test_label_evidence_pack_maps_unexpected_labeler_exception_to_worker_error(caplog) -> None:
    pack = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Acme helps teams close faster.",
                url="https://acme.example",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            )
        ],
    )

    debug = label_evidence_pack(pack, llm=RaisingLabelingLLM(RuntimeError("boom")))

    assert debug["status"] == "failed"
    assert debug["reason"] == "evidence_labeling_worker_error:RuntimeError"
    assert "boom" not in debug["reason"]
    assert debug["records_labeled"] == 0
    assert debug["artifact_cache_hits"] == 0
    assert debug["artifact_cache_misses"] == 0
    assert debug["provider_records"] == 0
    assert debug["provider_call_count"] == 0
    assert "semantic_labeling_version" not in pack.evidence[0].metadata
    assert "RuntimeError" in caplog.text
    assert "boom" in caplog.text


def test_label_evidence_pack_maps_artifact_cache_error_to_worker_error() -> None:
    llm = RaisingCacheLabelingLLM()
    pack = BrandEvidencePack(
        "Acme",
        "https://acme.example",
        [
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="Acme helps teams close faster.",
                url="https://acme.example",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            )
        ],
    )

    debug = label_evidence_pack(pack, llm=llm)

    assert debug["status"] == "failed"
    assert debug["reason"] == "evidence_labeling_worker_error:OSError"
    assert "artifact cache unavailable" not in debug["reason"]
    assert debug["records_labeled"] == 0
    assert debug["provider_call_count"] == 0
    assert llm.provider_calls == []
    assert llm.cache == {}
    assert "relevant_blocks" not in pack.evidence[0].metadata


def test_label_evidence_pack_filters_workset_but_keeps_full_identity_context() -> None:
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="selected", source="web", evidence_type="copy",
                content="Selected mission evidence.",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            ),
            EvidenceRecord(
                ref="context-only", source="web", evidence_type="copy",
                content="Context that must not be classified.",
                metadata={"source_class": "owned_copy", "identity_match": "domain"},
            ),
        ],
    )
    llm = StubLabelingLLM([
        {
            "ref": "selected",
            "relevant_blocks": ["mission"],
            "stance": "supports",
            "identity_match": "domain",
            "specificity": "explicit",
        }
    ])

    debug = label_evidence_pack(pack, llm=llm, selected_refs=["selected"])

    prompt = json.loads(llm.calls[0]["user"])
    assert len(prompt["records"]) == 1
    assert prompt["records"][0]["content"] == "Selected mission evidence."
    assert len(prompt["owned_identity_context"]) == 2
    assert debug["records_labeled"] == 1
    assert "semantic_labeling_version" not in pack.evidence[1].metadata
