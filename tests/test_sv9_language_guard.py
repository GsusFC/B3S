from src.sv9.language_guard import (
    spanish_component_summary,
    spanish_component_verdict,
    spanish_generated_text,
)


def test_generated_english_summary_falls_back_to_spanish() -> None:
    summary = spanish_component_summary(
        "mission",
        "To move organizations from fragmented AI to integrated intelligence by providing a single optimized access point.",
    )

    assert "Componente Misión detectado" in summary
    assert "To move organizations" not in summary


def test_source_like_short_english_phrase_can_remain_untouched() -> None:
    assert spanish_generated_text("All models. One API.") == "All models. One API."


def test_spanish_component_verdict_rebuilds_from_counts_when_tile_profile_missing() -> None:
    verdict = spanish_component_verdict(
        "magnetism",
        "The snapshot does not provide access to the full product interface.",
        {"lit": 3, "off": 1, "blind": 6, "scale": 10},
    )

    assert "Síntesis automática: 3/10 baldosas encendidas, 1 apagada, 6 puntos ciegos." in verdict
