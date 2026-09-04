from __future__ import annotations
import os
import pytest
from src.services.evidence_vault_scan_orchestration import prepare_vault_scan_after_capture
def _validated_test_dsn(dsn: str | None = None) -> str:
    import psycopg
    raw_dsn = dsn if dsn is not None else os.environ.get("B3S_TEST_DATABASE_URL")
    if not raw_dsn:
        raise RuntimeError("B3S_TEST_DATABASE_URL is required")
    try:
        params = psycopg.conninfo.conninfo_to_dict(raw_dsn)
    except Exception:
        raise RuntimeError("invalid disposable PostgreSQL test DSN") from None
    host = params.get("host")
    database = params.get("dbname")
    if any(key in params for key in ("hostaddr", "service", "servicefile")):
        raise RuntimeError("disposable PostgreSQL test DSN forbids hostaddr/service indirection")
    test_database = isinstance(database, str) and (database == "b3s_test" or (
        database.startswith("b3s_test_") and len(database) > len("b3s_test_")
    ))
    if host not in {"127.0.0.1", "::1"}:
        raise RuntimeError("disposable PostgreSQL test DSN requires literal loopback host")
    if not test_database:
        raise RuntimeError("disposable PostgreSQL test DSN requires test-only database")
    port_value = params.get("port")
    if not isinstance(port_value, str) or not port_value.isascii() or not port_value.isdecimal() or not 1 <= int(port_value) <= 65535:
        raise RuntimeError("disposable PostgreSQL test DSN requires explicit decimal port 1..65535")
    port = str(int(port_value))
    return psycopg.conninfo.make_conninfo(**{
        key: params.get("host") if key == "hostaddr" else port if key == "port" else params[key]
        for key in ("host", "hostaddr", "dbname", "port", "user", "password")
        if key == "hostaddr" or key in params
    })
def _reset_repository(dsn: str | None = None):
    dsn = _validated_test_dsn(dsn)
    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        raise RuntimeError("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    import psycopg
    from src.history.repository import PostgresHistoryRepository
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    return repository
def test_production_shaped_capture_preparation_persists_and_reads_back() -> None:
    if not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.skip("B3S_TEST_DATABASE_URL and B3S_TEST_ALLOW_SCHEMA_DROP=1 are required")
    repository = _reset_repository()
    scan_id = "vault-preparation-postgres-baseline"
    url = "https://example.com"
    snapshot = {
        "run": {"id": 42, "brand_name": "Example", "url": url},
        "raw_inputs": [{"source": "web", "payload": {"url": "https://example.com/about", "summary": "We help teams ship better products."}}],
        "acquisition_steps": {"web": {"status": "ok", "details": {"reason": "captured"}}},
        "features": [],
        "acquisition_gate": {"state": "pass", "issues": [], "warnings": []},
    }
    prepared = prepare_vault_scan_after_capture(
        repository=repository,
        snapshot=snapshot,
        scan_id=scan_id,
        url=url,
        brand_name="Example",
        environment="vault",
        incremental_enabled=True,
        observed_at="2026-08-06T11:00:00Z",
    )
    plan = prepared["operation_plan"]
    assert prepared["capture_persisted"] is True and prepared["mode"] == plan["mode"] == "baseline" and plan["authority"] is False
    operation = repository.get_capture_operation_plan(scan_id)
    assert operation is not None and operation["source_scan_id"] == scan_id
    assert operation["plan"]["operation_plan_fingerprint"] == plan["operation_plan_fingerprint"] and operation["raw_observation"]["source_scan_id"] == scan_id
    observations = repository.list_capture_observations_for_domain(url)
    assert len(observations) == 1
    readback = observations[0]
    assert readback["source_scan_id"] == scan_id and readback["raw_observation"]["source_scan_id"] == scan_id
    assert readback["metadata"]["operation_plan"]["operation_plan_fingerprint"] == plan["operation_plan_fingerprint"]
def test_validated_dsn_pins_hostaddr_against_environment(monkeypatch) -> None:
    import psycopg
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.7")
    monkeypatch.setenv("PGPORT", "6543")
    parsed = psycopg.conninfo.conninfo_to_dict(_validated_test_dsn("host=127.0.0.1 port=55432 dbname=b3s_test"))
    assert parsed["host"] == parsed["hostaddr"] == "127.0.0.1" and parsed["port"] == "55432"
@pytest.mark.parametrize(("dsn", "reason"), (("postgresql://scanner@example.com/b3s_test", "loopback"), ("postgresql://scanner@127.0.0.1/production", "test-only"), ("host=127.0.0.1 hostaddr=203.0.113.9 dbname=b3s_test", "indirection"), ("host=127.0.0.1 service=disposable dbname=b3s_test", "indirection"), ("host=127.0.0.1 dbname=b3s_test", "port"), ("host=127.0.0.1 port=0 dbname=b3s_test", "port"), ("host=127.0.0.1 port=5432,55432 dbname=b3s_test", "port")))
def test_disposable_dsn_guard_rejects_before_connect_or_drop(monkeypatch, dsn: str, reason: str) -> None:
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: pytest.fail("unsafe DSN reached connect"))
    with pytest.raises(RuntimeError, match=reason):
        _reset_repository(dsn)


def test_real_vercel_capture_replays_from_frozen_postgres_boundary() -> None:
    """Real captured bytes, real capture/operation tables, no external calls."""
    import json
    from pathlib import Path

    from src.history.report_parser import canonical_json_hash
    from src.services.evidence_vault_scan_orchestration import prepare_vault_exact_resume

    if not os.environ.get("B3S_TEST_DATABASE_URL") or os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.skip("requires the existing disposable PostgreSQL service")
    repository = _reset_repository()
    envelope = json.loads((Path(__file__).parents[1] / "fixtures/vercel/vercel_fresh_capture_envelope.json").read_text())
    snapshot = envelope["snapshot"]
    scan = "stabilization-vercel-frozen-capture"
    prepared = prepare_vault_scan_after_capture(
        repository=repository, snapshot=snapshot, scan_id=scan,
        url=snapshot["run"]["url"], brand_name=snapshot["run"]["brand_name"],
        environment="vault", incremental_enabled=True,
        observed_at="2026-09-04T12:00:00Z",
    )
    operation = repository.get_capture_operation_plan(scan)
    assert prepared["capture_persisted"] is True
    assert len(operation["raw_observation"]["evidence_records"]) > 0
    request = {"operation": "exact_resume"}
    action = {
        "action_id": "stabilization-vercel-action", "scan_id": scan, "state": "running",
        "request_payload": request, "status_payload": {"state": "running", "phase": "running"},
        "request_fingerprint": canonical_json_hash(request),
    }
    first = prepare_vault_exact_resume(repository=repository, action=action, scan_id=scan)
    second = prepare_vault_exact_resume(repository=repository, action=action, scan_id=scan)
    assert first == second
    assert first["report_binding"]["observation_hash"] == operation["observation_hash"]
    assert first["report_binding"]["capture_hash"] == operation["capture_hash"]
    assert first["canonical_snapshot"] == operation["raw_observation"]["capture_payload"]
    assert first["preparation"]["operation_plan"] == operation["plan"]
    assert repository.get_capture_operation_plan(scan) == operation
