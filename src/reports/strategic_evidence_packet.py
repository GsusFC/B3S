"""Strategic evidence packet derived from Brand Audit snapshots.

Brand Audit owns data collection. This module turns a persisted run snapshot into
small, named evidence groups that downstream interpreters can reuse without
reading raw scraper text or internal feature metadata.
"""

from __future__ import annotations

from typing import Any

from src.reports.strategic_evidence_packet_builder import build_packet
from src.reports.strategic_evidence_packet_helpers import NOISE_MARKERS
from src.reports.strategic_evidence_packet_models import (
    StrategicEvidenceLine,
    StrategicEvidencePacket,
)


def build_strategic_evidence_packet(snapshot: dict[str, Any]) -> StrategicEvidencePacket:
    return build_packet(snapshot)
