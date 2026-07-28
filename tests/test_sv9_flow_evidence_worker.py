from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot, _identity_match, _is_strategic_surface


def test_evidence_worker_prefers_markdown_content_before_title() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "title": "Acme - Homepage",
                        "markdown_content": "Acme helps support teams automate high-stakes calls.",
                    },
                }
            ],
        }
    )

    assert pack.evidence[0].content == "Acme helps support teams automate high-stakes calls."
    assert pack.evidence[0].metadata["source_class"] == "owned_copy"


def test_evidence_worker_dedups_repeated_raw_input_text() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {"source": "web", "payload": {"text": "Same capture text."}},
                {"source": "hyperbrowser", "payload": {"text": "  Same   capture TEXT. "}},
                {"source": "web", "payload": {"text": "Different text."}},
            ],
        }
    )

    normalized = [" ".join(record.content.split()).lower() for record in pack.evidence]
    assert normalized.count("same capture text.") == 1
    kept = next(record for record in pack.evidence if "Same capture" in record.content)
    assert kept.ref == "raw_inputs.0"
    assert kept.metadata["duplicate_refs"] == ["raw_inputs.1"]
    assert "deduplicated_raw_input_records:1" in pack.limitations
    assert any("Different text." in record.content for record in pack.evidence)


def test_evidence_worker_strips_cross_page_boilerplate_from_subpages() -> None:
    nav = "Products Banking Solutions Pricing Bring clarity to your clients."
    markdown = (
        "Homepage copy.\n" + nav + "\n"
        "\n---\n## Subpage: https://acme.example/about\n" + nav + "\nAbout body text with real substance here.\n"
        "\n---\n## Subpage: https://acme.example/jobs\n" + nav + "\nOur values. Your strengths.\n"
    )
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {"source": "web", "payload": {"url": "https://acme.example", "markdown_content": markdown}}
            ],
        }
    )

    subpage_contents = [r.content for r in pack.evidence if ".subpage." in r.ref]
    assert subpage_contents
    assert all(nav not in content for content in subpage_contents)
    assert any("Our values" in content for content in subpage_contents)
    homepage = next(r for r in pack.evidence if r.ref == "raw_inputs.0")
    assert nav in homepage.content


def test_evidence_worker_splits_owned_subpages_into_addressable_chunks() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://acme.example",
                        "markdown_content": (
                            "Homepage copy.\n"
                            "\n---\n## Subpage: https://acme.example/blog/manifesto\n"
                            "# Manifesto\n\n"
                            + ("Operational setup. " * 60)
                            + "We believe healthcare teams should spend less time coordinating and more time caring."
                        ),
                    },
                }
            ],
        }
    )

    refs = {record.ref: record for record in pack.evidence}

    assert "raw_inputs.0" in refs
    assert "raw_inputs.0.subpage.1.chunk.1" in refs
    assert any(
        record.url == "https://acme.example/blog/manifesto"
        and "We believe healthcare teams" in record.content
        for record in pack.evidence
    )


def test_identity_match_matrix() -> None:
    assert (
        _identity_match(
            brand_name="Acme",
            scan_url="https://www.acme.com",
            record_url="https://acme.com/about",
            content="",
        )
        == "domain"
    )
    assert (
        _identity_match(
            brand_name="Acme",
            scan_url="https://acme.com",
            record_url="https://blog.acme.com/post",
            content="",
        )
        == "domain"
    )
    assert (
        _identity_match(
            brand_name="Café Río S.L.",
            scan_url="https://caferio.com",
            record_url="https://review.example/case",
            content="Cafe Rio is discussed by customers.",
        )
        == "brand_name"
    )
    assert (
        _identity_match(
            brand_name="Toteemi",
            scan_url="https://toteemi.com",
            record_url="https://trustpilot.com/review/toteemi.com",
            content="Independent review page.",
        )
        == "brand_name"
    )
    assert (
        _identity_match(
            brand_name="Acme",
            scan_url="https://acme.com",
            record_url="https://foreign.example/story",
            content="A different company is discussed.",
        )
        == "none"
    )
    assert (
        _identity_match(
            brand_name="AI",
            scan_url="https://ai.example",
            record_url="https://foreign.example/story",
            content="AI appears everywhere.",
        )
        == "unverified"
    )


def test_evidence_worker_chunks_homepage_like_subpages() -> None:
    long_homepage = "# Hero\n" + ("Opening copy. " * 70) + "\n\n## Purpose\nCopy beyond old ceiling is now visible."
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {"url": "https://acme.example", "markdown_content": long_homepage},
                }
            ],
        }
    )

    refs = {record.ref: record for record in pack.evidence}

    assert "raw_inputs.0" in refs
    assert "raw_inputs.0.chunk.2" in refs
    assert any("Copy beyond old ceiling" in record.content for record in pack.evidence)


def test_evidence_worker_chunks_one_page_site_without_subpage_marker() -> None:
    markdown = "# Home\n" + ("Brand story. " * 80) + "\n\n## Values\nWe value direct evidence."
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {"source": "web", "payload": {"url": "https://acme.example", "markdown_content": markdown}}
            ],
        }
    )

    assert any(record.ref == "raw_inputs.0.chunk.2" for record in pack.evidence)
    assert any("We value direct evidence" in record.content for record in pack.evidence)


def test_evidence_worker_prioritizes_late_homepage_values_section() -> None:
    sections = ["# Hero\nOpening copy."]
    sections.extend(f"## Product {index}\nProduct detail." for index in range(1, 12))
    sections.append("## Valores\nAcreditamos en salud, consistencia y confianza.")
    markdown = "\n\n".join(sections)

    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Movyn", "url": "https://movyn.ai"},
            "raw_inputs": [
                {"source": "web", "payload": {"url": "https://movyn.ai", "markdown_content": markdown}}
            ],
        }
    )

    values = [record for record in pack.evidence if "Acreditamos en salud" in record.content]
    assert values
    assert not any(record.evidence_type == "acquisition.attempt.strategic_surfaces" for record in pack.evidence)
    assert any(record.evidence_type == "acquisition.evidence_sampling" for record in pack.evidence)


def test_evidence_worker_keeps_short_homepage_record_unchanged() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {"source": "web", "payload": {"url": "https://acme.example", "markdown_content": "Short homepage."}}
            ],
        }
    )

    homepage_records = [record for record in pack.evidence if record.ref.startswith("raw_inputs.0")]

    assert len(homepage_records) == 2
    assert homepage_records[0].ref == "raw_inputs.0"
    assert homepage_records[0].content == "Short homepage."
    assert homepage_records[1].ref == "raw_inputs.0.diagnostics.strategic_surfaces"


def test_evidence_worker_chunks_owned_subpages_by_markdown_sections() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://acme.example",
                        "markdown_content": (
                            "Homepage copy.\n"
                            "\n---\n## Subpage: https://acme.example/about\n"
                            "# About Acme\n"
                            "Acme helps operators move faster.\n\n"
                            "## Our values\n"
                            "We value craft, clarity, and customer trust.\n\n"
                            "## Careers\n"
                            "Join the team building durable financial tools."
                        ),
                    },
                }
            ],
        }
    )

    subpage_chunks = [record.content for record in pack.evidence if ".subpage.1.chunk." in record.ref]

    assert any(chunk.startswith("# About Acme") for chunk in subpage_chunks)
    assert any(chunk.startswith("## Our values") and "craft, clarity" in chunk for chunk in subpage_chunks)
    assert any(chunk.startswith("## Careers") for chunk in subpage_chunks)


def test_evidence_worker_records_absence_on_crawled_strategic_surfaces() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://acme.example",
                        "markdown_content": (
                            "Homepage copy.\n"
                            "\n---\n## Subpage: https://acme.example/about\n"
                            "# About Acme\n"
                            "Acme builds operational software for finance teams."
                        ),
                    },
                }
            ],
        }
    )

    absence = {
        record.ref: record
        for record in pack.evidence
        if record.evidence_type.startswith("acquisition.absence.")
    }

    assert "raw_inputs.0.subpage.1.absence.values" in absence
    assert "raw_inputs.0.subpage.1.absence.vision" in absence
    assert absence["raw_inputs.0.subpage.1.absence.values"].metadata["source_class"] == "acquisition_metadata"
    assert absence["raw_inputs.0.subpage.1.absence.values"].metadata["absence_signal"] == "keyword_miss"
    assert "no explicit values" in absence["raw_inputs.0.subpage.1.absence.values"].content


def test_strategic_surface_url_matrix_includes_spanish_about_slugs() -> None:
    assert _is_strategic_surface("https://toteemi.com/sobre-toteemi/") is True
    assert _is_strategic_surface("https://brand.com/quienes-somos/") is True
    assert _is_strategic_surface("https://brand.com/quiénes-somos/") is True
    assert _is_strategic_surface("https://brand.com/filosofia/") is True
    assert _is_strategic_surface("https://brand.com/manifiesto/") is True
    assert _is_strategic_surface("https://brand.com/about") is True
    assert _is_strategic_surface("https://brand.com/culture") is True
    assert _is_strategic_surface("https://toteemi.com/misiones/") is False
    assert _is_strategic_surface("https://brand.com/agricultura/") is False
    assert _is_strategic_surface("https://brand.com/about-cookies") is False
    assert _is_strategic_surface("https://brand.com/cookies/about") is True
    assert _is_strategic_surface("https://brand.com/our-values/") is True
    assert _is_strategic_surface("https://brand.com/") is False
    assert _is_strategic_surface("") is False
    assert _is_strategic_surface("https://brand.com/categoria-producto/zapatillas") is False


def test_evidence_worker_records_absence_on_sobre_surface() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Toteemi", "url": "https://toteemi.com"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://toteemi.com",
                        "markdown_content": (
                            "Homepage copy.\n"
                            "\n---\n## Subpage: https://toteemi.com/sobre-toteemi/\n"
                            "# Sobre Toteemi\n"
                            "Toteemi construye una experiencia deportiva gamificada."
                        ),
                    },
                }
            ],
        }
    )

    absence = {record.ref: record for record in pack.evidence if record.evidence_type.startswith("acquisition.absence.")}

    assert "raw_inputs.0.subpage.1.absence.values" in absence
    assert "raw_inputs.0.subpage.1.absence.vision" in absence


def test_evidence_worker_records_no_strategic_surfaces_attempt_when_none_found() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Shop", "url": "https://shop.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://shop.example",
                        "markdown_content": (
                            "Homepage copy.\n"
                            "\n---\n## Subpage: https://shop.example/products/bike\n"
                            "# Product page\n"
                            "Cycling shoes and apparel."
                        ),
                    },
                }
            ],
        }
    )

    record = next(
        item for item in pack.evidence if item.evidence_type == "acquisition.attempt.strategic_surfaces"
    )

    assert record.metadata["status"] == "none_found"
    assert record.metadata["crawled_url_count"] == 2


def test_evidence_worker_skips_not_found_subpage_chunks() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "web",
                    "payload": {
                        "url": "https://acme.example",
                        "markdown_content": (
                            "Homepage copy.\n"
                            "\n---\n## Subpage: https://acme.example/missing\n"
                            "# 404 Not Found\n\n"
                            "Not Found\n\n"
                            "The requested URL was not found on this server."
                        ),
                    },
                }
            ],
        }
    )

    refs = {record.ref for record in pack.evidence}

    assert "raw_inputs.0" in refs
    assert "raw_inputs.0.subpage.1.chunk.1" not in refs
    assert "raw_inputs.0.subpage.1.diagnostics.not_found" in refs


def test_evidence_worker_records_searchapi_no_results_as_acquisition_attempt() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "searchapi",
                    "payload": {
                        "version": "vertical_fallback_v1",
                        "provider": "searchapi",
                        "status": "ok",
                        "intents": {
                            "news": {
                                "status": "no_results",
                                "query": "Acme funding news",
                                "engine": "google_light",
                                "results": [],
                            }
                        },
                    },
                }
            ],
        }
    )

    record = next(item for item in pack.evidence if item.ref == "raw_inputs.0.searchapi.diagnostics.news")

    assert record.evidence_type == "acquisition.attempt.news"
    assert record.metadata["source_class"] == "acquisition_metadata"
    assert record.metadata["provider"] == "searchapi"
    assert record.metadata["status"] == "no_results"


def test_evidence_worker_classifies_acquisition_metadata() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "social",
                    "payload": {"summary": "Failed to scrape social channels; followers_count: 0."},
                }
            ],
        }
    )

    assert pack.evidence[0].metadata["source_class"] == "acquisition_metadata"


def test_evidence_worker_drops_legacy_derived_strategy_features() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "features": [
                {
                    "dimension_name": "coherencia",
                    "feature_name": "tone_analysis",
                    "raw_value": "Derived tone verdict from a previous pass.",
                    "confidence": "high",
                },
                {
                    "dimension_name": "vitalidad",
                    "feature_name": "community_signal",
                    "raw_value": "External users discuss Acme in public forums.",
                    "confidence": "medium",
                },
            ],
        }
    )

    refs = {record.ref: record for record in pack.evidence}

    assert "features.0" not in refs
    assert refs["features.1"].metadata["source_class"] == "external_proof"
    assert refs["features.dropped_derived_strategy"].metadata["source_class"] == "acquisition_metadata"
    assert "Dropped 1 legacy derived-strategy" in refs["features.dropped_derived_strategy"].content


def test_evidence_worker_exposes_exa_results_as_citable_external_proof() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "exa",
                    "payload": {
                        "mentions": [
                            {
                                "url": "https://review.example/acme",
                                "title": "Acme reviewed by operators",
                                "text": "Customers praise Acme's active community and adoption.",
                                "score": 0.82,
                                "intent": "external_mentions",
                                "metadata": {
                                    "external_identity_provenance": {
                                        "schema_version": "external-identity-provenance-v1",
                                        "policy_version": "external-identity-policy-v1",
                                        "provider": "exa",
                                        "subject_domain": "acme.example",
                                        "source_domain": "review.example",
                                        "matched_alias": "acme",
                                        "match_method": "alias_in_title",
                                        "match_score": 0.95,
                                        "collector_source_class": "external",
                                        "collector_relation": "external",
                                        "requires_human_review": False,
                                        "candidate_strength": "strong",
                                    }
                                },
                            }
                        ],
                        "news": [
                            {
                                "url": "https://news.example/acme-funding",
                                "title": "Acme raises funding",
                                "highlights": ["Acme is growing quickly in the market."],
                                "intent": "news",
                            }
                        ],
                        "diagnostics": {
                            "failed_intents": [],
                            "no_result_intents": [],
                            "intent_results": {},
                        },
                    },
                }
            ],
        }
    )

    refs = {record.ref: record for record in pack.evidence}

    mention = refs["raw_inputs.0.exa.mentions.0"]
    assert mention.source == "exa"
    assert mention.evidence_type == "external_proof.external_mentions"
    assert mention.url == "https://review.example/acme"
    assert mention.confidence == "high"
    assert mention.metadata["source_class"] == "external_proof"
    assert mention.metadata["identity_match"] == "brand_name"
    assert mention.metadata["external_identity_provenance"]["matched_alias"] == "acme"
    assert "active community" in mention.content
    assert refs["raw_inputs.0.exa.news.0"].metadata["intent"] == "news"


def test_evidence_worker_lowers_external_result_without_identity_match() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "exa",
                    "payload": {
                        "mentions": [
                            {
                                "url": "https://foreign.example/story",
                                "title": "Unrelated market news",
                                "text": "Beta launches a new product.",
                                "score": 0.91,
                                "intent": "external_mentions",
                            }
                        ],
                    },
                }
            ],
        }
    )

    record = next(item for item in pack.evidence if item.ref == "raw_inputs.0.exa.mentions.0")

    assert record.confidence == "low"
    assert record.metadata["identity_match"] == "none"


def test_evidence_worker_demotes_owned_confirmation_without_domain_match() -> None:
    domain_pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "exa",
                    "payload": {
                        "mentions": [
                            {
                                "url": "https://www.acme.example/about",
                                "title": "Acme about page",
                                "text": "Acme builds operating software.",
                                "intent": "owned_confirmation",
                            }
                        ],
                    },
                }
            ],
        }
    )
    foreign_pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "exa",
                    "payload": {
                        "mentions": [
                            {
                                "url": "https://review.example/acme",
                                "title": "Acme review",
                                "text": "Acme is mentioned here.",
                                "intent": "owned_confirmation",
                            }
                        ],
                    },
                }
            ],
        }
    )

    owned = next(item for item in domain_pack.evidence if item.ref == "raw_inputs.0.exa.mentions.0")
    demoted = next(item for item in foreign_pack.evidence if item.ref == "raw_inputs.0.exa.mentions.0")

    assert owned.metadata["source_class"] == "owned_copy"
    assert owned.metadata["identity_match"] == "domain"
    assert demoted.metadata["source_class"] == "external_proof"
    assert demoted.metadata["identity_match"] == "brand_name"
    assert demoted.metadata["intent_demoted"] == "owned_confirmation_without_domain_match"


def test_evidence_worker_does_not_lower_unverified_external_identity() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "AI", "url": "https://ai.example"},
            "raw_inputs": [
                {
                    "source": "searchapi",
                    "payload": {
                        "intents": {
                            "news": {
                                "status": "ok",
                                "results": [
                                    {
                                        "url": "https://foreign.example/story",
                                        "title": "AI appears everywhere",
                                        "snippet": "A generic AI article.",
                                    }
                                ],
                            }
                        }
                    },
                }
            ],
        }
    )

    record = next(item for item in pack.evidence if item.ref == "raw_inputs.0.searchapi.news.0")

    assert record.confidence == "medium"
    assert record.metadata["identity_match"] == "unverified"


def test_evidence_worker_keeps_exa_failed_intents_as_acquisition_metadata() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "exa",
                    "payload": {
                        "mentions": [],
                        "news": [],
                        "diagnostics": {
                            "failed_intents": ["news"],
                            "no_result_intents": ["ai_visibility"],
                            "intent_results": {
                                "news": {
                                    "query": "Acme recent news",
                                    "error": "provider timeout",
                                },
                                "ai_visibility": {
                                    "query": "Acme llms.txt",
                                },
                            },
                        },
                    },
                }
            ],
        }
    )

    records = {record.ref: record for record in pack.evidence}

    failed = records["raw_inputs.0.exa.diagnostics.failed.0"]
    assert failed.metadata["source_class"] == "acquisition_metadata"
    assert failed.metadata["intent"] == "news"
    assert "provider timeout" in failed.content
    assert records["raw_inputs.0.exa.diagnostics.no_results.0"].metadata["intent"] == "ai_visibility"


def test_evidence_worker_dedups_exa_results_repeated_across_groups() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "exa",
                    "payload": {
                        "mentions": [
                            {
                                "url": "https://news.example/acme-funding",
                                "title": "Acme raises funding",
                                "text": "Acme announces a new funding round.",
                                "intent": "external_mentions",
                            }
                        ],
                        "news": [
                            {
                                "url": "https://news.example/acme-funding/",
                                "title": "Acme raises funding",
                                "highlights": ["Acme announces a new funding round."],
                                "intent": "news",
                            },
                            {
                                "url": "https://news.example/acme-partnership",
                                "title": "Acme partners with Beta",
                                "highlights": ["Acme signs a partnership."],
                                "intent": "news",
                            },
                        ],
                        "diagnostics": {
                            "failed_intents": [],
                            "no_result_intents": [],
                            "intent_results": {},
                        },
                    },
                }
            ],
        }
    )

    refs = [record.ref for record in pack.evidence if ".exa." in record.ref and "diagnostics" not in record.ref]

    assert "raw_inputs.0.exa.mentions.0" in refs
    assert "raw_inputs.0.exa.news.0" not in refs
    assert "raw_inputs.0.exa.news.1" in refs


def test_evidence_worker_exposes_searchapi_results_as_external_proof() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "searchapi",
                    "payload": {
                        "version": "vertical_fallback_v1",
                        "provider": "searchapi",
                        "intents": {
                            "news": {
                                "status": "ok",
                                "query": "Acme news funding",
                                "engine": "google_light",
                                "results": [
                                    {
                                        "url": "https://news.example/acme",
                                        "title": "Acme launches community program",
                                        "snippet": "Acme customers are forming an active operator community.",
                                        "domain": "news.example",
                                        "position": 1,
                                    }
                                ],
                            }
                        },
                    },
                }
            ],
        }
    )

    record = next(item for item in pack.evidence if item.ref == "raw_inputs.0.searchapi.news.0")

    assert record.source == "searchapi"
    assert record.evidence_type == "external_proof.news"
    assert record.url == "https://news.example/acme"
    assert record.metadata["source_class"] == "external_proof"
    assert record.metadata["provider"] == "searchapi"
    assert "operator community" in record.content


def test_evidence_worker_exposes_github_repo_metrics_as_external_proof() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "github",
                    "payload": {
                        "version": "github-proof-v1",
                        "provider": "github",
                        "repos": [
                            {
                                "full_name": "acme/acme",
                                "html_url": "https://github.com/acme/acme",
                                "description": "Open source runtime for Acme developers.",
                                "stars": 2400,
                                "forks": 180,
                                "open_issues": 42,
                                "language": "TypeScript",
                                "topics": ["developer-tools", "runtime"],
                                "pushed_at": "2026-07-01T10:00:00Z",
                            }
                        ],
                    },
                }
            ],
        }
    )

    record = next(item for item in pack.evidence if item.ref == "raw_inputs.0.github.repos.0")

    assert record.evidence_type == "external_proof.repository"
    assert record.url == "https://github.com/acme/acme"
    assert record.confidence == "high"
    assert record.metadata["source_class"] == "external_proof"
    assert record.metadata["provider"] == "github"
    assert record.metadata["stars"] == 2400
    assert "2400 stars" in record.content
    assert "developer-tools" in record.content


def test_evidence_worker_records_github_skipped_as_acquisition_attempt() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "github",
                    "payload": {
                        "version": "github-proof-v1",
                        "provider": "github",
                        "status": "skipped",
                        "repos": [],
                        "diagnostics": {"reason": "no GitHub repository links observed on owned capture"},
                    },
                }
            ],
        }
    )

    record = next(item for item in pack.evidence if item.ref == "raw_inputs.0.github.diagnostics.status")

    assert record.evidence_type == "acquisition.attempt.repository_proof"
    assert record.metadata["source_class"] == "acquisition_metadata"
    assert record.metadata["provider"] == "github"
    assert record.metadata["status"] == "skipped"


def test_evidence_worker_exposes_acquisition_steps_without_raw_payloads() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "acquisition_steps": {
                "github": {
                    "source": "github",
                    "status": "skipped",
                    "cache_status": "skipped",
                    "eligible": False,
                    "details": {"reason": "no GitHub repository links observed on owned capture"},
                },
                "searchapi": {
                    "source": "searchapi",
                    "status": "skipped",
                    "cache_status": "skipped",
                    "eligible": False,
                    "details": {"reason": "no failed or empty Exa intents eligible for SearchAPI fallback"},
                },
                "hyperbrowser": {
                    "source": "hyperbrowser",
                    "status": "disabled",
                    "cache_status": "disabled",
                    "eligible": False,
                    "details": {"reason": "not requested"},
                },
                "social": {
                    "source": "social",
                    "status": "skipped",
                    "cache_status": "skipped",
                    "eligible": False,
                    "details": {},
                },
            },
        }
    )

    records = {record.ref: record for record in pack.evidence}

    assert records["acquisition_steps.github"].evidence_type == "acquisition.attempt.repository_proof"
    assert records["acquisition_steps.github"].metadata["source_class"] == "acquisition_metadata"
    assert records["acquisition_steps.github"].metadata["status"] == "skipped"
    assert records["acquisition_steps.searchapi"].evidence_type == "acquisition.attempt.external_proof"
    assert records["acquisition_steps.searchapi"].metadata["provider"] == "searchapi"
    assert "acquisition_steps.hyperbrowser" not in records
    assert "acquisition_steps.social" not in records


def test_evidence_worker_classifies_entity_research_packet_as_acquisition_metadata() -> None:
    pack = build_evidence_pack_from_snapshot(
        {
            "run": {"brand_name": "Acme", "url": "https://acme.example"},
            "raw_inputs": [
                {
                    "source": "entity_research_packet",
                    "payload": {
                        "summary": '{"block_source_guidance": {"magnetism": ["audited_surface"]}}',
                    },
                }
            ],
        }
    )

    assert pack.evidence[0].metadata["source_class"] == "acquisition_metadata"
