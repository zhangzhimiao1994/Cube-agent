"""Local fixture guards only: no PG/Redis/provider or native mode execution."""

import os
from collections.abc import Callable
from inspect import unwrap
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    MetaData,
    Table,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete

from agent_hub.db.models import (
    RunAuthenticatedInputRow,
    RunReviewEffectRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.runtime.crew.adapter import (
    CrewAIObjectFactory,
    _active_crewai_scope,
    _contextual_crewai_storage_path,
)
from tests.integration.models.test_run_admission import _cleanup_owned_runs
from tests.integration.runtime.test_model_review_modes import owned_crewai_storage


@pytest.mark.parametrize("prior", [None, "existing-owned-crew-root"])
def test_native_crew_fixture_uses_writable_tmp_and_restores_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: str | None
) -> None:
    key = "AGENT_HUB_CREWAI_STORAGE_DIR"
    if prior is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, prior)
    fixture = cast(Callable[[Path, pytest.MonkeyPatch], None], unwrap(owned_crewai_storage))
    with (
        pytest.raises(RuntimeError, match="fixture-body-failure"),
        pytest.MonkeyPatch.context() as scoped,
    ):
        fixture(tmp_path, scoped)
        factory = CrewAIObjectFactory()
        assert factory._storage_dir == (tmp_path / "crewai").resolve()
        with _active_crewai_scope(factory._storage_dir):
            root = Path(_contextual_crewai_storage_path())
            assert root.is_relative_to(tmp_path.resolve())
            (root / "probe.txt").write_bytes(b"actual owned storage write")
        raise RuntimeError("fixture-body-failure")
    assert os.environ.get(key) == prior
    assert (tmp_path / "crewai" / "probe.txt").read_bytes() == b"actual owned storage write"


async def test_admission_cleanup_respects_review_fks_and_preserves_other_run() -> None:
    # Use actual model column types and relevant FK definitions; omit PG-only
    # checks/unused columns. This proves local SQL ordering, not PG acceptance.
    models = (
        (RunRow, ("id",)),
        (RunAuthenticatedInputRow, ("turn_id", "tenant_id", "run_id")),
        (RunReviewIntentRow, ("review_id", "run_id", "tenant_id", "authorization_turn_id")),
        (RunReviewEffectRow, ("review_id", "run_id", "tenant_id", "authorization_turn_id")),
        (RunReviewEventRow, ("sequence", "review_id", "run_id", "tenant_id")),
    )
    metadata = MetaData()
    tables: dict[type, Table] = {}
    for model, names in models:
        source = cast(Table, model.__table__)
        tables[model] = Table(
            source.name,
            metadata,
            *(
                Column(name, source.c[name].type, primary_key=source.c[name].primary_key)
                for name in names
            ),
        )
        for constraint in source.constraints:
            if isinstance(constraint, UniqueConstraint) and set(constraint.columns.keys()) <= set(
                names
            ):
                tables[model].append_constraint(UniqueConstraint(*constraint.columns.keys()))
    for model, names in models:
        source = cast(Table, model.__table__)
        for fk in source.foreign_key_constraints:
            if set(fk.column_keys) <= set(names):
                targets = [element.target_fullname for element in fk.elements]
                if all(target.rsplit(".", 1)[0] in metadata.tables for target in targets):
                    tables[model].append_constraint(
                        ForeignKeyConstraint(fk.column_keys, targets, ondelete=fk.ondelete)
                    )
    engine = create_engine("sqlite://")
    owned, other, tenant = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        metadata.create_all(connection)
        for sequence, run_id in enumerate((owned, other), 1):
            turn, review = uuid4(), uuid4()
            connection.execute(tables[RunRow].insert().values(id=run_id))
            connection.execute(
                tables[RunAuthenticatedInputRow]
                .insert()
                .values(turn_id=turn, tenant_id=tenant, run_id=run_id)
            )
            identity = {"review_id": review, "run_id": run_id, "tenant_id": tenant}
            for model in (RunReviewIntentRow, RunReviewEffectRow):
                connection.execute(
                    tables[model].insert().values(**identity, authorization_turn_id=turn)
                )
            connection.execute(
                tables[RunReviewEventRow].insert().values(sequence=sequence, **identity)
            )

        class LocalSQLSession:
            def __init__(self, current: Connection) -> None:
                self.connection = current
                self.executions = 0

            async def execute(self, statement: Delete) -> None:
                self.executions += 1
                self.connection.execute(statement)

        local = LocalSQLSession(connection)
        session = cast(AsyncSession, local)
        await _cleanup_owned_runs(session, ())
        assert local.executions == 0
        await _cleanup_owned_runs(session, (owned,))
        assert list(connection.scalars(select(tables[RunRow].c.id))) == [other]
        for model, _ in models[1:]:
            assert list(connection.scalars(select(tables[model].c.run_id))) == [other]
        # Repeating cleanup cannot touch the unrelated run or its evidence.
        await _cleanup_owned_runs(session, (owned,))
        for model, _ in models[1:]:
            assert list(connection.scalars(select(tables[model].c.run_id))) == [other]
    engine.dispose()
