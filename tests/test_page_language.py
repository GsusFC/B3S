from src.collectors.page_language import (
    PAGE_LANGUAGE_DETECTION_VERSION,
    detect_page_language,
    summarize_page_languages,
)


def test_detects_english_from_page_copy() -> None:
    result = detect_page_language(
        """
        We help clubs make better decisions through evidence and analysis.
        Our software is built with the industry and for the industry.
        The platform gives every club more control over their process,
        because better information is the foundation of better decisions.
        """,
        html='<html lang="en">',
    )

    assert result["version"] == PAGE_LANGUAGE_DETECTION_VERSION
    assert result["observed_language"] == "en"
    assert result["confidence"] == "high"
    assert result["declared_language"] == "en"
    assert result["declared_mismatch"] is False


def test_detects_spanish_and_declared_language_mismatch() -> None:
    result = detect_page_language(
        """
        Ayudamos a los clubes a tomar mejores decisiones con análisis.
        Nuestra plataforma aplica el método científico al fútbol y permite
        que cada club tenga más control sobre sus fichajes. El objetivo es
        mejorar la eficiencia del mercado y la calidad de cada decisión.
        """,
        html='<html lang="en-US">',
    )

    assert result["observed_language"] == "es"
    assert result["confidence"] == "high"
    assert result["declared_language"] == "en"
    assert result["declared_mismatch"] is True


def test_detects_balanced_english_spanish_page_as_mixed() -> None:
    result = detect_page_language(
        """
        We help clubs make better decisions with evidence. Our software is
        built for the industry and gives every team more control.
        Ayudamos a los clubes a tomar mejores decisiones con evidencia.
        Nuestra plataforma está creada para la industria y ofrece más control.
        """
    )

    assert result["observed_language"] == "mixed_en_es"
    assert result["confidence"] == "medium"


def test_does_not_guess_from_short_or_unsupported_copy() -> None:
    short = detect_page_language("The future of football.")
    german = detect_page_language(
        "Wir entwickeln quantitative Werkzeuge für professionelle "
        "Fußballvereine und verbessern Entscheidungen im Transfermarkt. " * 4
    )

    assert short["observed_language"] == "und"
    assert german["observed_language"] == "und"


def test_language_summary_counts_only_captured_pages() -> None:
    summary = summarize_page_languages(
        [
            {
                "status": "captured",
                "observed_language": "en",
                "declared_mismatch": False,
            },
            {
                "status": "captured",
                "observed_language": "es",
                "declared_mismatch": True,
            },
            {
                "status": "captured",
                "observed_language": "und",
                "declared_mismatch": False,
            },
            {"status": "not_captured", "observed_language": "es"},
        ]
    )

    assert summary["status"] == "mixed"
    assert summary["mixed_language_site"] is True
    assert summary["captured_page_count"] == 3
    assert summary["evaluated_page_count"] == 2
    assert summary["unknown_page_count"] == 1
    assert summary["declared_mismatch_count"] == 1
    assert summary["distribution"] == [
        {"language": "en", "page_count": 1, "share": 0.5},
        {"language": "es", "page_count": 1, "share": 0.5},
        {"language": "und", "page_count": 1, "share": 0.0},
    ]
