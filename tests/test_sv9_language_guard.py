from src.sv9.language_guard import spanish_component_summary, spanish_generated_text


def test_generated_english_summary_falls_back_to_spanish() -> None:
    summary = spanish_component_summary(
        "mission",
        "To move organizations from fragmented AI to integrated intelligence by providing a single optimized access point.",
    )

    assert "Componente Misión detectado" in summary
    assert "To move organizations" not in summary


def test_source_like_short_english_phrase_can_remain_untouched() -> None:
    assert spanish_generated_text("All models. One API.") == "All models. One API."
