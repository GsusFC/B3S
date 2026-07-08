from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_labeling_worker import EVIDENCE_LABELING_VERSION, label_evidence_pack


class StubLabelingLLM:
    api_key = "test"

    def __init__(self, labels):
        self.labels = labels
        self.calls = []

    def _call_json(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return {"labels": self.labels}


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
    debug = label_evidence_pack(
        pack,
        llm=StubLabelingLLM(
            [
                {
                    "ref": "raw_inputs.0",
                    "relevant_blocks": ["mission"],
                    "stance": "supports",
                    "identity_match": "domain",
                    "specificity": "implied",
                }
            ]
        ),
    )

    assert debug["status"] == "labeled"
    assert debug["records_labeled"] == 1
    assert pack.schema_version == "brand-evidence-pack-v1"
    assert pack.evidence[0].metadata["relevant_blocks"] == ["mission"]
    assert pack.evidence[0].metadata["stance"] == "supports"
    assert pack.evidence[0].metadata["identity_match_llm"] == "domain"
    assert pack.evidence[0].metadata["specificity"] == "implied"
    assert pack.evidence[0].metadata["semantic_labeling_version"] == EVIDENCE_LABELING_VERSION


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
                    "specificity": "explicit",
                }
            ]
        ),
    )

    assert pack.evidence[0].metadata["identity_match"] == "brand_name"
    assert pack.evidence[0].metadata["identity_match_llm"] == "none"
    assert debug["identity_divergences"] == [
        {
            "ref": "raw_inputs.0",
            "deterministic_identity_match": "brand_name",
            "llm_identity_match": "none",
        }
    ]
