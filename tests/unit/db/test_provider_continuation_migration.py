from __future__ import annotations

import io
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from agent_hub.db.models import ProviderContinuationRow
from alembic import command

ROOT = Path(__file__).resolve().parents[3]
TABLE = "agent_hub_provider_continuations"


def config() -> Config:
    result = Config(str(ROOT / "alembic.ini"))
    result.set_main_option("script_location", str(ROOT / "alembic"))
    result.set_main_option("sqlalchemy.url", "postgresql+asyncpg://offline@localhost/offline")
    result.output_buffer = io.StringIO()
    return result


def migration() -> ModuleType:
    path = ROOT / "alembic/versions/0033_provider_continuations.py"
    spec = spec_from_file_location("private_snapshot_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_table_has_global_immutable_id_and_scoped_ledger_unique() -> None:
    table = ProviderContinuationRow.__table__
    assert isinstance(table, Table)
    assert table.name == TABLE and set(table.columns.keys()) == {
        "id", "tenant_id", "run_id", "ledger_key", "sha256", "payload", "byte_size",
    }
    assert list(table.primary_key.columns.keys()) == ["id"]
    assert all(not column.nullable for column in table.columns)
    assert any(isinstance(c, UniqueConstraint) and list(c.columns.keys()) == [
        "tenant_id", "run_id", "ledger_key",
    ] for c in table.constraints)
    checks = {str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "octet_length(payload) = byte_size" in checks
    assert "byte_size > 0 AND byte_size <= 1048576" in checks
    assert "sha256 ~ '^[0-9a-f]{64}$'" in checks
    assert "agent_hub_runs.id" in {fk.target_fullname for fk in table.foreign_keys}


def test_single_head_extends_0032_without_altering_old_revisions() -> None:
    current = migration()
    assert current.revision == "0033_provider_continuations"
    assert current.down_revision == "0032_plugin_handover" and len(current.revision) <= 32
    scripts = ScriptDirectory.from_config(config())
    assert scripts.get_heads() == [current.revision]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_migration_sql_is_transactional_private_and_scoped(direction: str) -> None:
    settings = config()
    if direction == "upgrade":
        command.upgrade(settings, "0032_plugin_handover:0033_provider_continuations", sql=True)
    else:
        command.downgrade(settings, "0033_provider_continuations:0032_plugin_handover", sql=True)
    assert isinstance(settings.output_buffer, io.StringIO)
    sql = settings.output_buffer.getvalue()
    assert sql.strip().startswith("BEGIN;") and sql.strip().endswith("COMMIT;")
    assert not any(name in sql for name in ("agent_hub_runtime_artifacts", "agent_hub_run_checkpoints",
                                          "rejected_outputs", "agent_hub_run_events"))
    if direction == "upgrade":
        assert sql.count(f"CREATE TABLE {TABLE}") == 1
        table = ProviderContinuationRow.__table__
        assert isinstance(table, Table)
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                assert str(constraint.sqltext) in sql
        assert "UNIQUE (tenant_id, run_id, ledger_key)" in sql
    else:
        assert sql.index("LOCK TABLE") < sql.index("IF EXISTS") < sql.index("RAISE EXCEPTION")
        assert sql.index("RAISE EXCEPTION") < sql.index(f"DROP TABLE {TABLE}")
        assert "DELETE FROM" not in sql and "TRUNCATE" not in sql


def test_downgrade_refusal_does_not_drop_recovery_data(monkeypatch: pytest.MonkeyPatch) -> None:
    module = migration()
    calls = []

    def refuse(statement: object) -> None:
        calls.append(str(statement))
        raise RuntimeError("private recovery data exists")

    monkeypatch.setattr(module.op, "execute", refuse)
    monkeypatch.setattr(module.op, "drop_table", lambda *_a, **_kw: pytest.fail("data loss"))
    monkeypatch.setattr(module.op, "drop_index", lambda *_a, **_kw: pytest.fail("index loss"))
    with pytest.raises(RuntimeError):
        module.downgrade()
    assert len(calls) == 1
