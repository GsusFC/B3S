"""Strict canonical brand identity validation for Evidence Vault boundaries."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class EvidenceVaultBrandIdentityError(ValueError):
    """A brand identity is unsafe or ambiguous for a Vault boundary."""


def canonicalize_vault_brand(value: str, *, allow_url: bool = True) -> str:
    """Return one strict ASCII DNS authority or raise.

    The validator rejects credentials, ports, IPs, single-label hosts,
    wildcards, Unicode, and ambiguous punycode identities.
    """

    raw = str(value or "").strip()
    if not raw or not raw.isascii() or any(character.isspace() for character in raw):
        raise EvidenceVaultBrandIdentityError("invalid_brand_identity")
    if "://" in raw:
        if not allow_url:
            raise EvidenceVaultBrandIdentityError("brand_token_is_not_a_domain")
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError as exc:
            raise EvidenceVaultBrandIdentityError("invalid_brand_url") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or username is not None
            or password is not None
            or ":" in parsed.netloc
            or "[" in parsed.netloc
            or "]" in parsed.netloc
        ):
            raise EvidenceVaultBrandIdentityError("invalid_brand_url")
    else:
        if any(character in raw for character in "/?#@:"):
            raise EvidenceVaultBrandIdentityError("invalid_brand_domain")
        host = raw
    host = str(host).lower()
    if host.endswith(".."):
        raise EvidenceVaultBrandIdentityError("invalid_brand_domain")
    host = host.removesuffix(".")
    if host.startswith("www."):
        host = host[4:]
    if (
        not host
        or len(host) > 253
        or host.startswith("www.")
        or "*" in host
        or not host.isascii()
        or any(label.startswith("xn--") for label in host.split("."))
    ):
        raise EvidenceVaultBrandIdentityError("invalid_brand_domain")
    if all(
        label.isdigit() or re.fullmatch(r"0x[0-9a-f]+", label)
        for label in host.split(".")
    ):
        raise EvidenceVaultBrandIdentityError("ip_brand_identity_is_not_allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise EvidenceVaultBrandIdentityError("ip_brand_identity_is_not_allowed")
    labels = host.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise EvidenceVaultBrandIdentityError("invalid_brand_domain")
    return host


__all__ = ["EvidenceVaultBrandIdentityError", "canonicalize_vault_brand"]
