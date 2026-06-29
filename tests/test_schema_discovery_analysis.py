"""Stage F Commit 2 — unit tests for run_schema_discovery_analysis.py.

Uses in-memory DuckDB fixtures (no filesystem CSV writes). Covers:
  - PK discovery (single-column, composite, none-found)
  - FK discovery (100% / 85% / 50% integrity)
  - Relationship shape classification (1:1 / 1:N / N:M)
  - Bridge detection via BFS
  - Skipped DAR on empty/degenerate tables
  - Type-compatibility gating
  - Key-role fallback to SAP naming heuristic
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_schema_discovery_analysis as mod  # noqa: E402


def _fresh_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with raw_sap + main_seeds schemas primed for tests."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw_sap")
    conn.execute("CREATE SCHEMA IF NOT EXISTS main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.source_column_roles (
            table_name VARCHAR,
            column_name VARCHAR,
            role VARCHAR,
            role_confidence VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, executed_at_utc TIMESTAMP,
            result_json VARCHAR, status VARCHAR, source_tables VARCHAR
        )
    """)
    return conn


# ─── PK discovery ──────────────────────────────────────────────────────

def test_01_pk_single_column_short_circuits() -> None:
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.mara (MATNR VARCHAR, MEINS VARCHAR)')
    conn.execute("INSERT INTO raw_sap.mara VALUES ('M001', 'EA'), ('M002', 'EA'), ('M003', 'KG')")
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES "
        "('mara', 'MATNR', 'key', 'high'), "
        "('mara', 'MEINS', 'dimension', 'high')"
    )
    pks = mod._discover_pk_candidates(conn, 'mara')
    assert len(pks) == 1, f"expected 1 PK candidate, got {len(pks)}"
    assert pks[0]['columns'] == ['MATNR']
    assert pks[0]['distinct_ratio'] == 1.0
    assert pks[0]['null_count'] == 0


def test_02_pk_composite_mandt_prefix() -> None:
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.mseg (MANDT VARCHAR, MBLNR VARCHAR, MJAHR INTEGER, ZEILE INTEGER, MATNR VARCHAR)')
    # MANDT is constant (like real SAP), MBLNR+MJAHR repeats, ZEILE is the line differentiator
    rows = []
    for mblnr in ('5000000001', '5000000002'):
        for zeile in (1, 2, 3):
            rows.append(f"('100', '{mblnr}', 2024, {zeile}, 'M001')")
    conn.execute(f"INSERT INTO raw_sap.mseg VALUES {', '.join(rows)}")
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES "
        "('mseg', 'MANDT', 'key', 'high'), "
        "('mseg', 'MBLNR', 'key', 'high'), "
        "('mseg', 'MJAHR', 'key', 'high'), "
        "('mseg', 'ZEILE', 'key', 'high'), "
        "('mseg', 'MATNR', 'key', 'medium')"
    )
    pks = mod._discover_pk_candidates(conn, 'mseg')
    assert len(pks) >= 1, "expected at least one PK candidate"
    cols = pks[0]['columns']
    # Composite PK must include MANDT + line-grain columns
    assert 'MANDT' in cols
    assert 'ZEILE' in cols or 'MBLNR' in cols


def test_03_pk_none_when_no_unique_combo() -> None:
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.dup (A VARCHAR, B VARCHAR)')
    conn.execute("INSERT INTO raw_sap.dup VALUES ('x', 'y'), ('x', 'y'), ('x', 'y')")
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES "
        "('dup', 'A', 'key', 'high'), ('dup', 'B', 'key', 'high')"
    )
    pks = mod._discover_pk_candidates(conn, 'dup')
    assert pks == [], f"expected no PK candidates, got {pks}"


def test_04_pk_role_fallback_via_sap_naming() -> None:
    """When source_column_roles is empty, fall back to SAP naming heuristic."""
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.lfa1 (LIFNR VARCHAR, NAME1 VARCHAR)')
    conn.execute("INSERT INTO raw_sap.lfa1 VALUES ('V001', 'Vendor A'), ('V002', 'Vendor B')")
    # No source_column_roles entries
    pks = mod._discover_pk_candidates(conn, 'lfa1')
    assert len(pks) == 1
    assert pks[0]['columns'] == ['LIFNR']


# ─── FK discovery ──────────────────────────────────────────────────────

def test_05_fk_100pct_integrity_emitted_high_confidence() -> None:
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.mara (MATNR VARCHAR)')
    conn.execute("INSERT INTO raw_sap.mara VALUES ('M001'), ('M002'), ('M003')")
    conn.execute('CREATE TABLE raw_sap.mseg (MBLNR VARCHAR, MATNR VARCHAR)')
    conn.execute(
        "INSERT INTO raw_sap.mseg VALUES "
        "('5000001', 'M001'), ('5000002', 'M002'), ('5000003', 'M001')"
    )
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES "
        "('mara', 'MATNR', 'key', 'high'), "
        "('mseg', 'MBLNR', 'key', 'high'), "
        "('mseg', 'MATNR', 'key', 'high')"
    )
    fks = mod._discover_fk_candidates(conn, 'mseg', ['mara', 'mseg'])
    hits = [f for f in fks if f['to_table'] == 'mara' and f['from_columns'] == ['MATNR']]
    assert len(hits) == 1
    assert hits[0]['confidence'] == 'high'
    assert hits[0]['referential_integrity_pct'] == 100.0


def test_06_fk_medium_integrity_emitted_medium_confidence() -> None:
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.mara (MATNR VARCHAR)')
    conn.execute("INSERT INTO raw_sap.mara VALUES ('M001'), ('M002'), ('M003'), ('M004')")
    conn.execute('CREATE TABLE raw_sap.mseg (MBLNR VARCHAR, MATNR VARCHAR)')
    # 5 distinct MATNR in mseg, 4 in mara: 4/5 = 80% integrity
    conn.execute(
        "INSERT INTO raw_sap.mseg VALUES "
        "('5000001', 'M001'), ('5000002', 'M002'), ('5000003', 'M003'), "
        "('5000004', 'M004'), ('5000005', 'M999')"
    )
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES "
        "('mara', 'MATNR', 'key', 'high'), "
        "('mseg', 'MBLNR', 'key', 'high'), "
        "('mseg', 'MATNR', 'key', 'high')"
    )
    fks = mod._discover_fk_candidates(conn, 'mseg', ['mara', 'mseg'])
    hits = [f for f in fks if f['to_table'] == 'mara' and f['from_columns'] == ['MATNR']]
    assert len(hits) == 1
    assert hits[0]['confidence'] == 'medium'
    assert hits[0]['referential_integrity_pct'] == 80.0


def test_07_fk_low_integrity_not_emitted() -> None:
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.mara (MATNR VARCHAR)')
    conn.execute("INSERT INTO raw_sap.mara VALUES ('M001'), ('M002')")
    conn.execute('CREATE TABLE raw_sap.mseg (MBLNR VARCHAR, MATNR VARCHAR)')
    # 4 distinct in mseg, 2 overlap: 50% integrity — below medium threshold
    conn.execute(
        "INSERT INTO raw_sap.mseg VALUES "
        "('5000001', 'M001'), ('5000002', 'M002'), "
        "('5000003', 'M999'), ('5000004', 'M888')"
    )
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES "
        "('mara', 'MATNR', 'key', 'high'), "
        "('mseg', 'MBLNR', 'key', 'high'), "
        "('mseg', 'MATNR', 'key', 'high')"
    )
    fks = mod._discover_fk_candidates(conn, 'mseg', ['mara', 'mseg'])
    hits = [f for f in fks if f['to_table'] == 'mara' and f['from_columns'] == ['MATNR']]
    assert hits == [], f"expected no FK (50% integrity < 80% threshold), got {hits}"


# ─── Relationship shapes ───────────────────────────────────────────────

def test_08_shape_one_to_one_detected() -> None:
    """Two tables sharing an FK where both have ~same row_count/distinct = 1:1."""
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.lfa1 (LIFNR VARCHAR)')
    conn.execute('CREATE TABLE raw_sap.lfb1 (LIFNR VARCHAR, BUKRS VARCHAR)')
    for i in range(5):
        conn.execute(f"INSERT INTO raw_sap.lfa1 VALUES ('V{i:03d}')")
        conn.execute(f"INSERT INTO raw_sap.lfb1 VALUES ('V{i:03d}', 'HT01')")
    fk = {
        'from_columns': ['LIFNR'], 'to_table': 'lfa1', 'to_columns': ['LIFNR'],
        'confidence': 'high', 'referential_integrity_pct': 100.0,
    }
    shapes = mod._classify_relationship_shapes(conn, 'lfb1', [fk])
    assert len(shapes) == 1
    assert shapes[0]['shape'] == 'one_to_one'
    assert shapes[0]['cardinality'] == '1:1'


def test_09_shape_header_detail_with_sum_match() -> None:
    """mkpf (header) 1:N mseg (detail). Sum-match on NETWR should be computed."""
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.mkpf (MBLNR VARCHAR, NETWR DOUBLE)')
    conn.execute('CREATE TABLE raw_sap.mseg (MBLNR VARCHAR, ZEILE INTEGER, NETWR DOUBLE)')
    # 2 headers, 3+2 lines. NETWR sums match.
    conn.execute("INSERT INTO raw_sap.mkpf VALUES ('5000001', 150.0), ('5000002', 50.0)")
    conn.execute(
        "INSERT INTO raw_sap.mseg VALUES "
        "('5000001', 1, 50.0), ('5000001', 2, 75.0), ('5000001', 3, 25.0), "
        "('5000002', 1, 30.0), ('5000002', 2, 20.0)"
    )
    fk = {
        'from_columns': ['MBLNR'], 'to_table': 'mkpf', 'to_columns': ['MBLNR'],
        'confidence': 'high', 'referential_integrity_pct': 100.0,
    }
    shapes = mod._classify_relationship_shapes(conn, 'mseg', [fk])
    assert len(shapes) == 1
    assert shapes[0]['shape'] == 'detail_header'  # mseg has more rows per distinct FK
    assert 'sum_match_pct' in shapes[0], "sum-match should be computed for header-detail"
    # NETWR sums: mkpf{5000001: 150, 5000002: 50} matches mseg SUM. All groups match.
    assert shapes[0]['sum_match_pct'] == 100.0


# ─── Bridge detection ──────────────────────────────────────────────────

def test_10_bridge_two_hop_path_detected() -> None:
    """equi → objk → mseg bridge via FK graph in prior DARs."""
    conn = _fresh_conn()
    # Fabricate prior schema_discovery DAR for objk with FK to mseg
    objk_payload = {
        'pk_candidates': [],
        'fk_candidates': [{
            'from_columns': ['SERNR'],
            'to_table': 'mseg',
            'to_columns': ['SERNR'],
            'confidence': 'high',
            'referential_integrity_pct': 100.0,
        }],
        'relationship_shapes': [],
        'bridge_tables': [],
    }
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results VALUES "
        "('DAR-00001', 'schema_discovery', '2026-04-23 10:00:00', ?, 'success', 'objk')",
        [json.dumps(objk_payload)]
    )
    # equi's direct FKs: equi.EQUNR → objk.EQUNR (high confidence)
    direct_fks = [{
        'from_columns': ['EQUNR'],
        'to_table': 'objk',
        'to_columns': ['EQUNR'],
        'confidence': 'high',
        'referential_integrity_pct': 100.0,
    }]
    bridges = mod._discover_bridges(conn, 'equi', direct_fks)
    # Expect one bridge: equi → objk (direct) → mseg
    assert len(bridges) == 1
    assert bridges[0]['between'] == ['equi', 'mseg']
    assert bridges[0]['via'] == 'objk'


# ─── Skipped DAR path ──────────────────────────────────────────────────

def test_11_skipped_dar_on_empty_table(tmp_path, monkeypatch) -> None:
    """Empty table → analyze_table emits skipped DAR + returns 0."""
    conn = _fresh_conn()
    conn.execute('CREATE TABLE raw_sap.empty_one (A VARCHAR, B VARCHAR)')
    # No rows — row_count=0

    # Redirect DAR csv writes to tmp dir
    tmp_dar = tmp_path / "domain_analysis_results.csv"
    monkeypatch.setattr(mod, "_DAR_CSV", tmp_dar)
    monkeypatch.setattr(mod, "_SEED_DIR", tmp_path)

    rc = mod.analyze_table(conn, 'empty_one')
    assert rc == 0
    assert tmp_dar.exists()
    rows = tmp_dar.read_text(encoding='utf-8').splitlines()
    assert len(rows) >= 2  # header + 1 data row
    # Last column shapes: status = 'skipped'
    assert 'skipped' in rows[-1]


# ─── Type compatibility ───────────────────────────────────────────────

def test_12_types_compatible_char_families_ok() -> None:
    assert mod._types_compatible('VARCHAR', 'CHAR') is True
    assert mod._types_compatible('INTEGER', 'BIGINT') is True
    assert mod._types_compatible('VARCHAR', 'INTEGER') is False
    assert mod._types_compatible('', 'VARCHAR') is False


# ─── harness ──────────────────────────────────────────────────────────

def _run_standalone() -> int:
    import tempfile
    tests_no_fixtures = [
        test_01_pk_single_column_short_circuits,
        test_02_pk_composite_mandt_prefix,
        test_03_pk_none_when_no_unique_combo,
        test_04_pk_role_fallback_via_sap_naming,
        test_05_fk_100pct_integrity_emitted_high_confidence,
        test_06_fk_medium_integrity_emitted_medium_confidence,
        test_07_fk_low_integrity_not_emitted,
        test_08_shape_one_to_one_detected,
        test_09_shape_header_detail_with_sum_match,
        test_10_bridge_two_hop_path_detected,
        test_12_types_compatible_char_families_ok,
    ]
    failed = 0
    for t in tests_no_fixtures:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {t.__name__}: {type(e).__name__}: {e}")

    # test_11 needs tmp_path + monkeypatch fixtures — minimal substitutes
    try:
        import os as _os
        tmpdir = Path(tempfile.mkdtemp())
        class _MP:
            def __init__(self): self.saved = []
            def setattr(self, obj, name, val):
                self.saved.append((obj, name, getattr(obj, name)))
                setattr(obj, name, val)
            def restore(self):
                for obj, name, val in reversed(self.saved):
                    setattr(obj, name, val)
        mp = _MP()
        try:
            test_11_skipped_dar_on_empty_table(tmpdir, mp)
            print("  [PASS] test_11_skipped_dar_on_empty_table")
        finally:
            mp.restore()
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    except AssertionError as e:
        failed += 1
        print(f"  [FAIL] test_11_skipped_dar_on_empty_table: {e}")
    except Exception as e:  # noqa: BLE001
        failed += 1
        print(f"  [ERR ] test_11_skipped_dar_on_empty_table: {type(e).__name__}: {e}")

    total = len(tests_no_fixtures) + 1
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
