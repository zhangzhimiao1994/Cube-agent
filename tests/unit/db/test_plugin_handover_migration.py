import importlib.util
import re
from io import StringIO
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Table

from agent_hub.db.models import AdminResourceRow
from alembic import command

REPOSITORY = Path(__file__).resolve().parents[3]
CONSTRAINT = "ck_agent_hub_admin_resources_kind"
TABLE = "agent_hub_admin_resources"
EXISTING_KINDS = {
    "workflow",
    "agent",
    "main_agent",
    "skill",
    "skill_source",
    "skill_source_revision",
    "mcp",
    "memory",
    "hermes",
    "audit",
    "log",
    "setting",
    "channel",
    "openclaw",
    "openclaw_session",
    "schedule",
    "evolution",
    "plugin",
    "plugin_signing_key",
    "capability_install",
}


def _load_migration(filename: str) -> ModuleType:
    path = REPOSITORY / "alembic" / "versions" / filename
    if not path.is_file():
        pytest.fail(f"Required migration is missing: {filename}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        pytest.fail(f"Cannot load migration: {filename}")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migration() -> ModuleType:
    return _load_migration("0032_plugin_handover_admin_resources.py")


def _kinds(expression: str) -> set[str]:
    match = re.fullmatch(r"kind IN \((.*)\)", expression)
    assert match is not None, expression
    values = match.group(1).split(",")
    assert all(re.fullmatch(r"\s*'[a-z_]+'\s*", value) for value in values)
    return {value.strip().strip("'") for value in values}


def _config() -> Config:
    config = Config(str(REPOSITORY / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY / "alembic"))
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://offline@localhost/offline")
    config.output_buffer = StringIO()
    return config


def _record_operations(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: calls.append(("drop", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: calls.append(("create", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op, "execute", lambda statement: calls.append(("execute", str(statement)))
    )
    return calls


def _assert_guard(sql: str) -> None:
    normalized = " ".join(sql.split())
    assert normalized.startswith("DO ")
    lock = normalized.index(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    check = normalized.index("IF EXISTS")
    refusal = normalized.index("RAISE EXCEPTION")
    assert lock < check < refusal
    assert re.search(
        r"SELECT 1 FROM agent_hub_admin_resources WHERE kind = 'plugin_handover'", normalized
    )
    assert "plugin_handover" in normalized[refusal:]
    assert "END IF;" in normalized
    assert not re.search(r"\b(DELETE|UPDATE|TRUNCATE)\b", normalized, re.IGNORECASE)


def test_orm_check_allows_handover_and_preserves_every_existing_kind() -> None:
    table = AdminResourceRow.__table__
    assert isinstance(table, Table)
    constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == CONSTRAINT
    )
    assert _kinds(str(constraint.sqltext)) == EXISTING_KINDS | {"plugin_handover"}


def test_migration_extends_verified_question_search_head(migration: ModuleType) -> None:
    previous = _load_migration("0031_conversation_question_search.py")
    assert previous.revision == "0031_question_search"
    assert previous.down_revision == "0030_skill_source_revisions"
    assert migration.revision == "0032_plugin_handover"
    assert len(migration.revision) <= 32
    assert migration.down_revision == previous.revision
    scripts = ScriptDirectory.from_config(_config())
    assert migration.revision in {entry.revision for entry in scripts.walk_revisions()}
    assert scripts.get_revision(migration.revision).down_revision == previous.revision


def test_upgrade_adds_only_handover_without_mutating_resources(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_operations(migration, monkeypatch)
    migration.upgrade()
    assert len(calls) == 2
    assert calls[0] == ("drop", CONSTRAINT, TABLE, {"type_": "check"})
    assert calls[1][:3] == ("create", CONSTRAINT, TABLE)
    upgraded_kinds = _kinds(str(calls[1][3]))
    assert upgraded_kinds == EXISTING_KINDS | {"plugin_handover"}
    previous = _load_migration("0030_skill_source_revisions.py")
    assert upgraded_kinds - {"plugin_handover"} == _kinds(previous._NEXT_KINDS)
    table = AdminResourceRow.__table__
    assert isinstance(table, Table)
    constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == CONSTRAINT
    )
    assert upgraded_kinds == _kinds(str(constraint.sqltext))


def test_downgrade_guards_before_dropping_and_restores_all_previous_kinds(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_operations(migration, monkeypatch)
    migration.downgrade()
    assert len(calls) == 3
    assert calls[0][0] == "execute"
    _assert_guard(str(calls[0][1]))
    assert calls[1] == ("drop", CONSTRAINT, TABLE, {"type_": "check"})
    assert calls[2][:3] == ("create", CONSTRAINT, TABLE)
    assert _kinds(str(calls[2][3])) == EXISTING_KINDS


def test_downgrade_propagates_database_refusal_before_any_constraint_change(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_operations(migration, monkeypatch)

    # Simulate a PostgreSQL rejection at the execute boundary, not SQL evaluation.
    def refuse(statement: object) -> None:
        calls.append(("execute", str(statement)))
        raise RuntimeError("plugin_handover records exist")

    monkeypatch.setattr(migration.op, "execute", refuse)
    with pytest.raises(RuntimeError, match="plugin_handover records exist"):
        migration.downgrade()
    assert len(calls) == 1
    assert calls[0][0] == "execute"
    _assert_guard(str(calls[0][1]))


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_incremental_offline_sql_is_transactional_and_preserves_resources(
    migration: ModuleType, direction: str
) -> None:
    config = _config()
    if direction == "upgrade":
        command.upgrade(config, "0031_question_search:0032_plugin_handover", sql=True)
    else:
        command.downgrade(config, "0032_plugin_handover:0031_question_search", sql=True)
    output = config.output_buffer
    assert isinstance(output, StringIO)
    sql = output.getvalue()
    assert sql.strip().startswith("BEGIN;")
    assert sql.strip().endswith("COMMIT;")
    drop = f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT}"
    create = f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK ("
    assert sql.count(drop) == 1
    assert sql.count(create) == 1
    assert sql.index(drop) < sql.index(create)
    expression = sql.split(create, 1)[1].split(");", 1)[0]
    expected = EXISTING_KINDS | {"plugin_handover"} if direction == "upgrade" else EXISTING_KINDS
    assert _kinds(expression) == expected
    assert not re.search(
        r"\b(?:DELETE FROM|UPDATE|TRUNCATE(?: TABLE)?)\s+agent_hub_admin_resources\b",
        sql,
        re.IGNORECASE,
    )
    if direction == "downgrade":
        guard = sql[sql.index("DO ") : sql.index(drop)].strip().rstrip(";")
        _assert_guard(guard)
        assert sql.index("RAISE EXCEPTION") < sql.index(drop)
