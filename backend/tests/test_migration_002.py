"""Regression tests for the 002_historical_data SQL splitter and dedup guard.

Background
----------
The historical-sync migration is the only SQL file we re-run on every
bot startup (see :func:`HistoricalSync._run_migration`). Two foot-guns
shipped in the original implementation:

1.  The dedup ``DELETE FROM ob_snapshots a USING ob_snapshots b`` ran
    unconditionally even after the unique index it was preparing for
    already existed. On a 200k+ row chunk that self-join took 40+ s and
    starved the connection pool on every restart.
2.  The SQL splitter was ``sql.split(";\\n")`` which tears PL/pgSQL
    ``DO $$...$$;`` blocks in half (the body has its own ``;\\n``).

These tests pin the contract for both fixes so a future refactor that
re-introduces either issue will fail in CI before it deploys.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from data.historical_sync import _split_sql_statements


# ─── Splitter: regression cases ──────────────────────────────────────


def test_split_sql_basic_three_statements():
    sql = "SELECT 1;\nSELECT 2;\nSELECT 3;\n"
    assert _split_sql_statements(sql) == ["SELECT 1", "SELECT 2", "SELECT 3"]


def test_split_sql_strips_line_comments():
    sql = """
    -- header comment
    CREATE TABLE foo (x INT);
    -- trailing comment
    """
    assert _split_sql_statements(sql) == ["CREATE TABLE foo (x INT)"]


def test_split_sql_handles_dollar_quoted_block():
    """A DO block must survive intact even though its body contains
    ``;`` and newlines that would fool the legacy splitter."""
    sql = """
    DO $do$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='x') THEN
            DELETE FROM tab WHERE 1=1;
        END IF;
    END
    $do$;
    SELECT 'after';
    """
    statements = _split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("DO $do$")
    assert "END IF" in statements[0]
    assert statements[0].endswith("$do$")
    assert statements[1] == "SELECT 'after'"


def test_split_sql_handles_anonymous_dollar_quotes():
    sql = "DO $$BEGIN PERFORM 1; END$$;\nSELECT 2;"
    statements = _split_sql_statements(sql)
    assert statements == ["DO $$BEGIN PERFORM 1; END$$", "SELECT 2"]


def test_split_sql_keeps_semicolons_inside_string_literals():
    sql = "INSERT INTO t(x) VALUES ('a;b;c');\nSELECT 1;"
    statements = _split_sql_statements(sql)
    assert statements == ["INSERT INTO t(x) VALUES ('a;b;c')", "SELECT 1"]


def test_split_sql_handles_escaped_quotes():
    sql = "SELECT 'it''s a test';\nSELECT 2;"
    statements = _split_sql_statements(sql)
    assert statements == ["SELECT 'it''s a test'", "SELECT 2"]


def test_split_sql_handles_block_comment():
    sql = "SELECT 1 /* hello;\nworld; */;\nSELECT 2;"
    statements = _split_sql_statements(sql)
    assert statements[0].startswith("SELECT 1")
    assert statements[1] == "SELECT 2"


def test_split_sql_drops_pure_comment_chunks():
    sql = """
    -- only comments here

    SELECT 1;
    -- trailing only
    """
    assert _split_sql_statements(sql) == ["SELECT 1"]


# ─── Migration content: contract ─────────────────────────────────────


def _read_migration_002() -> str:
    here = Path(__file__).resolve().parent
    sql_path = here.parent / "migrations" / "002_historical_data.sql"
    return sql_path.read_text()


def test_migration_002_dedup_is_guarded_by_unique_index():
    """The expensive self-join DELETE must only execute when the
    unique index it depends on is missing -- otherwise restarts pay
    O(N^2) cost on every boot for no reason."""
    sql = _read_migration_002()
    assert "DELETE FROM ob_snapshots" in sql, "dedup statement still present"
    statements = _split_sql_statements(sql)

    delete_chunks = [s for s in statements if "DELETE FROM ob_snapshots" in s]
    assert delete_chunks, "expected the dedup statement to survive splitting"

    for chunk in delete_chunks:
        assert chunk.lstrip().upper().startswith("DO"), (
            "dedup must be wrapped in a DO block so we can guard it"
        )
        upper = chunk.upper()
        assert "IF NOT EXISTS" in upper, "missing the index existence guard"
        assert "IDX_OB_SNAPSHOTS_TICKER_TS" in upper, (
            "guard must reference the unique index by name"
        )


def test_migration_002_unique_index_still_created():
    """The CREATE UNIQUE INDEX backstop must still ship -- it is what
    the guard checks for and what enforces the invariant going forward.
    """
    sql = _read_migration_002()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_ob_snapshots_ticker_ts" in sql


def test_migration_002_splits_cleanly():
    """The migration file must round-trip through the splitter without
    producing empty chunks or chopping the DO block."""
    sql = _read_migration_002()
    statements = _split_sql_statements(sql)
    assert statements, "splitter returned nothing"
    for s in statements:
        assert s.strip(), "empty statement leaked through"
        assert not s.strip().startswith("--"), (
            f"comment-only chunk leaked through: {s[:60]!r}"
        )
    do_blocks = [s for s in statements if s.lstrip().upper().startswith("DO")]
    assert len(do_blocks) == 1, "expected exactly one DO block in 002"
    assert do_blocks[0].rstrip().endswith("$do$"), (
        "DO block must terminate at its $do$ tag, not mid-body"
    )


# ─── Dedup query semantics: pure logic check ─────────────────────────


@pytest.mark.parametrize(
    "rows, expected_kept",
    [
        # No duplicates -> keep everything (highest ctid per group).
        ([("KX-1", "t0"), ("KX-1", "t1"), ("KX-2", "t0")], 3),
        # One dup pair (same ticker+ts twice) -> keep the higher ctid.
        ([("KX-1", "t0"), ("KX-1", "t0")], 1),
        # Triple dup -> keep one.
        ([("KX-1", "t0"), ("KX-1", "t0"), ("KX-1", "t0")], 1),
        # Mixed: two distinct + one dup pair -> keep three.
        ([("KX-1", "t0"), ("KX-1", "t0"), ("KX-1", "t1"), ("KX-2", "t0")], 3),
    ],
)
def test_dedup_invariant_matches_unique_index(rows, expected_kept):
    """Mirror the post-DELETE invariant the unique index enforces:
    each ``(ticker, timestamp)`` pair appears at most once. We model
    "ctid" as insertion order so ``a.ctid < b.ctid`` keeps the latest
    row, matching the SQL.
    """
    indexed = list(enumerate(rows))
    seen_better: set[tuple[str, str]] = set()
    for ctid, key in indexed:
        for other_ctid, other_key in indexed:
            if ctid < other_ctid and key == other_key:
                seen_better.add((ctid, key[0], key[1]))
                break
    survivors = [r for ctid, r in indexed
                 if (ctid, r[0], r[1]) not in seen_better]
    assert len(survivors) == expected_kept
    assert len({s for s in survivors}) == len(survivors), (
        "dedup must leave the (ticker, ts) pairs unique"
    )
