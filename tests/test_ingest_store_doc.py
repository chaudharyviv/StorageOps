"""docs/STORE.md must describe the real DuckDB schema of the ingest store."""

import re
from pathlib import Path

import duckdb
import pytest

from ingest.report import FaultCode
from ingest.store import CLEAN_TABLE, QUARANTINE_TABLE, RUNS_TABLE, IngestStore

DOC = Path(__file__).resolve().parents[1] / "docs" / "STORE.md"
TABLES = (CLEAN_TABLE, QUARANTINE_TABLE, RUNS_TABLE)

Column = tuple[str, str, str]
Schema = dict[str, tuple[list[Column], list[str]]]


def documented() -> Schema:
    """Parse STORE.md: table -> (columns as (name, type, nullable), primary key columns)."""
    found: Schema = {}
    table = ""
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if heading := re.match(r"^## (\w+)$", line):
            table = heading.group(1)
            found[table] = ([], [])
        elif table and (row := re.match(r"^\| `(\w+)` \| ([A-Z ]+?) \| (YES|NO) \|", line)):
            found[table][0].append((row.group(1), row.group(2), row.group(3)))
        elif table and (key := re.match(r"^\*\*Primary key:\*\* (.+)$", line)):
            found[table][1].extend(re.findall(r"`(\w+)`", key.group(1)))
    return found


def actual(path: Path) -> Schema:
    """Read the same facts from a real store file."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        out = {}
        for table in TABLES:
            columns = con.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            keys = con.execute(
                "SELECT constraint_column_names FROM duckdb_constraints() "
                "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
                [table],
            ).fetchall()
            out[table] = (
                [(name, kind, nullable) for name, kind, nullable in columns],
                list(keys[0][0]) if keys else [],
            )
        return out
    finally:
        con.close()


@pytest.fixture(scope="module")
def real_schema(tmp_path_factory: pytest.TempPathFactory) -> Schema:
    path = tmp_path_factory.mktemp("store") / "store.duckdb"
    IngestStore(path).close()
    return actual(path)


def test_the_doc_lists_exactly_the_store_tables() -> None:
    assert set(documented()) == set(TABLES)


@pytest.mark.parametrize("table", TABLES)
def test_columns_types_and_nullability_match_the_real_schema(
    table: str, real_schema: Schema
) -> None:
    doc_columns, _ = documented()[table]
    real_columns, _ = real_schema[table]
    assert doc_columns == real_columns, (
        f"docs/STORE.md differs from the real {table} table; update the page (types in order)"
    )


@pytest.mark.parametrize("table", TABLES)
def test_primary_keys_match_the_real_schema(table: str, real_schema: Schema) -> None:
    assert documented()[table][1] == real_schema[table][1]
    assert real_schema[table][1], f"{table} has no primary key"


def test_the_checker_catches_a_difference(tmp_path: Path) -> None:
    path = tmp_path / "store.duckdb"
    IngestStore(path).close()
    con = duckdb.connect(str(path))
    con.execute("ALTER TABLE quarantine ADD COLUMN extra VARCHAR")
    con.close()
    assert actual(path)[QUARANTINE_TABLE][0] != documented()[QUARANTINE_TABLE][0]


def test_documented_fault_codes_are_all_the_codes_in_order() -> None:
    text = DOC.read_text(encoding="utf-8")
    block = re.search(r"Fault codes, in precedence order: (.+?)\.\n", text, re.S)
    assert block is not None
    assert re.findall(r"`(\w+)`", block.group(1)) == [c.value for c in FaultCode]
