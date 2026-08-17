import pytest

from src.sv9_flow.semantic_passages import semantic_passages
from src.sv9_flow.evidence_tile_relation_worker import (
    EvidenceTileRelationProposalError,
    propose_evidence_tile_relations,
)


class RelationLLM:
    api_key = "test"

    def __init__(self, relations):
        self.relations = relations
        self.calls = []

    def _call_json(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return {"relations": self.relations}


def _evidence():
    return [{
        "evidence_fingerprint": "a" * 64,
        "ref": "raw.0",
        "source": "web",
        "evidence_type": "copy",
        "url": "https://acme.example",
        "content": "We help teams ship better products.",
        "labels": {"relevant_blocks": ["mission"]},
    }]


def _shortlist():
    return {"a" * 64: [{
        "tile_id": "M1",
        "tile_key": "mission.M1",
        "component_key": "mission",
        "condition": "The mission states what the organization does.",
    }]}


def test_relation_worker_returns_only_literal_scoped_proposals() -> None:
    llm = RelationLLM([{
        "evidence_fingerprint": "a" * 64,
        "tile_id": "M1",
        "polarity": "supports",
        "literal_quote": "help teams ship better products",
        "rationale": "This directly states the contribution.",
    }])

    result = propose_evidence_tile_relations(
        evidence_rows=_evidence(), tile_shortlists=_shortlist(), llm=llm
    )

    assert result["relations"][0]["tile_id"] == "M1"
    assert "authority" not in result["relations"][0]
    assert "score" not in result["relations"][0]
    assert result["discarded_relations"] == []
    prompt = __import__("json").loads(llm.calls[0][1].split("\n", 1)[1])
    assert prompt[0]["content"] == "We help teams ship better products."


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("literal_quote", "invented quote", "quote_not_literal"),
        ("literal_quote", ".", "quote_not_literal"),
        ("tile_id", "M2", "tile_outside_shortlist"),
        ("polarity", "demonstrates_absence", "polarity_invalid"),
        ("literal_quote", 1, "field_type_invalid"),
        ("rationale", ["not", "a", "string"], "field_type_invalid"),
    ],
)
def test_relation_worker_discards_model_output_outside_contract(
    field, value, message
) -> None:
    relation = {
        "evidence_fingerprint": "a" * 64,
        "tile_id": "M1",
        "polarity": "supports",
        "literal_quote": "help teams ship better products",
        "rationale": "Direct statement.",
    }
    relation[field] = value

    result = propose_evidence_tile_relations(
        evidence_rows=_evidence(),
        tile_shortlists=_shortlist(),
        llm=RelationLLM([relation]),
    )

    assert result["relations"] == []
    assert result["discarded_relations"][0]["reason"] == message
    assert len(
        result["discarded_relations"][0]["submitted_relation_fingerprint"]
    ) == 64



def test_relation_windows_cover_quote_crossing_a_passage_boundary() -> None:
    quote = ("material claim " * 19) + "material claim"
    content = ("x" * 47_950) + quote + ("z" * 100)
    evidence = _evidence()
    evidence[0]["content"] = content
    llm = RelationLLM([{
        "evidence_fingerprint": "a" * 64,
        "tile_id": "M1",
        "polarity": "supports",
        "literal_quote": quote,
        "rationale": "The complete claim crosses the first passage boundary.",
    }])

    assert [len(semantic_passages("x" * size)) for size in (47_681, 47_682, 48_000, 2_097_152)] == [1, 1, 1, 44]
    result = propose_evidence_tile_relations(
        evidence_rows=evidence,
        tile_shortlists=_shortlist(),
        llm=llm,
    )

    assert len(llm.calls) > 1 and result["relations"][0]["literal_quote"] == quote


def test_relation_worker_rejects_oversized_model_payload() -> None:
    relation = {
        "evidence_fingerprint": "a" * 64,
        "tile_id": "M1",
        "polarity": "supports",
        "literal_quote": "help teams ship better products",
        "rationale": "Direct statement.",
    }

    with pytest.raises(
        EvidenceTileRelationProposalError,
        match="invalid payload",
    ):
        propose_evidence_tile_relations(
            evidence_rows=_evidence(),
            tile_shortlists=_shortlist(),
            llm=RelationLLM([relation] * 121),
        )
