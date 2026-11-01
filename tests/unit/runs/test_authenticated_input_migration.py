from collections.abc import Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    MetaData,
    Table,
    UniqueConstraint,
    create_mock_engine,
)
from sqlalchemy.sql.schema import SchemaItem

from agent_hub.db.models import RunAuthenticatedInputRow, RunInputAuthorizationRow, RunRow


@runtime_checkable
class Migration(Protocol):
    def upgrade(self) -> None: ...


type Constraint = tuple[str, object, object] | tuple[str, tuple[str, ...], tuple[str, ...], str | None]


def test_migration_and_model_constraints_have_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_sql(*args: object, **kwargs: object) -> None:
        raise AssertionError('migration parity only compiles metadata; no SQL execution')
    dialect = create_mock_engine('postgresql://', unexpected_sql).dialect
    path = Path(__file__).resolve().parents[3] / 'alembic/versions/0034_authenticated_inputs.py'
    assert path.exists(), 'authenticated originals require their own migration'
    spec = spec_from_file_location('authenticated_inputs_migration', path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = MetaData()
    run_table = RunRow.__table__
    assert isinstance(run_table, Table)
    run_table.to_metadata(metadata)
    class Operations:
        def create_table(self, name: str, *columns: SchemaItem) -> Table:
            return Table(name, metadata, *columns)
        def create_index(self, name: str, table_name: str, columns: Sequence[str]) -> None:
            from sqlalchemy import Index
            Index(name, *(metadata.tables[table_name].c[c] for c in columns))
    monkeypatch.setattr(module, 'op', Operations())
    assert isinstance(module, Migration)
    module.upgrade()
    def constraints(table: Table) -> set[Constraint]:
        result: set[Constraint] = set()
        for c in table.constraints:
            if isinstance(c, CheckConstraint):
                result.add(('check', c.name, str(c.sqltext)))
            elif isinstance(c, ForeignKeyConstraint):
                result.add(('fk', tuple(c.column_keys), tuple(e.target_fullname for e in c.elements),
                            c.ondelete))
            elif isinstance(c, UniqueConstraint):
                result.add(('unique', c.name, tuple(c.columns.keys())))
        return result
    for model in (RunAuthenticatedInputRow, RunInputAuthorizationRow):
        migrated = metadata.tables[model.__tablename__]
        model_table = model.__table__
        assert isinstance(model_table, Table)
        assert constraints(migrated) == constraints(model_table)
        assert {(c.name, str(c.type.compile(dialect=dialect)), c.nullable)
                for c in migrated.columns} == {
                    (c.name, str(c.type.compile(dialect=dialect)), c.nullable)
                    for c in model_table.columns}
        assert {i.name for i in migrated.indexes} == {i.name for i in model_table.indexes}
