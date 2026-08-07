from src.services.evidence_vault_field_replay import (
    has_libpq_connection_override_environment,
    is_local_postgres_dsn,
    postgres_database_name,
)


def test_field_shadow_accepts_only_exact_local_postgres_hosts() -> None:
    assert is_local_postgres_dsn("postgresql://postgres@localhost:5432/disposable") is True
    assert is_local_postgres_dsn("postgresql://postgres@127.0.0.1/disposable") is True
    assert is_local_postgres_dsn(
        "postgresql://postgres@/disposable?host=/private/tmp&port=55441"
    ) is True
    assert is_local_postgres_dsn("postgresql://postgres@localhost.evil.test/production") is False
    assert is_local_postgres_dsn(
        "postgresql://postgres@/production?host=/private/tmp-evil"
    ) is False
    assert is_local_postgres_dsn("postgresql://prod.example.com/production") is False
    assert is_local_postgres_dsn(
        "postgresql://localhost/disposable?host=prod.example.com"
    ) is False
    assert is_local_postgres_dsn(
        "postgresql://localhost/disposable?hostaddr=203.0.113.1"
    ) is False
    assert is_local_postgres_dsn(
        "postgresql://localhost/disposable?service=production"
    ) is False
    assert is_local_postgres_dsn("dbname=production host=localhost") is False


def test_field_shadow_extracts_exact_database_safety_token() -> None:
    assert postgres_database_name(
        "postgresql://postgres@/b3s_field_shadow?host=/private/tmp"
    ) == "b3s_field_shadow"
    assert postgres_database_name("dbname=production host=localhost") == ""


def test_field_shadow_rejects_ambient_libpq_routing_overrides() -> None:
    assert has_libpq_connection_override_environment({}) is False
    assert has_libpq_connection_override_environment(
        {"PGHOSTADDR": "203.0.113.1"}
    ) is True
    assert has_libpq_connection_override_environment(
        {"PGDATABASE": "production"}
    ) is True
