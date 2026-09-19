"""Versioned public API surface for B3S Scanner."""

import copy

from .errors import install_api_error_handlers
from .router import _BRAND_CATALOG_OPENAPI_SCHEMA_NAMES, router as api_router
from .service import brand_catalog_api_enabled


def install_scanner_api(app) -> None:
    install_api_error_handlers(app)
    app.include_router(api_router)
    original_openapi = app.openapi

    def openapi_with_brand_catalog_gate():
        spec = original_openapi()
        if brand_catalog_api_enabled():
            return spec
        filtered = copy.deepcopy(spec)
        filtered.get("paths", {}).pop("/api/v1/brands", None)
        schemas = filtered.get("components", {}).get("schemas", {})
        for name in _BRAND_CATALOG_OPENAPI_SCHEMA_NAMES:
            schemas.pop(name, None)
        return filtered

    app.openapi = openapi_with_brand_catalog_gate


__all__ = ["install_scanner_api"]
