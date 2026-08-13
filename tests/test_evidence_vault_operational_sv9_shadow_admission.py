from __future__ import annotations

from src.history.repository import (
    _vault_operational_sv9_shadow_parent_is_admissible,
)


_PARENT = "a" * 64
_PRODUCED = "b" * 64
_NEXT = "c" * 64
_PACKET = "d" * 64
_SIBLING = "e" * 64
_EVENT = "11111111-1111-1111-1111-111111111111"
_NEXT_EVENT = "22222222-2222-2222-2222-222222222222"


def _packet(*, fingerprint: str = _PACKET) -> dict:
    return {
        "candidate_packet_fingerprint": fingerprint,
        "current_canonical_memory_version": _PARENT,
        "proposed_canonical_memory_version": _PRODUCED,
    }


def _current(*, version: str = _PRODUCED, event_id: str = _EVENT) -> dict:
    return {
        "canonical_memory_version": version,
        "adoption_event_id": event_id,
    }


def _event(
    *,
    fingerprint: str = _PACKET,
    parent: str = _PARENT,
    promoted: str = _PRODUCED,
    event_id: str = _EVENT,
) -> dict:
    return {
        "candidate_packet_fingerprint": fingerprint,
        "parent_canonical_memory_version": parent,
        "promoted_canonical_memory_version": promoted,
        "event_id": event_id,
    }


def _admissible(
    *,
    packet: dict | None = None,
    fingerprint: str = _PACKET,
    expected_parent: str | None = _PARENT,
    current: dict | None = None,
    event: dict | None = None,
) -> bool:
    return _vault_operational_sv9_shadow_parent_is_admissible(
        operational_packet=packet or _packet(),
        operational_packet_fingerprint=fingerprint,
        expected_parent_canonical_memory_version=expected_parent,
        current_memory=current,
        latest_adoption_event=event,
    )


def test_accepts_current_candidate_and_exact_direct_current_producer() -> None:
    current_parent = {
        "canonical_memory_version": _PARENT,
        "adoption_event_id": "00000000-0000-0000-0000-000000000000",
    }
    assert _admissible(current=current_parent)
    assert _admissible(
        packet=_packet(fingerprint=_SIBLING),
        fingerprint=_SIBLING,
        current=current_parent,
    )
    assert _admissible(current=_current(), event=_event())


def test_rejects_wrong_parent_sibling_and_event_field_mismatches() -> None:
    assert not _admissible(
        expected_parent=_PRODUCED,
        current=_current(),
        event=_event(),
    )
    assert not _admissible(
        packet=_packet(fingerprint=_SIBLING),
        fingerprint=_SIBLING,
        current=_current(),
        event=_event(),
    )
    assert not _admissible(
        current=_current(),
        event=_event(parent="f" * 64),
    )
    assert not _admissible(
        current=_current(),
        event=_event(promoted="f" * 64),
    )
    assert not _admissible(
        current=_current(event_id=_NEXT_EVENT),
        event=_event(),
    )
    assert not _admissible(
        packet={
            **_packet(),
            "proposed_canonical_memory_version": "f" * 64,
        },
        current=_current(),
        event=_event(),
    )


def test_rejects_former_producer_after_next_adoption() -> None:
    assert not _admissible(
        current=_current(version=_NEXT, event_id=_NEXT_EVENT),
        event=_event(
            fingerprint="f" * 64,
            parent=_PRODUCED,
            promoted=_NEXT,
            event_id=_NEXT_EVENT,
        ),
    )
