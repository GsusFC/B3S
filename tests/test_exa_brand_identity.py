from __future__ import annotations

import pytest

from src.collectors.web_collector import WebData
from src.services.exa_brand_identity import (
    BrandIdentity,
    IdentityCandidate,
    IdentityCheckError,
    derive_brand_identity,
    llm_identity_check,
)


def test_identity_name_is_the_longest_title_segment_with_the_brand():
    identity = derive_brand_identity(
        brand_name="Saffron",
        brand_url="https://www.saffron-consultants.com/",
        web_data=WebData(
            url="https://www.saffron-consultants.com/",
            title="Brand, your identity in action - Saffron Brand Consultants",
        ),
    )

    assert identity == BrandIdentity(
        name="Saffron Brand Consultants",
        domain="saffron-consultants.com",
        description="Brand, your identity in action",
        strong_aliases=("saffronbrandconsultants", "saffronconsultants", "saffronconsultantscom"),
    )


def test_single_word_brand_name_is_not_a_strong_alias():
    identity = derive_brand_identity(
        brand_name="Primary",
        brand_url="https://primary.studio",
        web_data=WebData(url="https://primary.studio", title="Primary | Brand Sprints"),
    )

    assert identity.name == "Primary"
    assert identity.domain == "primary.studio"
    assert identity.description == "Brand Sprints"
    assert identity.strong_aliases == ("primarystudio",)


def test_domain_like_brand_name_uses_its_first_label_and_meta_description_wins():
    identity = derive_brand_identity(
        brand_name="sensesbit.com",
        brand_url="https://sensesbit.com",
        web_data=WebData(
            url="https://sensesbit.com",
            title="Sensesbit | Sensory Analysis Software",
            meta_description="Sensory analysis software for food and beverage R&D teams.",
        ),
    )

    assert identity.name == "Sensesbit"
    assert identity.description == "Sensory analysis software for food and beverage R&D teams."
    assert identity.strong_aliases == ("sensesbitcom",)


def test_identity_without_web_data_falls_back_to_the_domain_label():
    identity = derive_brand_identity(
        brand_name="Saffron",
        brand_url="https://saffron-consultants.com",
        web_data=None,
    )

    assert identity == BrandIdentity(
        name="Saffron Consultants",
        domain="saffron-consultants.com",
        description="",
        strong_aliases=("saffronconsultants", "saffronconsultantscom"),
    )


def test_og_site_name_wins_over_the_title_and_og_description_fills_the_description():
    html = (
        "<html><head><title>Home - Saffron</title>"
        '<meta content="Saffron Brand Consultants" property="og:site_name">'
        "<meta property='og:description' content='Independent brand consultancy &amp; strategy.' />"
        "</head><body><meta property=\"og:site_name\" content=\"Body Name\"></body></html>"
    )

    identity = derive_brand_identity(
        brand_name="Saffron",
        brand_url="https://saffron-consultants.com",
        web_data=WebData(url="https://saffron-consultants.com", title="Home - Saffron", html=html),
    )

    assert identity.name == "Saffron Brand Consultants"
    assert identity.description == "Independent brand consultancy & strategy."
    assert "saffronbrandconsultants" in identity.strong_aliases


def test_description_collapses_whitespace_and_trims_at_a_word_boundary():
    words = " ".join(f"word{index}" for index in range(60))
    identity = derive_brand_identity(
        brand_name="Primary",
        brand_url="https://primary.studio",
        web_data=WebData(
            url="https://primary.studio",
            title="Primary",
            meta_description=f"  Brand\n\nsprints   for  {words}",
        ),
    )

    collapsed = f"Brand sprints for {words}"
    assert len(identity.description) <= 200
    assert collapsed.startswith(identity.description)
    assert collapsed[len(identity.description)] == " "


class _FakeAnalyzer:
    def __init__(self, response, *, api_key: str = "test-key", failure_reason: str | None = None):
        self.api_key = api_key
        self.response = response
        self.last_failure_reason = failure_reason
        self.calls: list[dict] = []

    def _call_json(self, system: str, user: str, max_tokens: int = 8000, **kwargs):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens, **kwargs})
        return self.response


_PRIMARY = BrandIdentity(
    name="Primary",
    domain="primary.studio",
    description="Brand Sprints",
    strong_aliases=("primarystudio",),
)
_CANDIDATES = [
    IdentityCandidate(url="https://primary.com/kids", title="Primary kids clothing", text="Soft basics for kids."),
    IdentityCandidate(
        url="https://www.itsnicethat.com/articles/primary-studio",
        title="Primary rethinks the brand sprint",
        text="The London studio Primary runs short brand sprints.",
    ),
]


def test_llm_identity_check_asks_one_deterministic_json_question_per_batch():
    analyzer = _FakeAnalyzer({"same_company": [1]})

    accepted = llm_identity_check(_PRIMARY, _CANDIDATES, llm=analyzer)

    assert accepted == {1}
    assert len(analyzer.calls) == 1
    call = analyzer.calls[0]
    assert call["temperature"] == 0
    assert call["timeout_seconds"] > 0
    assert call["json_schema"]["required"] == ["same_company"]
    prompt = call["user"]
    assert "Company: Primary (website primary.studio). Its website describes it as: Brand Sprints." in prompt
    assert "about THIS company" in prompt
    assert "its own social media accounts" in prompt
    assert "https://primary.com/kids" in prompt
    assert "The London studio Primary runs short brand sprints." in prompt


@pytest.mark.parametrize(
    ("analyzer", "reason"),
    [
        (_FakeAnalyzer({"same_company": [0]}, api_key=""), "llm_unavailable"),
        (_FakeAnalyzer({}, failure_reason="llm_timeout"), "llm_timeout"),
        (_FakeAnalyzer({"same_company": [2]}), "invalid_output"),
        (_FakeAnalyzer({"same_company": [True]}), "invalid_output"),
    ],
)
def test_llm_identity_check_raises_a_short_reason_code_on_failure(analyzer, reason):
    with pytest.raises(IdentityCheckError) as error:
        llm_identity_check(_PRIMARY, _CANDIDATES, llm=analyzer)

    assert error.value.reason == reason
    if reason == "llm_unavailable":
        assert analyzer.calls == []
