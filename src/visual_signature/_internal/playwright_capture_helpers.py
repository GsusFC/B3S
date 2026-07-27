"""Internal helpers re-exported for the Playwright capture runtime."""

from src.visual_signature._internal.playwright_capture_dismissal_rules import DISMISSAL_TARGET_SELECTOR
from src.visual_signature._internal.playwright_capture_helpers_impl import (
    _attempt_obstruction_dismissal,
    _attempt_obstruction_dismissal_with_discovery,
    _dismissal_successful,
    _discover_dismissal_targets,
    _prepare_perceptual_state_machine,
)

__all__ = [
    "DISMISSAL_TARGET_SELECTOR",
    "_attempt_obstruction_dismissal",
    "_attempt_obstruction_dismissal_with_discovery",
    "_dismissal_successful",
    "_discover_dismissal_targets",
    "_prepare_perceptual_state_machine",
]
