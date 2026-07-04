from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse, urlunparse

MAX_URL_LENGTH = 2048


def validate_url(raw: str) -> tuple[bool, str]:
    value = (raw or "").strip()
    if not value:
        return False, "URL is required"
    if len(value) > MAX_URL_LENGTH:
        return False, f"URL is longer than {MAX_URL_LENGTH} characters"
    if not value.lower().startswith(("http://", "https://")):
        value = f"https://{value}"

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return False, "URL must use http or https"

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False, "URL must include a host"
    if hostname == "localhost":
        return False, "localhost URLs are not allowed"

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        return False, "private or local IP URLs are not allowed"
    if ip is None and "." not in hostname:
        return False, "host must include a dot"

    blocked = _blocked_domains()
    if any(hostname == domain or hostname.endswith(f".{domain}") for domain in blocked):
        return False, "host is on the blocklist"

    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    normalized = urlunparse((scheme, netloc, parsed.path.rstrip("/") if parsed.path != "/" else "", "", parsed.query, ""))
    return True, normalized


def _blocked_domains() -> set[str]:
    raw = os.environ.get("BRAND3_BLOCKED_DOMAINS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}
