"""Option B Phase 1 — tests for run_bridge_coverage_analysis.

Tests build a synthetic raw_sap schema in an in-memory DuckDB
connection and monkeypatch _DAR_CSV / _DB_PATH to tmp_path. None
touch cpe_analytics.duckdb or any seed CSV.

Coverage:
  Pure-function:
    - module import sanity
    - DAR row shape matches OQ-A (success / skipped / error variants)
    - high-cardinality threshold writes skipped DAR
    - rationale text reflects reach/unreach counts
  Loader:
    - returns list of dicts with expected keys
    - filters confidence!='high' rows
    - filters to_table not in scope
  Measurement:
    - basic FK + filter returns correct reachable / unreachable
    - filter column missing in to-table -> None
    - multi-key FK joins on all key pairs (subset of OQ-B substrate)
  End-to-end via analyze():
    - emits expected DAR count
    - the seri->mseg + BWART case shows '201' unreachable (OQ-B-equivalent)
    - high-cardinality filter column emits status='skipped'
    - no in-scope FK candidates -> zero emitted, error to stderr
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_bridge_coverage_analysis as bca  # noqa: E402


_DAR_FIELDS = bca._DAR_FIELDS


# ----- fixture helpers -----------------------------------------------

def _setup_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with raw_sap + main_seeds.domain_analysis_results."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR,
            analysis_type VARCHAR,
            executed_at_utc TIMESTAMP,
            result_json VARCHAR,
            status VARCHAR,
            source_tables VARCHAR
        )
    """)
    return conn


def _insert_schema_discovery_dar(conn, dar_id: str, source_table: str,
                                 fk_candidates: list[dict]) -> None:
    payload = {"fk_candidates": fk_candidates}
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results "
        "(id, analysis_type, executed_at_utc, result_json, status, "
        " source_tables) "
        "VALUES (?, 'schema_discovery', CURRENT_TIMESTAMP, ?, 'success', ?)",
        [dar_id, json.dumps(payload), source_table],
    )


def _setup_seri_mseg_fixture(conn) -> None:
    """Build minimal raw_sap.seri + raw_sap.mseg matching the OQ-B
    substrate's empirical reachability:
      seri rows have MBLNR from BWART='101' GR documents only
      mseg has 4 distinct BWART codes ('101', '122', '161', '201')
      via single-column MBLNR join, only '101' is reachable.
    """
    conn.execute("""
        CREATE TABLE raw_sap.seri (
            MBLNR VARCHAR,
            ZEILE VARCHAR,
            EQUNR VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE raw_sap.mseg (
            MBLNR VARCHAR,
            ZEILE VARCHAR,
            BWART VARCHAR,
            MATNR VARCHAR
        )
    """)
    # seri: 3 GR documents, each with one item
    conn.execute("INSERT INTO raw_sap.seri VALUES "
                 "('GR001', '0001', 'EQ1'), "
                 "('GR002', '0001', 'EQ2'), "
                 "('GR003', '0001', 'EQ3')")
    # mseg: GR documents (BWART=101) joining seri.MBLNR + non-GR docs
    # whose MBLNR doesn't appear in seri.
    conn.execute("INSERT INTO raw_sap.mseg VALUES "
                 "('GR001', '0001', '101', 'M1'), "
                 "('GR002', '0001', '101', 'M2'), "
                 "('GR003', '0001', '101', 'M3'), "
                 "('VR100', '0001', '122', 'M1'), "
                 "('IS200', '0001', '161', 'M2'), "
                 "('GI300', '0001', '201', 'M3')")
    _insert_schema_discovery_dar(conn, "DAR-SD-seri", "seri", [
        {"from_columns": ["MBLNR"], "to_table": "mseg",
         "to_columns": ["MBLNR"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
    ])
    _insert_schema_discovery_dar(conn, "DAR-SD-mseg", "mseg", [
        {"from_columns": ["MBLNR"], "to_table": "seri",
         "to_columns": ["MBLNR"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
    ])


# ----- pure-function tests -------------------------------------------

def test_module_imports():
    assert bca._ANALYSIS_TYPE == "bridge_coverage_by_filter"
    assert bca._DOMAIN_NAME == "structural"
    assert "BWART" in bca._ALLOWLIST_FILTER_COLUMNS
    assert len(bca._ALLOWLIST_FILTER_COLUMNS) == 10


def test_dar_row_shape_matches_OQA(tmp_path, monkeypatch):
    """Build a success DAR row + verify the OQ-A schema fields."""
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    fk = {
        "from_table": "seri",
        "from_columns": ["MBLNR"],
        "to_table": "mseg",
        "to_columns": ["MBLNR"],
        "referential_integrity_pct": 100.0,
        "confidence": "high",
        "schema_discovery_dar_id": "DAR-SD-seri",
    }
    to_cols = {"MBLNR": "VARCHAR", "BWART": "VARCHAR"}
    measurement = {
        "reachable_values": [
            {"value": "101", "row_count_via_bridge": 3},
        ],
        "all_distinct": ["101", "122", "161", "201"],
        "cardinality_overflow": False,
        "evidence_query_sql": "SELECT t.BWART, COUNT(*) FROM ...",
    }
    row = bca._build_dar_row(
        fk=fk, filter_column="BWART", to_table_columns=to_cols,
        measurement=measurement, status="success",
        run_id="test_run", schema_version="abc123def456",
    )
    # All 18 BAR fields populated
    assert set(row.keys()) == set(_DAR_FIELDS)
    assert row["analysis_type"] == "bridge_coverage_by_filter"
    assert row["status"] == "success"
    assert row["domain_name"] == "structural"
    assert row["source_tables"] == "mseg,seri"
    assert row["executed_by"] == "run_bridge_coverage_analysis.py"
    # result_json is OQ-A shape
    rj = json.loads(row["result_json"])
    assert rj["bridge"]["from_table"] == "seri"
    assert rj["bridge"]["to_table"] == "mseg"
    assert rj["bridge"]["via_table"] is None  # F-1
    assert rj["bridge"]["via_keys_from_to_mid"] == ["MBLNR"]
    assert rj["bridge"]["via_keys_mid_to_to"] == []  # F-1
    assert rj["filter_column"]["table"] == "mseg"
    assert rj["filter_column"]["column"] == "BWART"
    assert rj["unreachable_values"] == ["122", "161", "201"]
    assert rj["value_cardinality"]["all_distinct"] == 4
    assert rj["value_cardinality"]["reachable"] == 1
    assert rj["value_cardinality"]["unreachable"] == 3
    assert rj["measurement_method"] == "group_by_through_fk"


def test_dar_row_skipped_high_cardinality(tmp_path, monkeypatch):
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    fk = {
        "from_table": "ekko",
        "from_columns": ["EBELN"],
        "to_table": "ekpo",
        "to_columns": ["EBELN"],
        "referential_integrity_pct": 100.0,
        "confidence": "high",
        "schema_discovery_dar_id": "DAR-SD-ekko",
    }
    to_cols = {"EBELN": "VARCHAR", "MTART": "VARCHAR"}
    measurement = {
        "reachable_values": [],
        "all_distinct": [],
        "cardinality_overflow": True,
        "evidence_query_sql": "...",
        "skip_reason": "high_cardinality",
        "rationale": "MTART has > 1000 distinct values; not measured.",
    }
    row = bca._build_dar_row(
        fk=fk, filter_column="MTART", to_table_columns=to_cols,
        measurement=measurement, status="skipped",
        run_id="test_run", schema_version="ver",
    )
    assert row["status"] == "skipped"
    rj = json.loads(row["result_json"])
    assert rj["skip_reason"] == "high_cardinality"
    assert "rationale" in rj
    assert rj["bridge"]["from_table"] == "ekko"


def test_rationale_reflects_reach_unreach():
    fk = {"from_table": "seri", "to_table": "mseg",
          "from_columns": ["MBLNR"]}
    txt = bca._build_rationale(fk, "BWART", n_reach=1, n_unreach=3)
    assert "1 of 4 BWART" in txt
    assert "unreachable" in txt
    assert "seri->mseg" in txt
    assert "MBLNR" in txt
    txt2 = bca._build_rationale(fk, "BWART", n_reach=4, n_unreach=0)
    assert "All 4 BWART values reachable" in txt2


# ----- loader tests --------------------------------------------------

def test_load_fk_candidates_returns_dicts():
    conn = _setup_conn()
    _insert_schema_discovery_dar(conn, "DAR-001", "seri", [
        {"from_columns": ["MBLNR"], "to_table": "mseg",
         "to_columns": ["MBLNR"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
    ])
    _insert_schema_discovery_dar(conn, "DAR-002", "mseg", [])
    out = bca._load_in_scope_fk_candidates(conn, ["seri", "mseg"])
    assert len(out) == 1
    assert out[0]["from_table"] == "seri"
    assert out[0]["to_table"] == "mseg"
    assert out[0]["from_columns"] == ["MBLNR"]
    assert out[0]["schema_discovery_dar_id"] == "DAR-001"


def test_load_fk_candidates_filters_low_confidence():
    conn = _setup_conn()
    _insert_schema_discovery_dar(conn, "DAR-001", "seri", [
        {"from_columns": ["MBLNR"], "to_table": "mseg",
         "to_columns": ["MBLNR"],
         "referential_integrity_pct": 80.0, "confidence": "medium"},
    ])
    out = bca._load_in_scope_fk_candidates(conn, ["seri", "mseg"])
    assert out == []


def test_load_fk_candidates_filters_out_of_scope_to_table():
    conn = _setup_conn()
    _insert_schema_discovery_dar(conn, "DAR-001", "seri", [
        {"from_columns": ["MATNR"], "to_table": "mara",
         "to_columns": ["MATNR"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
        {"from_columns": ["MBLNR"], "to_table": "mseg",
         "to_columns": ["MBLNR"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
    ])
    out = bca._load_in_scope_fk_candidates(conn, ["seri", "mseg"])
    assert len(out) == 1
    assert out[0]["to_table"] == "mseg"


# ----- measurement tests ---------------------------------------------

def test_measure_reachability_basic():
    """OQ-B-equivalent: BWART reachable through seri.MBLNR=mseg.MBLNR
    is just ['101']; '201' is in unreachable."""
    conn = _setup_conn()
    _setup_seri_mseg_fixture(conn)
    fk = {
        "from_table": "seri", "from_columns": ["MBLNR"],
        "to_table": "mseg", "to_columns": ["MBLNR"],
    }
    to_cols = bca._table_columns(conn, "mseg")
    res = bca._measure_reachability(conn, fk, "BWART", to_cols)
    assert res is not None
    assert "error" not in res
    reachable_vals = {r["value"] for r in res["reachable_values"]}
    assert reachable_vals == {"101"}
    assert set(res["all_distinct"]) == {"101", "122", "161", "201"}
    assert res["cardinality_overflow"] is False


def test_measure_reachability_filter_column_missing_in_to_table():
    conn = _setup_conn()
    _setup_seri_mseg_fixture(conn)
    fk = {
        "from_table": "seri", "from_columns": ["MBLNR"],
        "to_table": "mseg", "to_columns": ["MBLNR"],
    }
    to_cols = bca._table_columns(conn, "mseg")
    # NONEXISTENT_COL is not in to_cols → resolve fails → returns None
    res = bca._measure_reachability(conn, fk, "NONEXISTENT_COL", to_cols)
    assert res is None


def test_measure_reachability_multi_key_join():
    """Multi-key FK: from_columns=[A,B] to_columns=[A,B] joins on AND."""
    conn = _setup_conn()
    conn.execute("CREATE TABLE raw_sap.left (K1 VARCHAR, K2 VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.right "
                 "(K1 VARCHAR, K2 VARCHAR, BWART VARCHAR)")
    conn.execute("INSERT INTO raw_sap.left VALUES ('A','1'),('B','2')")
    conn.execute("INSERT INTO raw_sap.right VALUES "
                 "('A','1','101'),('A','9','201'),"
                 "('B','2','101'),('Z','Z','201')")
    fk = {
        "from_table": "left", "from_columns": ["K1", "K2"],
        "to_table": "right", "to_columns": ["K1", "K2"],
    }
    to_cols = bca._table_columns(conn, "right")
    res = bca._measure_reachability(conn, fk, "BWART", to_cols)
    assert res is not None
    reachable = {r["value"] for r in res["reachable_values"]}
    # Only '101' reaches via the joint K1+K2 join (the '201' row at K1=Z,
    # K2=Z and the K1=A,K2=9 are unreachable through the joint key).
    assert reachable == {"101"}
    assert "201" in res["all_distinct"]


# ----- end-to-end analyze() tests ------------------------------------

def test_analyze_emits_dars_and_finds_seri_mseg_unreachable_201(
    tmp_path, monkeypatch,
):
    """OQ-B-equivalent end-to-end: analyze on synthetic seri+mseg fixture
    emits at least 1 DAR; the seri->mseg BWART one shows '201' unreachable."""
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    conn = _setup_conn()
    _setup_seri_mseg_fixture(conn)
    emitted = bca.analyze(conn, ["seri", "mseg"])
    assert emitted >= 1

    with bca._DAR_CSV.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == emitted
    bwart_dars = [
        r for r in rows
        if json.loads(r["result_json"]).get("filter_column", {}).get(
            "column"
        ) == "BWART"
    ]
    assert len(bwart_dars) >= 1
    seri_to_mseg = [
        r for r in bwart_dars
        if json.loads(r["result_json"])["bridge"]["from_table"] == "seri"
        and json.loads(r["result_json"])["bridge"]["to_table"] == "mseg"
    ]
    assert len(seri_to_mseg) == 1
    rj = json.loads(seri_to_mseg[0]["result_json"])
    assert "201" in rj["unreachable_values"]
    reachable_vals = {r["value"] for r in rj["reachable_values"]}
    assert reachable_vals == {"101"}


def test_analyze_filter_not_in_to_table_emits_no_dar_for_that_pair(
    tmp_path, monkeypatch,
):
    """If allowlist filter columns aren't in the to-table, no DAR is
    emitted for that filter (silent skip)."""
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    conn = _setup_conn()
    # Fixture: to-table has NO allowlist columns at all
    conn.execute("CREATE TABLE raw_sap.equi "
                 "(EQUNR VARCHAR, MATNR VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.objk "
                 "(EQUNR VARCHAR, SERNR VARCHAR)")
    conn.execute("INSERT INTO raw_sap.equi VALUES ('E1','M1')")
    conn.execute("INSERT INTO raw_sap.objk VALUES ('E1','S1')")
    _insert_schema_discovery_dar(conn, "DAR-001", "equi", [
        {"from_columns": ["EQUNR"], "to_table": "objk",
         "to_columns": ["EQUNR"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
    ])
    emitted = bca.analyze(conn, ["equi", "objk"])
    assert emitted == 0


def test_analyze_high_cardinality_writes_skipped_dar(
    tmp_path, monkeypatch,
):
    """Filter column with > _CARDINALITY_BOUND distinct values writes
    a DAR with status='skipped'."""
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    monkeypatch.setattr(bca, "_CARDINALITY_BOUND", 10)
    conn = _setup_conn()
    conn.execute("CREATE TABLE raw_sap.t1 (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.t2 (K VARCHAR, BWART VARCHAR)")
    conn.execute("INSERT INTO raw_sap.t1 VALUES " +
                 ", ".join(f"('K{i}')" for i in range(20)))
    # 15 distinct BWART values > monkeypatched bound of 10
    conn.execute("INSERT INTO raw_sap.t2 VALUES " +
                 ", ".join(f"('K{i}','BW{i}')" for i in range(15)))
    _insert_schema_discovery_dar(conn, "DAR-001", "t1", [
        {"from_columns": ["K"], "to_table": "t2", "to_columns": ["K"],
         "referential_integrity_pct": 100.0, "confidence": "high"},
    ])
    emitted = bca.analyze(conn, ["t1", "t2"])
    assert emitted == 1
    with bca._DAR_CSV.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["status"] == "skipped"
    rj = json.loads(rows[0]["result_json"])
    assert rj["skip_reason"] == "high_cardinality"


def test_analyze_no_in_scope_fks_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    conn = _setup_conn()
    # No schema_discovery DARs at all
    emitted = bca.analyze(conn, ["seri", "mseg"])
    assert emitted == 0
    captured = capsys.readouterr()
    assert "no in-scope" in captured.err.lower()


def test_next_dar_id_increments(tmp_path, monkeypatch):
    monkeypatch.setattr(bca, "_DAR_CSV", tmp_path / "dar.csv")
    # Empty file: starts at DAR-00001
    assert bca._next_dar_id() == "DAR-00001"
    # Pre-seed with rows
    with bca._DAR_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DAR_FIELDS, lineterminator="\n")
        w.writeheader()
        for did in ["DAR-00001", "DAR-00007", "DAR-00099"]:
            w.writerow({**{k: "" for k in _DAR_FIELDS}, "id": did})
    assert bca._next_dar_id() == "DAR-00100"
