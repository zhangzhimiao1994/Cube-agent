from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData, Table, UniqueConstraint
from sqlalchemy.dialects import postgresql

from agent_hub.db.models import RunAuthenticatedInputRow, RunInputAuthorizationRow, RunRow


def test_migration_and_model_constraints_have_parity(monkeypatch):
    path = Path(__file__).resolve().parents[3] / 'alembic/versions/0034_authenticated_inputs.py'
    assert path.exists(), 'authenticated originals require their own migration'
    spec = spec_from_file_location('authenticated_inputs_migration', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = MetaData()
    RunRow.__table__.to_metadata(metadata)
    class Operations:
        def create_table(self, name, *columns):
            return Table(name, metadata, *columns)
        def create_index(self, name, table_name, columns):
            from sqlalchemy import Index
            Index(name, *(metadata.tables[table_name].c[c] for c in columns))
    monkeypatch.setattr(module, 'op', Operations())
    module.upgrade()
    def constraints(table):
        result = set()
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
        assert constraints(migrated) == constraints(model.__table__)
        assert {(c.name, str(c.type.compile(dialect=postgresql.dialect())), c.nullable)
                for c in migrated.columns} == {
                    (c.name, str(c.type.compile(dialect=postgresql.dialect())), c.nullable)
                    for c in model.__table__.columns}
        assert {i.name for i in migrated.indexes} == {i.name for i in model.__table__.indexes}
