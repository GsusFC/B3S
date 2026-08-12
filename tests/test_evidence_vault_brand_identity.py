from __future__ import annotations

import pytest

from src.services.evidence_vault_brand_identity import (
    EvidenceVaultBrandIdentityError,
    canonicalize_vault_brand,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("EXAMPLE.com.", "example.com"),
        ("www.example.com", "example.com"),
        ("https://EXAMPLE.com./path?q=1", "example.com"),
    ],
)
def test_vault_brand_identity_is_canonical(value: str, expected: str) -> None:
    assert canonicalize_vault_brand(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "sub.example.com/path",
        "https://user@example.com",
        "https://example.com:443",
        "127.0.0.1",
        "127.1",
        "0177.0.0.1",
        "0x7f.1",
        "01.02.03.04",
        "xn--bcher-kva.example",
        "bücher.example",
        "Kexample.com",
        "https://Kexample.com",
        "https://[v1.example.com]",
        "https://[example.com",
        "https://example.com]",
        "https://[]",
        "https://example.com:",
        "https://example.com:/",
        "https://example.com:?query",
        "https://example.com:#fragment",
        "https://example.com／evil",
        ".example.com",
        "example.com..",
        "www.www.example.com",
        "example..com",
        "a",
        "a_b.com",
    ],
)
def test_vault_brand_identity_rejects_ambiguous_authorities(value: str) -> None:
    with pytest.raises(EvidenceVaultBrandIdentityError):
        canonicalize_vault_brand(value)


def test_vault_brand_identity_can_require_a_bare_domain() -> None:
    with pytest.raises(EvidenceVaultBrandIdentityError):
        canonicalize_vault_brand("https://example.com", allow_url=False)
