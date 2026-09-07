"""Verify an existing PostgreSQL schema before explicitly stamping the baseline.

This reads database metadata only. It never writes rows, runs Alembic commands, or
stamps a revision. After a successful verification, an operator may explicitly run:

    alembic -c ../alembic.ini stamp 20260718_0001
"""
from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine, Inspector

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from migrations.baseline_contract import baseline_schema_description


SchemaDescription = dict[str, dict[str, Any]]
IndexDescription = tuple[str, tuple[str, ...], bool, str | None]


def _normalised_type(type_: Any) -> str:
    return " ".join(type_.compile(dialect=postgresql.dialect()).lower().split())


def _normalised_predicate(predicate: Any) -> str | None:
    if predicate is None:
        return None
    if hasattr(predicate, "compile"):
        predicate = predicate.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    value = str(predicate).lower()
    value = re.sub(r"\$\$([^$]*)\$\$", r"'\1'", value)
    value = re.sub(r"::(?:character varying|text)", "", value)
    value = re.sub(r"\(\s*([a-z_]\w*)\s*\)", r"\1", value)
    value = re.sub(r"\(\s*([^()]+?)\s*\)", r"\1", value)
    return " ".join(value.split())


def describe_metadata(metadata: MetaData) -> SchemaDescription:
    """Describe only schema structure, never application data."""
    description: SchemaDescription = {}
    for table in sorted(metadata.tables.values(), key=lambda item: item.name):
        foreign_keys = sorted(
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].column.table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.elements[0].ondelete or "").lower(),
            )
            for constraint in table.foreign_key_constraints
        )
        unique_constraints = sorted(
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        )
        indexes = sorted(
            (
                index.name,
                tuple(index.columns.keys()),
                bool(index.unique),
                _normalised_predicate(
                    index.dialect_options["postgresql"].get("where")
                ),
            )
            for index in table.indexes
        )
        description[table.name] = {
            "columns": {
                column.name: {
                    "type": _normalised_type(column.type),
                    "nullable": column.nullable,
                }
                for column in table.columns
            },
            "primary_key": tuple(table.primary_key.columns.keys()),
            "foreign_keys": tuple(foreign_keys),
            "unique_constraints": tuple(unique_constraints),
            "indexes": tuple(indexes),
        }
    return description


def describe_database(inspector: Inspector, table_names: set[str]) -> SchemaDescription:
    """Describe required database tables through SQLAlchemy's PostgreSQL inspector."""
    description: SchemaDescription = {}
    available_tables = set(inspector.get_table_names())
    for table_name in sorted(table_names & available_tables):
        foreign_keys = sorted(
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (foreign_key.get("options", {}).get("ondelete") or "").lower(),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        )
        unique_constraints = sorted(
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name)
        )
        indexes = sorted(
            (
                index["name"],
                tuple(index["column_names"]),
                bool(index.get("unique", False)),
                _normalised_predicate(
                    (index.get("dialect_options") or {}).get("postgresql_where")
                    or index.get("postgresql_where")
                ),
            )
            for index in inspector.get_indexes(table_name)
        )
        description[table_name] = {
            "columns": {
                column["name"]: {
                    "type": _normalised_type(column["type"]),
                    "nullable": column["nullable"],
                }
                for column in inspector.get_columns(table_name)
            },
            "primary_key": tuple(
                inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
            ),
            "foreign_keys": tuple(foreign_keys),
            "unique_constraints": tuple(unique_constraints),
            "indexes": tuple(indexes),
        }
    return description


def _index_diff(
    table_name: str,
    expected: tuple[IndexDescription, ...],
    actual: tuple[IndexDescription, ...],
) -> list[str]:
    diff: list[str] = []
    expected_by_name = {index[0]: index for index in expected}
    actual_by_name = {index[0]: index for index in actual}

    for index_name in sorted(set(expected_by_name) - set(actual_by_name)):
        diff.append(f"{table_name}.indexes.{index_name}: missing index")
    for index_name in sorted(set(actual_by_name) - set(expected_by_name)):
        diff.append(f"{table_name}.indexes.{index_name}: unexpected index")

    for index_name in sorted(set(expected_by_name) & set(actual_by_name)):
        expected_index = expected_by_name[index_name]
        actual_index = actual_by_name[index_name]
        for offset, property_name in (
            (1, "columns"),
            (2, "unique"),
            (3, "predicate"),
        ):
            if expected_index[offset] != actual_index[offset]:
                diff.append(
                    f"{table_name}.indexes.{index_name}.{property_name}: "
                    f"expected {expected_index[offset]!r}, "
                    f"found {actual_index[offset]!r}"
                )
    return diff


def schema_diff(expected: Mapping[str, Mapping[str, Any]], actual: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return a safe structural diff with no application row values."""
    diff: list[str] = []
    for table_name in sorted(set(expected) - set(actual)):
        diff.append(f"{table_name}: missing table")
    for table_name in sorted(set(expected) & set(actual)):
        expected_table = expected[table_name]
        actual_table = actual[table_name]
        expected_columns = expected_table["columns"]
        actual_columns = actual_table["columns"]
        for column_name in sorted(set(expected_columns) - set(actual_columns)):
            diff.append(f"{table_name}.columns.{column_name}: missing column")
        for column_name in sorted(set(actual_columns) - set(expected_columns)):
            diff.append(f"{table_name}.columns.{column_name}: unexpected column")
        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            for property_name in ("type", "nullable"):
                expected_value = expected_columns[column_name][property_name]
                actual_value = actual_columns[column_name][property_name]
                if expected_value != actual_value:
                    diff.append(
                        f"{table_name}.columns.{column_name}.{property_name}: "
                        f"expected {expected_value}, found {actual_value}"
                    )

        for property_name in ("primary_key", "foreign_keys", "unique_constraints"):
            expected_value = expected_table[property_name]
            actual_value = actual_table[property_name]
            if expected_value != actual_value:
                diff.append(
                    f"{table_name}.{property_name}: "
                    f"expected {expected_value!r}, found {actual_value!r}"
                )

        expected_indexes = tuple(expected_table["indexes"])
        actual_indexes = tuple(actual_table["indexes"])
        if expected_indexes != actual_indexes:
            diff.extend(_index_diff(table_name, expected_indexes, actual_indexes))
    return diff


def expected_schema_description() -> SchemaDescription:
    """Return the immutable 20260718_0001 contract, not live model metadata."""
    return baseline_schema_description()


def verify_schema(engine: Engine) -> list[str]:
    expected = expected_schema_description()
    actual = describe_database(inspect(engine), set(expected))
    return schema_diff(expected, actual)


def main() -> int:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        diff = verify_schema(engine)
    finally:
        engine.dispose()
    if diff:
        print("Schema baseline verification failed:")
        for line in diff:
            print(f"- {line}")
        return 1
    print("Schema baseline verification passed.")
    print("Run 'alembic -c ../alembic.ini stamp 20260718_0001' separately to adopt it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
