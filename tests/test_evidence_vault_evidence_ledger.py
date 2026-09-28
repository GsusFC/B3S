import hashlib

import pytest

from src.services import evidence_vault_evidence_ledger as ledger
from src.sv9_flow.contracts import (
    BrandEvidencePack,
    BrandInterpretation,
    EvidenceRecord,
    Sv9FlowCandidate,
    TileSignal,
)
from src.sv9_flow.evidence_worker import build_evidence_pack_from_snapshot


BRAND = "acme.example"
HOME = "https://acme.example"
ABOUT = f"{HOME}/about"
TEAM = f"{HOME}/team"
_PROMISE = "Our founders promise radical transparency on every invoice we send."
_SHIPPING = "Our team ships a small improvement to the ledger every single week."


def _words(count, prefix="w"):
    return " ".join(f"{prefix}{index}" for index in range(count))


def _text_of_length(length, prefix):
    text = _words(length, prefix)[:length].rstrip()
    return text + "x" * (length - len(text))


def _about(sentence=_PROMISE):
    return f"{_words(100, 'a')} {sentence} {_words(100, 'b')}"


def _web(home, *subpages, url=HOME, final_url=None, statuses=None, lastmods=None):
    statuses, lastmods = statuses or {}, lastmods or {}
    markdown = home + "".join(f"\n\n---\n## Subpage: {page_url}\n{text}" for page_url, text in subpages if text)
    visited = [{"url": url, "status": "captured", "lastmod": lastmods.get(url, "")}] + [
        {"url": page_url, "status": statuses.get(page_url, "captured"), "lastmod": lastmods.get(page_url, "")}
        for page_url, _text in subpages
    ]
    return {
        "url": url,
        "markdown_content": markdown,
        "capture_provenance": {"requested_url": url, "final_url": final_url or url},
        "page_selection": {
            "visited_pages": visited,
            "known_pages": [{"url": row["url"], "lastmod": row["lastmod"]} for row in visited],
        },
    }


def _snapshot(web, *, gate="pass", web_status="fetched"):
    snapshot = {
        "run": {"brand_name": "Acme", "url": HOME},
        "acquisition_steps": {"web": {"source": "web", "status": web_status}},
        "raw_inputs": [{"source": "web", "payload": web}],
    }
    if gate is not None:
        snapshot["acquisition_gate"] = {"state": gate}
    return snapshot


def _row(ref, content, *, url=HOME, source="web", source_class="owned_copy", evidence_type="raw_input"):
    return {
        "ref": ref,
        "source": source,
        "evidence_type": evidence_type,
        "url": url,
        "content": content,
        "confidence": "medium",
        "metadata": {"source_class": source_class},
    }


def _news(ref, *, evidence_type="external_proof.news"):
    return _row(
        ref,
        "Acme raised a Series A to expand its finance tooling.",
        url="https://news.example/acme-series-a?utm_source=exa",
        source="exa",
        source_class="external_proof",
        evidence_type=evidence_type,
    )


def _fingerprint(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pack_rows(snapshot):
    return [record.to_dict() for record in build_evidence_pack_from_snapshot(snapshot).evidence]


def _ledger(prior, prior_rows, current, current_rows=(), shown_index=None):
    return ledger.build_evidence_ledger(
        brand_domain=BRAND,
        prior_snapshot=prior,
        prior_rows=list(prior_rows),
        current_snapshot=current,
        current_rows=list(current_rows),
        shown_index=shown_index,
    )


def _only(result):
    [row] = result["rows"]
    return row


def _verdict(row):
    return row["state"], row["reason_codes"]


def _about_pair(current_about, **snapshot_options):
    home = _words(120, "h")
    prior = _snapshot(_web(home, (ABOUT, _about())))
    current = _snapshot(_web(home, (ABOUT, current_about)), **snapshot_options)
    return prior, current


def _promise_row():
    return _row("raw_inputs.0.subpage.1.chunk.1", _PROMISE, url=ABOUT)


def test_unchanged_pair_marks_every_prior_evidence_row_seen():
    snapshot = _snapshot(_web(_words(150, "h"), (ABOUT, _about()), (TEAM, _words(90, "t"))))
    rows = _pack_rows(snapshot)

    result = _ledger(snapshot, rows, snapshot, rows)

    assert [_verdict(row) for row in result["rows"]] == [("seen", [])] * len(result["rows"])
    assert {row["source_key"] for row in result["rows"]} == {ledger.source_key(url) for url in (HOME, ABOUT, TEAM)}


def test_truncated_homepage_is_not_verified_instead_of_absent():
    prior_home = _text_of_length(10_891, "p")
    current_home = _text_of_length(1_270, "c")
    fragment = prior_home[5_000:5_600]

    row = _only(
        _ledger(_snapshot(_web(prior_home)), [_row("raw_inputs.0.chunk.9", fragment)], _snapshot(_web(current_home)))
    )

    assert _verdict(row) == ("not_verified", ["page_truncated"])
    assert (row["health"]["page"]["prior_chars"], row["health"]["page"]["current_chars"]) == (10_891, 1_270)


def test_quote_spanning_a_current_chunk_boundary_is_seen():
    page = _words(400, "r")
    quote = page[300:900]
    current_page = f"## Welcome\n{page}"
    current_rows = [_row("raw_inputs.0", current_page[:620]), _row("raw_inputs.0.chunk.2", current_page[620:])]
    assert not any(" ".join(quote.split()) in " ".join(row["content"].split()) for row in current_rows)

    row = _only(
        _ledger(_snapshot(_web(page)), [_row("raw_inputs.0.chunk.2", quote)], _snapshot(_web(current_page)), current_rows)
    )

    assert _verdict(row) == ("seen", [])


def test_fragment_joined_across_a_removed_boilerplate_line_is_seen():
    nav = "Book a demo with our finance team"

    def page(prefix):
        return f"{prefix} opening line {_words(30, prefix)}\n{nav}\n{prefix} closing line {_words(30, prefix + 'z')}"

    snapshot = _snapshot(_web(page("home"), (ABOUT, page("about")), (TEAM, page("team"))))
    rows = _pack_rows(snapshot)
    about = [row for row in rows if row["url"] == ABOUT and row["metadata"]["source_class"] == "owned_copy"]
    assert len(about) == 1 and nav not in about[0]["content"] and "about closing line" in about[0]["content"]

    row = _only(_ledger(snapshot, about, snapshot, rows))

    assert _verdict(row) == ("seen", [])


def test_not_found_page_is_not_verified():
    not_found = f"404 Not Found\nThe requested URL was not found on this server.\n{_about(_SHIPPING)}"
    prior, current = _about_pair(not_found)

    row = _only(_ledger(prior, [_promise_row()], current))

    assert _verdict(row) == ("not_verified", ["page_404"])


def test_page_missing_from_the_current_selection_is_not_verified():
    home = _words(120, "h")

    row = _only(_ledger(_snapshot(_web(home, (ABOUT, _about()))), [_promise_row()], _snapshot(_web(home))))

    assert _verdict(row) == ("not_verified", ["page_not_visited"])


def test_page_visited_but_not_captured_is_not_verified():
    home = _words(120, "h")
    current = _snapshot(_web(home, (ABOUT, ""), statuses={ABOUT: "not_captured"}))

    row = _only(_ledger(_snapshot(_web(home, (ABOUT, _about()))), [_promise_row()], current))

    assert _verdict(row) == ("not_verified", ["page_not_captured"])


@pytest.mark.parametrize(
    ("gate", "web_status", "reasons"),
    [
        ("blocked", "fetched", ["capture_blocked"]),
        ("degraded_approved", "fetched", ["capture_blocked"]),
        (None, "fetched", ["capture_blocked"]),
        ("pass", "obstructed", ["capture_failed"]),
        ("blocked", "failed", ["capture_blocked", "capture_failed"]),
    ],
)
def test_unhealthy_capture_is_not_verified(gate, web_status, reasons):
    prior, current = _about_pair(_about(_SHIPPING), gate=gate, web_status=web_status)

    row = _only(_ledger(prior, [_promise_row()], current))

    assert _verdict(row) == ("not_verified", reasons)


def test_removed_fragment_on_a_healthy_page_is_verified_absent():
    prior, current = _about_pair(_about(_SHIPPING))

    row = _only(_ledger(prior, [_promise_row()], current))

    assert _verdict(row) == ("verified_absent", [])
    assert row["health"]["page"]["change"] == "unchanged"


def test_www_scheme_and_tracking_variants_are_the_same_owned_source():
    home = _words(120, "h")
    prior_url = "https://www.acme.example/about/?utm_source=newsletter#team"
    prior = _snapshot(_web(home, (prior_url, _about())))
    current = _snapshot(_web(home, ("http://acme.example/about", _about())))

    row = _only(_ledger(prior, [_row("raw_inputs.0.subpage.1.chunk.1", _PROMISE, url=prior_url)], current))

    assert _verdict(row) == ("seen", [])


@pytest.mark.parametrize(
    ("prior_url", "current_url"),
    [(f"{HOME}/es/about", f"{HOME}/en/about"), (f"{HOME}/pricing?plan=pro", f"{HOME}/pricing?plan=team")],
)
def test_locale_or_query_variant_is_a_different_source_that_never_verifies_it(prior_url, current_url):
    home = _words(120, "h")
    prior = _snapshot(_web(home, (prior_url, _about())))
    current = _snapshot(_web(home, (current_url, _about())))

    row = _only(_ledger(prior, [_row("raw_inputs.0.subpage.1.chunk.1", _PROMISE, url=prior_url)], current))

    assert _verdict(row) == ("not_verified", ["page_not_visited"])


def test_homepage_final_url_is_an_alias_of_the_requested_url():
    prior = _snapshot(_web(_about(), url=HOME, final_url=f"{HOME}/es"))
    current = _snapshot(_web(_about(), url=f"{HOME}/es"))

    row = _only(_ledger(prior, [_row("raw_inputs.0", _PROMISE)], current))

    assert _verdict(row) == ("seen", [])


def test_homepage_redirected_to_another_locale_is_not_verified():
    prior = _snapshot(_web(_about(), url=HOME, final_url=f"{HOME}/es"))
    current = _snapshot(_web(_about(_SHIPPING), url=HOME, final_url=f"{HOME}/en"))

    row = _only(_ledger(prior, [_row("raw_inputs.0", _PROMISE)], current))

    assert _verdict(row) == ("not_verified", ["page_not_visited"])


def test_external_item_not_returned_again_is_not_verified():
    snapshot = _snapshot(_web(_words(120, "h")))

    row = _only(_ledger(snapshot, [_news("raw_inputs.1.exa.news.0")], snapshot))

    assert row["evidence_class"] == "external"
    assert _verdict(row) == ("not_verified", ["external_exact_fetch_unavailable"])


def test_external_item_returned_under_another_ref_and_group_is_seen():
    snapshot = _snapshot(_web(_words(120, "h")))
    moved = _news("raw_inputs.2.exa.mentions.4", evidence_type="external_proof.external_mentions")

    row = _only(_ledger(snapshot, [_news("raw_inputs.1.exa.news.0")], snapshot, [moved]))

    assert _verdict(row) == ("seen", [])


def test_search_confirmation_of_an_owned_page_uses_the_external_verifier():
    prior, current = _about_pair(_about(_SHIPPING))
    confirmation = _row(
        "raw_inputs.1.exa.mentions.0",
        _PROMISE,
        url=ABOUT,
        source="exa",
        source_class="owned_copy",
        evidence_type="external_proof.owned_confirmation",
    )

    row = _only(_ledger(prior, [confirmation], current))

    assert row["evidence_class"] == "external"
    assert _verdict(row) == ("not_verified", ["external_exact_fetch_unavailable"])


def _chunk(index):
    return EvidenceRecord(
        ref=f"raw_inputs.0.chunk.{index}",
        source="web",
        evidence_type="raw_input",
        content=f"Mission chunk {index} says {_words(12, f'm{index}x')}",
        url=HOME,
        metadata={"source_class": "owned_copy"},
    )


_LATE = "late fragment that Core never reads"
_LONG = EvidenceRecord(
    ref="raw_inputs.3",
    source="web",
    evidence_type="raw_input",
    content=f"Opening mission statement. {_text_of_length(730, 'q')} {_LATE}.",
    url=HOME,
    metadata={"source_class": "owned_copy"},
)
_SIGNAL = EvidenceRecord(
    ref="raw_inputs.7",
    source="web",
    evidence_type="raw_input",
    content="Signal-only proof of the mission.",
    url=HOME,
    metadata={"source_class": "owned_copy"},
)


def _mission_candidate():
    refs = [_LONG.ref] + [_chunk(index).ref for index in range(1, 10)]
    return Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(
            brand_name="Acme",
            url=HOME,
            evidence=[_LONG, _SIGNAL, *(_chunk(index) for index in range(1, 10))],
        ),
        interpretation=BrandInterpretation(
            brand_name="Acme",
            url=HOME,
            blocks={
                "mission": {
                    "detected": True,
                    "content": "Help finance teams close faster.",
                    "confidence": "high",
                    "rationale": "The homepage states the operating outcome.",
                }
            },
            evidence_refs={"mission": refs},
        ),
        tile_signals=[
            TileSignal(
                component="mission",
                tile="mission.M1",
                effect="supports",
                confidence="high",
                source="brand_interpretation",
                evidence_refs=[_SIGNAL.ref],
            )
        ],
        evaluation_evidence_refs={"mission": refs},
        evaluation_evidence_version="sv9-flow-evaluation-evidence-refs-v1",
    ).to_dict()


def _mission_index():
    return ledger.build_shown_index(
        [
            {"component_key": "mission", "status": "evaluated", "candidate": _mission_candidate()},
            {"component_key": "values", "status": "not_detected", "candidate": _mission_candidate()},
        ]
    )


@pytest.mark.parametrize(
    ("fragment", "status", "reasons", "refs"),
    [
        (_chunk(2).content, "shown", [], ["raw_inputs.0.chunk.2"]),
        ("Opening mission statement.", "shown", [], ["raw_inputs.3"]),
        (_LATE, "not_shown", ["fragment_after_snippet_limit"], []),
        (_chunk(9).content, "not_shown", ["not_in_evaluation_evidence"], []),
        (_SIGNAL.content, "shown_as_signal", [], ["raw_inputs.7"]),
    ],
)
def test_shown_to_core_follows_the_core_prompt_snippets(fragment, status, reasons, refs):
    shown = ledger.shown_to_core(fragment, _mission_index())

    assert shown["mission"] == {"status": status, "reason_codes": reasons, "evidence_refs": refs}


def test_components_without_a_core_prompt_are_not_shown():
    shown = ledger.shown_to_core(_chunk(2).content, _mission_index())

    assert shown["vision"] == {"status": "not_shown", "reason_codes": ["component_not_evaluated"], "evidence_refs": []}
    assert shown["values"] == {"status": "not_shown", "reason_codes": ["component_not_detected"], "evidence_refs": []}
    assert set(shown) == set(ledger.build_shown_index([]))


def test_shown_to_core_never_changes_the_state():
    prior, current = _about_pair(_about(_SHIPPING))
    rows = [_promise_row(), _row("raw_inputs.0.chunk.2", _chunk(2).content)]
    shown = _mission_index()

    without = _ledger(prior, rows, current)
    with_index = _ledger(prior, rows, current, shown_index=shown)

    assert [_verdict(row) for row in with_index["rows"]] == [_verdict(row) for row in without["rows"]]
    assert {row["evidence_ref"]: row["shown_to_core"]["mission"]["status"] for row in with_index["rows"]} == {
        "raw_inputs.0.chunk.2": "shown",
        "raw_inputs.0.subpage.1.chunk.1": "not_shown",
    }


def test_unregistered_evidence_class_is_not_verified():
    snapshot = _snapshot(_web(_words(120, "h")))
    visual = _row(
        "visual_signature.tiles.0",
        "Palette is warm and editorial.",
        source="visual_signature",
        source_class="visual_signal",
        evidence_type="visual_tile_signal",
    )

    row = _only(_ledger(snapshot, [visual], snapshot, [visual]))

    assert row["evidence_class"] == "visual_signature"
    assert _verdict(row) == ("not_verified", ["unknown_evidence_class"])


def test_every_row_is_shadow_only_versioned_and_hash_identified():
    prior, current = _about_pair(_about())
    diagnostics = _row(
        "raw_inputs.0.diagnostics.page_selection",
        "Owned page selection captured 2 of 2 known pages.",
        source_class="acquisition_metadata",
        evidence_type="acquisition.page_selection",
    )
    visual = _row("visual_signature.tiles.0", "Warm palette.", source="visual_signature", source_class="visual_signal")

    result = _ledger(prior, [_promise_row(), _news("raw_inputs.1.exa.news.0"), visual, diagnostics], current)

    assert (result["policy_version"], result["runtime_effect"]) == (ledger.EVIDENCE_LEDGER_POLICY_VERSION, False)
    assert [(row["policy_version"], row["runtime_effect"]) for row in result["rows"]] == [
        (ledger.EVIDENCE_LEDGER_POLICY_VERSION, False)
    ] * 3
    assert result["skipped_rows"] == {"acquisition_metadata": 1}
    for row in result["rows"]:
        assert len(row["evidence_id"]) == len(row["source_identity_id"]) == len(row["evidence_fingerprint"]) == 64
        assert "content" not in row


def test_tile_summary_maps_supporting_pairs_to_ledger_states():
    prior, current = _about_pair(_about(_SHIPPING))
    kept = _row("raw_inputs.0.subpage.1.chunk.2", _words(40, "a"), url=ABOUT)
    rows = _ledger(prior, [_promise_row(), kept], current)["rows"]

    def pair(ref, content):
        return {"evidence_ref": ref, "evidence_fingerprint": _fingerprint(content)}

    judgments = [
        {
            "tile_id": "M1",
            "supporting_evidence": [pair(kept["ref"], kept["content"]), pair(_promise_row()["ref"], _PROMISE)],
        },
        {"tile_id": "V1", "supporting_evidence": [pair(kept["ref"], "stale content")]},
    ]

    assert ledger.summarize_tile_support(rows, judgments) == [
        {
            "tile_id": "M1",
            "supporting_evidence": [
                {**pair(kept["ref"], kept["content"]), "state": "seen", "reason_codes": []},
                {**pair(_promise_row()["ref"], _PROMISE), "state": "verified_absent", "reason_codes": []},
            ],
            "state_counts": {"seen": 1, "verified_absent": 1},
        },
        {
            "tile_id": "V1",
            "supporting_evidence": [
                {
                    **pair(kept["ref"], "stale content"),
                    "state": "not_verified",
                    "reason_codes": ["supporting_evidence_not_in_ledger"],
                }
            ],
            "state_counts": {"not_verified": 1},
        },
    ]
