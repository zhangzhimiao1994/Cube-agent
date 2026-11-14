"""Execute the actual qualified migration on a local SQL database; no Root."""
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Never

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from agent_hub.db.models import RunAuthenticatedInputRow, RunRow
from agent_hub.runs import execution_clock_schema as schema
from alembic import op


def test_actual_0036_to_0037_upgrade_preserves_legacy_and_enforces_witness(tmp_path: Path) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///"+str(tmp_path/"migration.db"))
    old = sa.MetaData()
    # Pre-0037 relevant tables, with only PostgreSQL storage types adapted.
    for model in (RunRow, RunAuthenticatedInputRow):
        table = model.__table__
        assert isinstance(table, sa.Table)
        cols = [sa.Column(c.name, sa.JSON() if isinstance(c.type, JSONB)
                          else sa.String(32) if isinstance(c.type, PGUUID) else c.type,
                          nullable=c.nullable, primary_key=c.primary_key, server_default=c.server_default)
                for c in table.columns if c.name != "execution_clock_sha256"]
        unique = [sa.UniqueConstraint(*(c.name for c in item.columns), name=item.name)
                  for item in table.constraints if isinstance(item, sa.UniqueConstraint)
                  and "execution_clock_sha256" not in item.columns]
        sa.Table(table.name, old, *cols, *unique)
    sa.Table("alembic_version", old, sa.Column("version_num", sa.String(32), primary_key=True))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            old.create_all(conn)
            conn.execute(old.tables["alembic_version"].insert().values(version_num="0036_review_ledger"))
            conn.execute(old.tables["agent_hub_runs"].insert().values(
                id="1"*32, tenant_id="2"*32, actor_id="3"*32, actor_role="operator",
                request="legacy", mode="direct", status="queued", version=1))
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                schema.upgrade()
            conn.exec_driver_sql("UPDATE alembic_version SET version_num='0037_execution_clock'")
            assert "agent_hub_run_execution_clocks" in sa.inspect(conn).get_table_names()
            assert conn.exec_driver_sql("SELECT execution_clock_sha256 FROM agent_hub_runs").scalar() is None
            fks = sa.inspect(conn).get_foreign_keys("agent_hub_run_execution_clocks")
            assert any(fk["constrained_columns"] == ["run_id", "binding_sha256"] for fk in fks)
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                schema.downgrade()
            assert "agent_hub_run_execution_clocks" not in sa.inspect(conn).get_table_names()
            assert "execution_clock_sha256" not in {c["name"] for c in sa.inspect(conn).get_columns("agent_hub_runs")}
            assert conn.exec_driver_sql("SELECT count(*) FROM agent_hub_runs WHERE request='legacy'").scalar() == 1
    finally:
        engine.dispose()


def test_postgres_downgrade_locks_and_refuses_before_any_destructive_ddl() -> None:
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        schema.downgrade()
    sql = " ".join(output.getvalue().split())
    assert "LOCK TABLE agent_hub_runs IN ACCESS EXCLUSIVE MODE" in sql
    assert "LOCK TABLE agent_hub_run_execution_clocks IN ACCESS EXCLUSIVE MODE" in sql
    assert sql.index("LOCK TABLE agent_hub_runs") < sql.index("LOCK TABLE agent_hub_run_execution_clocks")
    assert sql.index("LOCK TABLE") < sql.index("IF EXISTS") < sql.index("RAISE EXCEPTION")
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP TABLE agent_hub_run_execution_clocks")
    assert "ERRCODE = 'check_violation'" in sql
    assert "execution_clock_sha256 IS NOT NULL" in sql



def test_postgres_refusal_propagates_without_drop_or_witness_change(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refused(RuntimeError):
        pass

    executed: list[str] = []

    def refuse(sql: object, **kwargs: object) -> Never:
        executed.append(str(sql))
        raise Refused("synthetic PostgreSQL check_violation")

    def destructive(*args: object, **kwargs: object) -> Never:
        pytest.fail("destructive DDL reached before a successful emptiness guard")

    monkeypatch.setattr(op, "get_bind", lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(op, "execute", refuse)
    monkeypatch.setattr(op, "drop_table", destructive)
    monkeypatch.setattr(op, "batch_alter_table", destructive)
    with pytest.raises(Refused):
        schema.downgrade()
    assert len(executed) == 1 and "LOCK TABLE" in executed[0] and "check_violation" in executed[0]
