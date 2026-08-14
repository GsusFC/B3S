from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.history import repository as repository_module
from src.history.repository import PostgresHistoryRepository
from src.services.evidence_vault_operational_assessment_shadow import (
    EvidenceVaultOperationalAssessmentShadowError,
)


_PARENT = "a" * 64
_CURRENT = "b" * 64
_PACKET = "c" * 64
_EVENT = "11111111-1111-1111-1111-111111111111"


@dataclass
class _Result:
    rows: list

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, candidates: list[dict]) -> None:
        self.candidates = candidates
        self.queries: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        normalized = tuple(params) if params is not None else None
        self.queries.append((text, normalized))
        if "pg_advisory_xact_lock_shared" in text:
            return _Result([])
        if "SELECT version, filename, checksum" in text:
            return _Result([])
        if "JOIN LATERAL" in text:
            return _Result(self.candidates)
        if "packet_kind = 'operational_v2'" in text:
            return _Result([{"stored": "packet"}])
        raise AssertionError(text)


def _candidate(*, fingerprint: str = _PACKET) -> dict:
    return {
        "brand_id": "brand-id",
        "canonical_domain": "example.com",
        "adoption_event_id": _EVENT,
        "candidate_packet_fingerprint": fingerprint,
        "parent_canonical_memory_version": _PARENT,
        "promoted_canonical_memory_version": _CURRENT,
    }


def _event(*, fingerprint: str = _PACKET) -> dict:
    return {
        "event_id": _EVENT,
        "candidate_packet_fingerprint": fingerprint,
        "parent_canonical_memory_version": _PARENT,
        "promoted_canonical_memory_version": _CURRENT,
    }


def _current() -> dict:
    return {
        "canonical_memory_version": _CURRENT,
        "adoption_event_id": _EVENT,
    }


def _packet(*, fingerprint: str = _PACKET) -> dict:
    del fingerprint
    return {
        "current_canonical_memory_version": _PARENT,
        "proposed_canonical_memory_version": _CURRENT,
    }


def _repository(monkeypatch, connection: _Connection, *, event=None, packet=None):
    monkeypatch.setattr(repository_module, "_migration_manifest", lambda: ())
    monkeypatch.setattr(
        repository_module,
        "_require_exact_migration_manifest",
        lambda _manifest, _rows: None,
    )
    monkeypatch.setattr(
        repository_module,
        "_project_vault_operational_memory_authority_chain",
        lambda _conn, _brand_id: [(_current(), event or _event())],
    )
    monkeypatch.setattr(
        repository_module,
        "_vault_operational_packet_record",
        lambda _row: {"packet": packet or _packet()},
    )
    return PostgresHistoryRepository(
        "postgresql://unused",
        connect=lambda *_args, **_kwargs: connection,
        schema_policy="verify_head",
    )


def test_discovery_returns_only_bounded_identity_for_latest_direct_producer(
    monkeypatch,
) -> None:
    connection = _Connection([_candidate()])
    repository = _repository(monkeypatch, connection)

    assert repository.discover_evidence_vault_operational_sv9_shadow_work_items(
        limit=7,
    ) == [
        {
            "domain": "example.com",
            "operational_packet_fingerprint": _PACKET,
            "expected_parent_canonical_memory_version": _PARENT,
        }
    ]

    discovery_query, params = next(
        (query, params)
        for query, params in connection.queries
        if "JOIN LATERAL" in query
    )
    assert "ORDER BY events.sequence DESC" in discovery_query
    assert "NOT EXISTS" in discovery_query
    assert "ORDER BY brands.canonical_domain, brands.id" in discovery_query
    assert params == ("b3s", 7)
    statements = [query for query, _params in connection.queries]
    assert statements.index("SELECT pg_advisory_xact_lock_shared(%s)") < statements.index(
        "SELECT version, filename, checksum FROM b3s_history.schema_migrations "
        "ORDER BY version"
    ) < statements.index(discovery_query)


def test_discovery_skips_already_ledgered_or_non_latest_rows_in_sql(monkeypatch) -> None:
    connection = _Connection([])
    repository = _repository(monkeypatch, connection)

    assert repository.discover_evidence_vault_operational_sv9_shadow_work_items() == []
    assert not any(
        "packet_kind = 'operational_v2'" in query
        for query, _params in connection.queries
    )


@pytest.mark.parametrize("limit", [0, 101, True, 1.0, "1"])
def test_discovery_rejects_unbounded_or_ambiguous_limit_before_connect(
    monkeypatch,
    limit,
) -> None:
    monkeypatch.setattr(
        repository_module,
        "_migration_manifest",
        lambda: pytest.fail("database must not be opened"),
    )
    repository = PostgresHistoryRepository(
        "postgresql://unused",
        connect=lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
        schema_policy="verify_head",
    )

    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="work-item discovery limit",
    ):
        repository.discover_evidence_vault_operational_sv9_shadow_work_items(
            limit=limit,
        )


def test_discovery_rejects_invalid_workspace_before_connect() -> None:
    repository = PostgresHistoryRepository(
        "postgresql://unused",
        connect=lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
        schema_policy="verify_head",
    )

    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="workspace identity",
    ):
        repository.discover_evidence_vault_operational_sv9_shadow_work_items(
            workspace_slug=" b3s",
        )


def test_discovery_fails_closed_when_sql_candidate_is_not_latest_event(
    monkeypatch,
) -> None:
    connection = _Connection([_candidate(fingerprint="d" * 64)])
    repository = _repository(monkeypatch, connection)

    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="adoption changed during discovery",
    ):
        repository.discover_evidence_vault_operational_sv9_shadow_work_items()


def test_discovery_fails_closed_when_latest_packet_is_not_direct_current_producer(
    monkeypatch,
) -> None:
    connection = _Connection([_candidate()])
    wrong_packet = {
        "current_canonical_memory_version": _PARENT,
        "proposed_canonical_memory_version": "d" * 64,
    }
    repository = _repository(monkeypatch, connection, packet=wrong_packet)

    with pytest.raises(
        EvidenceVaultOperationalAssessmentShadowError,
        match="did not directly produce current",
    ):
        repository.discover_evidence_vault_operational_sv9_shadow_work_items()
