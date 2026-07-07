import json
from pathlib import Path

from scripts.sv9_flow_layer3_business_case import label_records_for_blocks, measure_reports
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord


class StubLabelLLM:
    api_key = "test"

    def __init__(self, labels):
        self.labels = labels
        self.calls = []

    def _call_json(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return {"labels": self.labels}


def test_label_records_for_blocks_enriches_measurement_without_mutating_pack():
    pack = BrandEvidencePack(
        brand_name="Acme",
        url="https://acme.example",
        evidence=[
            EvidenceRecord(
                ref="raw_inputs.0",
                source="web",
                evidence_type="raw_input",
                content="What drives us is making finance work feel lighter.",
                metadata={"source_class": "owned_copy"},
            )
        ],
    )
    llm = StubLabelLLM(
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

    labels = label_records_for_blocks(pack=pack, blocks=["mission"], records=pack.evidence, llm=llm)

    assert labels == [
        {
            "ref": "raw_inputs.0",
            "relevant_blocks": ["mission"],
            "stance": "supports",
            "identity_match": "domain",
            "specificity": "implied",
        }
    ]
    assert "relevant_blocks" not in pack.evidence[0].metadata
    assert llm.calls[0]["kwargs"]["schema_name"] == "sv9_flow_layer3_business_case_labels"


def test_measure_reports_counts_latent_relevant_insufficient_blocks(tmp_path: Path):
    report = _write_report(
        tmp_path,
        coverage_blocks={
            "mission": {"status": "insufficient_acquisition"},
            "values": {"status": "positive_evidence"},
        },
        evidence=[
            {
                "ref": "raw_inputs.0",
                "source": "web",
                "evidence_type": "raw_input",
                "content": "What drives us is making finance work feel lighter.",
                "metadata": {"source_class": "owned_copy"},
            }
        ],
    )
    llm = StubLabelLLM(
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

    result = measure_reports([report], llm=llm)

    assert result["semantic_measured"] is True
    assert result["insufficient_acquisition_blocks"] == 1
    assert result["latent_relevant_blocks"] == 1
    assert result["latent_by_block"] == {"mission": 1}
    assert result["cases"][0]["status"] == "latent_relevant_evidence"
    assert result["cases"][0]["relevant_refs"][0]["ref"] == "raw_inputs.0"


def test_measure_reports_without_llm_reports_unmeasured_gate(tmp_path: Path):
    report = _write_report(
        tmp_path,
        coverage_blocks={"mission": {"status": "insufficient_acquisition"}},
        evidence=[
            {
                "ref": "raw_inputs.0",
                "source": "web",
                "evidence_type": "raw_input",
                "content": "Mission-like copy.",
                "metadata": {"source_class": "owned_copy"},
            }
        ],
    )

    result = measure_reports([report], llm=None)

    assert result["semantic_measured"] is False
    assert result["llm_status"] == "not_run"
    assert result["insufficient_acquisition_blocks"] == 1
    assert result["latent_relevant_blocks"] is None
    assert result["cases"][0]["status"] == "no_labeled_relevant_evidence"


def _write_report(tmp_path: Path, *, coverage_blocks, evidence) -> Path:
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "id": "scan123",
                "brand_name": "Acme",
                "url": "https://acme.example",
                "raw": {
                    "flow": {
                        "candidate": {
                            "evidence_pack": {
                                "brand_name": "Acme",
                                "url": "https://acme.example",
                                "evidence": evidence,
                                "limitations": [],
                            }
                        },
                        "interpretation_debug": {
                            "evidence_coverage": {
                                "blocks": coverage_blocks,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path
