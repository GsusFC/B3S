from argparse import Namespace

from scripts.sv9_flow_model_bakeoff import compare_runs, load_evidence_pack, render_markdown


def test_load_evidence_pack_from_report_json(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        """{
          "raw": {
            "flow": {
              "candidate": {
                "evidence_pack": {
                  "brand_name": "Case",
                  "url": "https://case.test",
                  "limitations": ["limited"],
                  "evidence": [
                    {
                      "ref": "raw_inputs.0",
                      "source": "web",
                      "evidence_type": "raw_input",
                      "content": "Case turns effort into rewards.",
                      "url": "https://case.test",
                      "confidence": "high",
                      "metadata": {"source_class": "owned_copy"}
                    }
                  ]
                }
              }
            }
          }
        }""",
        encoding="utf-8",
    )

    pack = load_evidence_pack(
        Namespace(
            report_json=str(report_path),
            candidate_json=None,
            evidence_pack_json=None,
            run_id=None,
            db_path="",
        )
    )

    assert pack.brand_name == "Case"
    assert pack.evidence[0].ref == "raw_inputs.0"
    assert pack.evidence[0].metadata["source_class"] == "owned_copy"


def test_compare_runs_reports_detected_delta_between_models():
    comparison = compare_runs(
        [
            {
                "model": "gemini-3.1-flash-lite",
                "repeat_index": 1,
                "detected_blocks": ["attributes"],
                "blocks": {
                    "attributes": {"detected": True, "final_source": "llm", "refs": ["raw_inputs.0"]},
                    "magnetism": {"detected": False, "final_source": "gate_rejected", "refs": []},
                },
            },
            {
                "model": "gemini-3.5-flash",
                "repeat_index": 1,
                "detected_blocks": ["attributes", "magnetism"],
                "blocks": {
                    "attributes": {"detected": True, "final_source": "llm", "refs": ["raw_inputs.0"]},
                    "magnetism": {
                        "detected": True,
                        "final_source": "adjudicator_rescued_gate_rejection",
                        "refs": ["raw_inputs.1"],
                    },
                },
            },
        ]
    )

    assert comparison["detected_added"] == ["magnetism"]
    assert comparison["changed_blocks"] == ["magnetism"]


def test_render_markdown_lists_model_block_outputs():
    markdown = render_markdown(
        {
            "brand_name": "Case",
            "url": "https://case.test",
            "evidence": {"record_count": 1, "source_counts": {"web": 1}},
            "models": ["a", "b"],
            "runs": [
                {
                    "model": "a",
                    "status": "ok",
                    "detected_blocks": ["mission"],
                    "tile_signals": {"count": 2},
                    "debug": {"block_failures": []},
                    "blocks": {
                        "mission": {
                            "detected": True,
                            "confidence": "high",
                            "final_source": "llm",
                            "ref_count": 1,
                            "content": "A mission.",
                        }
                    },
                }
            ],
            "comparison": {"candidate_model": "b", "detected_added": [], "detected_removed": [], "changed_blocks": []},
        }
    )

    assert "# SV9 Flow model bakeoff" in markdown
    assert "`mission` on" in markdown
