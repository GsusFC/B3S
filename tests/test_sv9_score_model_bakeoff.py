from scripts.sv9_score_model_bakeoff import compare_runs, render_markdown


def test_compare_runs_reports_score_and_tile_deltas():
    comparison = compare_runs(
        [
            {
                "model": "gemini-3.1-flash-lite",
                "repeat_index": 1,
                "score": 26,
                "base_average": 2.4,
                "detected_blocks": ["mission"],
                "components": {
                    "mission": {
                        "status": "scored",
                        "score": 2,
                        "lit_tiles": ["MI1", "MI2"],
                        "off_tiles": ["MI3"],
                        "blind_spot_tiles": [],
                    }
                },
            },
            {
                "model": "gemini-3.5-flash",
                "repeat_index": 1,
                "score": 34,
                "base_average": 3.1,
                "detected_blocks": ["mission", "vision"],
                "components": {
                    "mission": {
                        "status": "scored",
                        "score": 3,
                        "lit_tiles": ["MI1", "MI2", "MI3"],
                        "off_tiles": [],
                        "blind_spot_tiles": [],
                    }
                },
            },
        ]
    )

    assert comparison["score_delta"] == 8
    assert comparison["base_average_delta"] == 0.7
    assert comparison["detected_added"] == ["vision"]
    assert comparison["changed_components"] == ["mission"]
    assert comparison["components"]["mission"]["lit_added"] == ["MI3"]


def test_render_markdown_lists_full_score_summary():
    markdown = render_markdown(
        {
            "brand_name": "Case",
            "url": "https://case.test",
            "evidence": {"record_count": 1, "source_counts": {"web": 1}},
            "models": ["a", "b"],
            "gate_authority": "disabled",
            "runs": [
                {
                    "model": "a",
                    "score": 12,
                    "base_average": 1.5,
                    "reliability_status": "shadow",
                    "detected_blocks": ["mission"],
                    "not_detected": ["vision"],
                    "components": {
                        "mission": {
                            "status": "scored",
                            "score": 1,
                            "scale": 5,
                            "points": 1,
                            "lit_tiles": ["MI1"],
                            "off_tiles": ["MI2"],
                            "blind_spot_tiles": [],
                        }
                    },
                }
            ],
            "comparison": {
                "baseline_model": "a",
                "candidate_model": "b",
                "score_delta": 4,
                "detected_added": ["vision"],
                "detected_removed": [],
                "changed_components": ["mission"],
            },
        }
    )

    assert "# SV9 score model bakeoff" in markdown
    assert "| a | 12 | 1.5 | shadow | mission | vision |" in markdown
    assert "Delta score (b - a): 4" in markdown
