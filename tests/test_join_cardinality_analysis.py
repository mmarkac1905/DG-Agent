"""Direction D §5.7 — tests for run_join_cardinality_analysis.

Eight test cases per the spec:
  1. per_record_key      on ekko<->ekkn via EBELN
  2. header_detail       on ekko<->ekpo via EBELN
  3. catastrophic_fanout on equi<->mseg via MATNR  ** BG027 regression guard
  4. no_signal           on a candidate with matched/sampled < 0.1
  5. Bridge enumeration finds the synthetic intermediary
  6. Sample saturation when distinct keys < 50
  7. source_row_counts recorded in DAR (downstream staleness key)
  8. Re-run supersedes prior DARs (no duplicates)

All tests build a synthetic raw_sap schema in an in-memory DuckDB
connection and monkeypatch _DAR_CSV / _DB_PATH to tmp_path. Tests do
not touch cpe_analytics.duckdb or any seed CSV.

Note on test 4 (no_signal SERNP bridge):
The spec calls this the "fixture reality regression guard." schema_discovery
in the live repo does not currently surface SERNP↔SERNR as an FK candidate,
so the original SERNP-bridge case is not enumerable from current Source B
output. The test instead constructs a synthetic candidate where matched/
sampled < 0.1 and asserts the no_signal classification + DAR emission. The
classification path is what the spec wants protected; whether SERNP shows
up empirically depends on schema_discovery enrichment outside Direction D.
This is FLAGGED to the user — see test docstring for details.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_join_cardinality_analysis as jca  # noqa: E402


_DAR_FIELDS = jca._DAR_FIELDS


def _setup_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with raw_sap schema + main_seeds metadata tables."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, executed_at_utc TIMESTAMP,
            result_json VARCHAR, status VARCHAR, source_tables VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.source_column_roles (
            table_name VARCHAR, column_name VARCHAR, role VARCHAR
        )
    """)
    return conn


def _add_role(conn, table: str, column: str, role: str = "key") -> None:
    conn.execute(
        "INSERT INTO main_seeds.source_column_roles VALUES (?, ?, ?)",
        [table, column, role],
    )


def _add_schema_discovery_fk(conn, source_table: str,
                             fks: list[dict]) -> None:
    payload = {"fk_candidates": fks}
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results "
        "(id, analysis_type, status, source_tables, result_json) "
        "VALUES (?, 'schema_discovery', 'success', ?, ?)",
        [f"DAR-SD-{source_table}", source_table, json.dumps(payload)],
    )


def _patch_emit(monkeypatch, tmp_path) -> Path:
    """Redirect _DAR_CSV to a tmp file. Returns the path for read-back."""
    tmp_csv = tmp_path / "domain_analysis_results.csv"
    monkeypatch.setattr(jca, "_DAR_CSV", tmp_csv)
    # Also patch the supersede helper's view of _DAR_CSV (it imports its
    # own constant from _dar_supersede).
    import _dar_supersede
    monkeypatch.setattr(_dar_supersede, "_DAR_CSV", tmp_csv)
    return tmp_csv


def _read_emitted(tmp_csv: Path) -> list[dict]:
    if not tmp_csv.exists():
        return []
    with tmp_csv.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _findings(rows: list[dict]) -> list[dict]:
    """Parse result_json from each emitted row for assertion convenience."""
    out = []
    for r in rows:
        if r.get("analysis_type") != "join_cardinality":
            continue
        try:
            out.append(json.loads(r["result_json"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return out


# ─── Test 1: per_record_key (ekko <-> ekkn via EBELN) ──────────────────

def test_01_per_record_key_ekko_ekkn(tmp_path, monkeypatch) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # ekko: 60 rows, EBELN unique 1..60
    conn.execute("CREATE TABLE raw_sap.ekko (EBELN VARCHAR, BUKRS VARCHAR)")
    for i in range(1, 61):
        conn.execute("INSERT INTO raw_sap.ekko VALUES (?, ?)",
                     [f"PO{i:04d}", "1000"])
    # ekkn: 60 rows, one EBELN per row (1:1)
    conn.execute("CREATE TABLE raw_sap.ekkn (EBELN VARCHAR, ZEKKN VARCHAR)")
    for i in range(1, 61):
        conn.execute("INSERT INTO raw_sap.ekkn VALUES (?, ?)",
                     [f"PO{i:04d}", "01"])

    n = jca.analyze_pair(conn, "ekko", "ekkn", all_tables=["ekko", "ekkn"])
    rows = _read_emitted(tmp_path / "domain_analysis_results.csv")
    findings = _findings(rows)

    assert n >= 1
    ebeln = [f for f in findings if f["key_columns_t1"] == ["EBELN"]
             and f["kind"] == "direct"]
    assert ebeln, f"expected EBELN direct candidate; got {findings}"
    f = ebeln[0]
    assert f["fanout_class"] == "per_record_key", (
        f"expected per_record_key for 1:1 mapping; got {f['fanout_class']} "
        f"(avg={f['avg_fanout']}, stddev={f['stddev_fanout']}, "
        f"matched_ratio={f['matched_keys_ratio']})"
    )
    assert "shared_name" in f["source"]


# ─── Test 2: header_detail (ekko <-> ekpo via EBELN) ───────────────────

def test_02_header_detail_ekko_ekpo(tmp_path, monkeypatch) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # ekko: 60 distinct headers
    conn.execute("CREATE TABLE raw_sap.ekko (EBELN VARCHAR, BUKRS VARCHAR)")
    for i in range(1, 61):
        conn.execute("INSERT INTO raw_sap.ekko VALUES (?, ?)",
                     [f"PO{i:04d}", "1000"])
    # ekpo: each header has 5 line items → header_detail (avg 5, low stddev)
    conn.execute("CREATE TABLE raw_sap.ekpo (EBELN VARCHAR, EBELP VARCHAR)")
    for i in range(1, 61):
        for j in range(1, 6):
            conn.execute("INSERT INTO raw_sap.ekpo VALUES (?, ?)",
                         [f"PO{i:04d}", f"{j:02d}"])

    n = jca.analyze_pair(conn, "ekko", "ekpo", all_tables=["ekko", "ekpo"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))

    assert n >= 1
    ebeln = [f for f in findings if f["key_columns_t1"] == ["EBELN"]
             and f["kind"] == "direct"]
    assert ebeln
    f = ebeln[0]
    assert f["fanout_class"] == "header_detail", (
        f"expected header_detail for 1:5 with no variance; "
        f"got {f['fanout_class']} (avg={f['avg_fanout']}, "
        f"stddev={f['stddev_fanout']})"
    )
    assert 4.5 <= f["avg_fanout"] <= 5.5


# ─── Test 3: catastrophic_fanout (equi <-> mseg via MATNR) ─────────────
# **BG027 regression guard.**

def test_03_catastrophic_fanout_equi_mseg(tmp_path, monkeypatch) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # equi: 1000 rows, only 5 distinct MATNR (200 each → fanout 200 per
    # MATNR when joined to mseg with similar concentration)
    conn.execute("CREATE TABLE raw_sap.equi (EQUNR VARCHAR, MATNR VARCHAR)")
    for i in range(1000):
        conn.execute("INSERT INTO raw_sap.equi VALUES (?, ?)",
                     [f"EQ{i:06d}", f"MAT{i % 5:03d}"])
    # mseg: 1000 rows, same 5 distinct MATNR (200 per MATNR)
    conn.execute("CREATE TABLE raw_sap.mseg (MBLNR VARCHAR, MATNR VARCHAR)")
    for i in range(1000):
        conn.execute("INSERT INTO raw_sap.mseg VALUES (?, ?)",
                     [f"MB{i:06d}", f"MAT{i % 5:03d}"])
    # 5 MATNR × 200 mseg rows each = avg fanout 200 per equi MATNR sampled.

    n = jca.analyze_pair(conn, "equi", "mseg", all_tables=["equi", "mseg"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))

    matnr_direct = [f for f in findings if f["key_columns_t1"] == ["MATNR"]
                    and f["kind"] == "direct"]
    assert matnr_direct, "MATNR direct candidate must be enumerated"
    f = matnr_direct[0]
    assert f["fanout_class"] == "catastrophic_fanout", (
        f"expected catastrophic_fanout for MATNR 5x200; "
        f"got {f['fanout_class']} (avg={f['avg_fanout']})"
    )
    assert f["avg_fanout"] >= 100, f"avg_fanout should be >= 100; got {f['avg_fanout']}"


# ─── Test 4: no_signal classification (synthetic) ──────────────────────
# **Spec calls this the "fixture reality regression guard" for the
# SERNP bridge. Implemented as classification-logic test because the
# live schema_discovery output does not currently surface SERNP as an
# FK candidate. See module docstring for the FLAG.**

def test_04_no_signal_when_matched_ratio_below_threshold(
    tmp_path, monkeypatch,
) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # left: 100 rows with distinct K (100 distinct values)
    conn.execute("CREATE TABLE raw_sap.lefta (K VARCHAR)")
    for i in range(100):
        conn.execute("INSERT INTO raw_sap.lefta VALUES (?)",
                     [f"L{i:04d}"])
    # right: 50 rows with K values that DO NOT overlap with left
    # (all distinct, all out of left's range). Match ratio will be 0.
    conn.execute("CREATE TABLE raw_sap.rightb (K VARCHAR)")
    for i in range(50):
        conn.execute("INSERT INTO raw_sap.rightb VALUES (?)",
                     [f"R{i:04d}"])

    n = jca.analyze_pair(conn, "lefta", "rightb",
                         all_tables=["lefta", "rightb"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))

    direct_k = [f for f in findings if f["key_columns_t1"] == ["K"]
                and f["kind"] == "direct"]
    assert direct_k, "K direct candidate must be enumerated"
    f = direct_k[0]
    assert f["fanout_class"] == "no_signal", (
        f"expected no_signal when matched_keys=0; got {f['fanout_class']} "
        f"(matched_ratio={f['matched_keys_ratio']})"
    )
    assert f["matched_keys"] == 0
    assert f["matched_keys_ratio"] == 0.0


# ─── Test 5: bridge enumeration ────────────────────────────────────────

def test_05_bridge_enumeration_finds_intermediary(
    tmp_path, monkeypatch,
) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # Build: tA <-> tB direct (no shared cols); tA <-> tC via X; tC <-> tB via Y.
    # Bridge candidate: tA -> tC -> tB.
    conn.execute("CREATE TABLE raw_sap.ta (X VARCHAR, Z VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.tb (Y VARCHAR, W VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.tc (X VARCHAR, Y VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.ta VALUES (?, ?)",
                     [f"X{i:03d}", f"Z{i:03d}"])
        conn.execute("INSERT INTO raw_sap.tb VALUES (?, ?)",
                     [f"Y{i:03d}", f"W{i:03d}"])
        conn.execute("INSERT INTO raw_sap.tc VALUES (?, ?)",
                     [f"X{i:03d}", f"Y{i:03d}"])
    # Direction D Amendment 2 rule 2(a): bridge t3 sides must be role='key'.
    _add_role(conn, "tc", "X", "key")
    _add_role(conn, "tc", "Y", "key")

    bridges = jca._bridge_candidates(conn, "ta", "tb",
                                     all_tables=["ta", "tb", "tc"])
    via_tc = [b for b in bridges if b["bridge_via"] == "tc"
              and b["key_columns_t1"] == ["X"]
              and b["key_columns_t2"] == ["Y"]]
    assert via_tc, (
        f"expected bridge ta->tc->tb on X/Y; got {bridges}"
    )

    # End-to-end: analyze and verify a bridge DAR is emitted with the
    # expected classification (per_record_key here, since X and Y are
    # both unique-1:1).
    jca.analyze_pair(conn, "ta", "tb", all_tables=["ta", "tb", "tc"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))
    bridge_findings = [f for f in findings if f["kind"] == "bridge"
                       and f["bridge_via"] == "tc"]
    assert bridge_findings
    f = bridge_findings[0]
    assert f["fanout_class"] in ("per_record_key", "header_detail"), (
        f"bridge ta->tc->tb on X/Y should resolve cleanly; "
        f"got {f['fanout_class']}"
    )


# ─── Test 6: sample saturation ─────────────────────────────────────────

def test_06_sample_saturation_when_distinct_below_floor(
    tmp_path, monkeypatch,
) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # Only 10 distinct keys < _SAMPLE_FLOOR (50) → sample_saturated=true
    conn.execute("CREATE TABLE raw_sap.tiny1 (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.tiny2 (K VARCHAR)")
    for i in range(10):
        conn.execute("INSERT INTO raw_sap.tiny1 VALUES (?)", [f"K{i:02d}"])
        conn.execute("INSERT INTO raw_sap.tiny2 VALUES (?)", [f"K{i:02d}"])

    jca.analyze_pair(conn, "tiny1", "tiny2",
                     all_tables=["tiny1", "tiny2"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))
    direct = [f for f in findings if f["kind"] == "direct"]
    assert direct
    assert direct[0]["sample_saturated"] is True, (
        f"expected sample_saturated=True when distinct < floor; "
        f"got {direct[0]['sample_saturated']}"
    )
    assert direct[0]["sample_size"] == 10


# ─── Test 7: source_row_counts recorded for staleness detection ────────

def test_07_source_row_counts_recorded_in_dar(tmp_path, monkeypatch) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    conn.execute("CREATE TABLE raw_sap.aa (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.bb (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.aa VALUES (?)", [f"K{i:03d}"])
    for i in range(120):
        conn.execute("INSERT INTO raw_sap.bb VALUES (?)", [f"K{i % 60:03d}"])

    jca.analyze_pair(conn, "aa", "bb", all_tables=["aa", "bb"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))
    assert findings
    f = findings[0]
    assert f["source_row_counts"] == {"aa": 60, "bb": 120}, (
        f"expected exact row_counts; got {f['source_row_counts']}"
    )


# ─── Test 8: re-run supersedes prior DARs ──────────────────────────────

def test_08_rerun_supersedes_prior_dars(tmp_path, monkeypatch) -> None:
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    conn.execute("CREATE TABLE raw_sap.dd (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.ee (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.dd VALUES (?)", [f"K{i:03d}"])
        conn.execute("INSERT INTO raw_sap.ee VALUES (?)", [f"K{i:03d}"])

    # First run
    jca.analyze_pair(conn, "dd", "ee", all_tables=["dd", "ee"])
    rows_after_first = _read_emitted(tmp_path / "domain_analysis_results.csv")
    n_first = len([r for r in rows_after_first
                   if r["analysis_type"] == "join_cardinality"])
    assert n_first >= 1
    assert all(r["superseded_by"] == "" for r in rows_after_first
               if r["analysis_type"] == "join_cardinality")

    # Second run — same pair
    jca.analyze_pair(conn, "dd", "ee", all_tables=["dd", "ee"])
    rows_after_second = _read_emitted(tmp_path / "domain_analysis_results.csv")
    cards = [r for r in rows_after_second
             if r["analysis_type"] == "join_cardinality"]
    # Should have first-run rows (superseded) + second-run rows (current)
    assert len(cards) == 2 * n_first
    superseded = [r for r in cards if r["superseded_by"]]
    current = [r for r in cards if not r["superseded_by"]]
    assert len(superseded) == n_first, (
        f"expected first {n_first} rows superseded; got {len(superseded)}"
    )
    assert len(current) == n_first, (
        f"expected second {n_first} rows current; got {len(current)}"
    )
    # All superseded rows point at one of the new ids
    new_ids = {r["id"] for r in current}
    for r in superseded:
        assert r["superseded_by"] in new_ids
        assert r["status"] == "superseded"


# ─── Test 9: Amendment 2 rule 2(a) — bridge t3 role-key filter ─────────

def test_09_bridge_t3_role_filter_drops_non_key_columns(
    tmp_path, monkeypatch,
) -> None:
    """Bridges through a non-key t3 column must NOT be enumerated.

    Setup: ta-tc share K1 (key on tc); tc-tb share K1 (key on tc) AND
    AMOUNT (NOT key on tc — measure-style column). Bridge candidate
    using AMOUNT as t3 right-side key must be filtered.
    """
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    conn.execute("CREATE TABLE raw_sap.ta (K1 VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.tb (K1 VARCHAR, AMOUNT VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.tc (K1 VARCHAR, AMOUNT VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.ta VALUES (?)", [f"K{i:03d}"])
        conn.execute("INSERT INTO raw_sap.tb VALUES (?, ?)",
                     [f"K{i:03d}", f"A{i:03d}"])
        conn.execute("INSERT INTO raw_sap.tc VALUES (?, ?)",
                     [f"K{i:03d}", f"A{i:03d}"])
    # tc.K1 is role='key'; tc.AMOUNT is role='measure' (NOT key)
    _add_role(conn, "tc", "K1", "key")
    _add_role(conn, "tc", "AMOUNT", "measure")

    bridges = jca._bridge_candidates(conn, "ta", "tb",
                                     all_tables=["ta", "tb", "tc"])
    # Bridges via tc must use K1 on BOTH sides (k3_left + k3_right).
    # Any bridge involving AMOUNT on either t3 side is filtered.
    assert bridges, "expected at least one K1/K1 bridge through tc"
    for b in bridges:
        assert "AMOUNT" not in [c.upper() for c in b["bridge_keys_left"]], (
            f"AMOUNT should not appear as left-side bridge key: {b}"
        )
        assert "AMOUNT" not in [c.upper() for c in b["bridge_keys_right"]], (
            f"AMOUNT should not appear as right-side bridge key: {b}"
        )


# ─── Test 10: Amendment 2 rule 2(c) — two-pass short-circuit ───────────

def test_10_two_pass_skips_bridges_when_direct_per_record_key(
    tmp_path, monkeypatch,
) -> None:
    """If a direct candidate classifies per_record_key, bridge enumeration
    is short-circuited entirely for that pair.
    """
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # Direct per_record_key: 1:1 on K
    conn.execute("CREATE TABLE raw_sap.pa (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.pb (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.pa VALUES (?)", [f"K{i:03d}"])
        conn.execute("INSERT INTO raw_sap.pb VALUES (?)", [f"K{i:03d}"])
    # A potential bridge intermediary that would have produced bridges
    # absent the two-pass short-circuit.
    conn.execute("CREATE TABLE raw_sap.pc (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.pc VALUES (?)", [f"K{i:03d}"])
    _add_role(conn, "pc", "K", "key")

    jca.analyze_pair(conn, "pa", "pb", all_tables=["pa", "pb", "pc"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))

    direct_findings = [f for f in findings if f["kind"] == "direct"]
    bridge_findings = [f for f in findings if f["kind"] == "bridge"]
    assert direct_findings, "direct candidate should have been emitted"
    assert any(f["fanout_class"] == "per_record_key" for f in direct_findings)
    assert bridge_findings == [], (
        f"expected zero bridges (two-pass short-circuit); got {len(bridge_findings)}"
    )


# ─── Test 11: Amendment 2 rule 2(c) — header_detail does NOT short-circuit ─

def test_11_two_pass_runs_bridges_when_only_header_detail_direct(
    tmp_path, monkeypatch,
) -> None:
    """If direct is only header_detail (no per_record_key), bridge
    enumeration MUST still run — a bridge might find a per_record_key.
    """
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # Direct header_detail: pa(60) -> pb(300) on K = 1:5 fanout, low variance
    conn.execute("CREATE TABLE raw_sap.qa (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.qb (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.qa VALUES (?)", [f"K{i:03d}"])
        for j in range(5):
            conn.execute("INSERT INTO raw_sap.qb VALUES (?)", [f"K{i:03d}"])
    # Bridge intermediary qc with role='key' on K
    conn.execute("CREATE TABLE raw_sap.qc (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.qc VALUES (?)", [f"K{i:03d}"])
    _add_role(conn, "qc", "K", "key")

    jca.analyze_pair(conn, "qa", "qb", all_tables=["qa", "qb", "qc"])
    findings = _findings(_read_emitted(tmp_path / "domain_analysis_results.csv"))

    direct_findings = [f for f in findings if f["kind"] == "direct"]
    bridge_findings = [f for f in findings if f["kind"] == "bridge"]
    assert direct_findings
    assert all(f["fanout_class"] != "per_record_key" for f in direct_findings)
    assert bridge_findings, (
        "expected bridges enumerated when direct is only header_detail "
        "(rule 2(c) only short-circuits on per_record_key)"
    )


# ─── Test 12: Amendment 2 rule 2(b) — type-family mismatch filtering ───

def test_12_type_family_mismatch_filters_bridge(tmp_path, monkeypatch) -> None:
    """Bridge with VARCHAR-to-INTEGER key on either leg must be filtered
    by rule 2(b) BEFORE measurement.
    """
    conn = _setup_conn()
    _patch_emit(monkeypatch, tmp_path)
    # ra.K is VARCHAR, rc.K is INTEGER -> type mismatch on leg 1
    conn.execute("CREATE TABLE raw_sap.ra (K VARCHAR)")
    conn.execute("CREATE TABLE raw_sap.rc (K INTEGER)")
    conn.execute("CREATE TABLE raw_sap.rb (K VARCHAR)")
    for i in range(60):
        conn.execute("INSERT INTO raw_sap.ra VALUES (?)", [f"K{i:03d}"])
        conn.execute("INSERT INTO raw_sap.rc VALUES (?)", [i])
        conn.execute("INSERT INTO raw_sap.rb VALUES (?)", [f"K{i:03d}"])
    _add_role(conn, "rc", "K", "key")

    bridges = jca._bridge_candidates(conn, "ra", "rb",
                                     all_tables=["ra", "rb", "rc"])
    via_rc = [b for b in bridges if b["bridge_via"] == "rc"]
    assert via_rc == [], (
        f"expected zero bridges via rc due to VARCHAR↔INTEGER type "
        f"mismatch; got {via_rc}"
    )


# ─── harness ───────────────────────────────────────────────────────────

def _run_standalone() -> int:
    import tempfile
    import shutil as _shutil

    class _MP:
        def __init__(self): self.saved = []
        def setattr(self, obj, name, val):
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def restore(self):
            for obj, name, val in reversed(self.saved):
                setattr(obj, name, val)

    tests = [
        test_01_per_record_key_ekko_ekkn,
        test_02_header_detail_ekko_ekpo,
        test_03_catastrophic_fanout_equi_mseg,
        test_04_no_signal_when_matched_ratio_below_threshold,
        test_05_bridge_enumeration_finds_intermediary,
        test_06_sample_saturation_when_distinct_below_floor,
        test_07_source_row_counts_recorded_in_dar,
        test_08_rerun_supersedes_prior_dars,
        test_09_bridge_t3_role_filter_drops_non_key_columns,
        test_10_two_pass_skips_bridges_when_direct_per_record_key,
        test_11_two_pass_runs_bridges_when_only_header_detail_direct,
        test_12_type_family_mismatch_filters_bridge,
    ]
    failed = 0
    for t in tests:
        tmpdir = Path(tempfile.mkdtemp())
        mp = _MP()
        try:
            t(tmpdir, mp)
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {t.__name__}: {type(e).__name__}: {e}")
        finally:
            mp.restore()
            _shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
