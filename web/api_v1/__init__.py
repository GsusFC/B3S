"""Versioned public API surface for B3S Scanner."""

from .errors import install_api_error_handlers
from .router import router as api_router


def install_scanner_api(app) -> None:
    install_api_error_handlers(app)
    app.include_router(api_router)


__all__ = ["install_scanner_api"]
