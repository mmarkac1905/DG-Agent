"""KI #71 Step 1 unit tests — archive_dependency_analyzer.

All tests inject glossary/s2t/manifest fixtures; the filesystem and
subprocess paths are exercised only via the freshness-gate logic
tests, which use ``compile_if_stale=False`` to assert the gate fires.

Test plan
---------
01-03   shape: empty s2t / already-archived / term not found
04      exclusive archive — no blockers
05      sharing blocker — another approved term owns the same model
06      sharing with an archived term is NOT a blocker
07      downstream blocker — a consumer owned by another term
08      downstream that IS in target_models is NOT a blocker
09      multiple blockers — all_blocking_terms dedupes correctly
10      terms_using_model direct unit test
11      get_downstream_models direct unit test
12      get_term_target_models direct unit test
13      _manifest_contains_models content-check helper
14      manifest freshness gate refuses to compile when forbidden
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

from archive_dependency_analyzer import (  # noqa: E402
    ArchiveImpact,
    DownstreamBlocker,
    SharingBlocker,
    TermRef,
    _manifest_contains_models,
    analyze_archive_impact,
    get_downstream_models,
    get_term_target_models,
    terms_using_model,
)


# ---------------------------------------------------------------------------
# Fixture builders — kept tiny so each test reads top-to-bottom.
# ---------------------------------------------------------------------------

def _glossary(rows):
    return pd.DataFrame(rows, columns=["id", "term_name", "status"])


def _s2t(rows):
    return pd.DataFrame(rows, columns=["business_term_id", "target_model"])


def _node(name, depends_on=()):
    """Build a single manifest node dict."""
    return {
        "name": name,
        "resource_type": "model",
        "path": f"marts/{name}.sql",
        "depends_on": {"nodes": list(depends_on)},
    }


def _manifest(nodes_by_name):
    """Wrap simple {name: [upstream_names]} into the manifest schema.
    Upstream names are converted to unique_ids using the same project
    prefix used throughout.
    """
    proj = "cpe_procurement_analytics"
    uid = lambda n: f"model.{proj}.{n}"
    nodes = {}
    for name, ups in nodes_by_name.items():
        nodes[uid(name)] = _node(name, depends_on=[uid(u) for u in ups])
    return {"nodes": nodes}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_01_term_with_no_s2t_rows_is_vacuous_archive() -> None:
    gloss = _glossary([("BG001", "term_one", "approved")])
    s2t = _s2t([])  # term has no deployed artefacts
    impact = analyze_archive_impact(
        "BG001", manifest={"nodes": {}}, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.target_models == []
    assert impact.can_archive is True
    assert impact.exclusive_cascade == []
    assert impact.sharing_blockers == []
    assert impact.downstream_blockers == []


def test_02_already_archived_term_reports_no_op() -> None:
    gloss = _glossary([("BG001", "term_one", "archived")])
    s2t = _s2t([("BG001", "fact_a")])
    impact = analyze_archive_impact(
        "BG001", manifest={"nodes": {}}, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.already_archived is True
    assert impact.can_archive is False
    assert impact.exclusive_cascade == []


def test_03_term_not_in_glossary_raises() -> None:
    gloss = _glossary([("BG001", "term_one", "approved")])
    s2t = _s2t([])
    try:
        analyze_archive_impact(
            "BG999", manifest={"nodes": {}}, glossary_df=gloss, s2t_df=s2t,
        )
    except ValueError as e:
        assert "BG999" in str(e)
        return
    raise AssertionError("expected ValueError for missing term_id")


def test_04_exclusive_archive_passes() -> None:
    gloss = _glossary([
        ("BG001", "term_one", "approved"),
        ("BG002", "term_two", "approved"),
    ])
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG002", "fact_b"),  # different model, no sharing
    ])
    mf = _manifest({"fact_a": [], "fact_b": []})
    impact = analyze_archive_impact(
        "BG001", manifest=mf, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.can_archive is True
    assert impact.exclusive_cascade == ["fact_a"]
    assert impact.sharing_blockers == []
    assert impact.downstream_blockers == []


def test_05_sharing_blocker_when_other_approved_term_owns_model() -> None:
    gloss = _glossary([
        ("BG001", "term_one", "approved"),
        ("BG002", "term_two", "approved"),
    ])
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG002", "fact_a"),  # BG002 also owns fact_a
    ])
    mf = _manifest({"fact_a": []})
    impact = analyze_archive_impact(
        "BG001", manifest=mf, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.can_archive is False
    assert len(impact.sharing_blockers) == 1
    sb = impact.sharing_blockers[0]
    assert sb.model_name == "fact_a"
    assert [t.term_id for t in sb.other_terms] == ["BG002"]
    assert impact.exclusive_cascade == []
    # all_blocking_terms surfaces BG002 for guided unwind
    blocking = [t.term_id for t in impact.all_blocking_terms]
    assert blocking == ["BG002"]


def test_06_sharing_with_archived_term_is_not_a_blocker() -> None:
    gloss = _glossary([
        ("BG001", "term_one", "approved"),
        ("BG002", "term_two", "archived"),  # archived — invisible to sharing check
    ])
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG002", "fact_a"),  # historical, archived
    ])
    mf = _manifest({"fact_a": []})
    impact = analyze_archive_impact(
        "BG001", manifest=mf, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.can_archive is True
    assert impact.sharing_blockers == []


def test_07_downstream_blocker_when_consumer_owned_by_other_term() -> None:
    gloss = _glossary([
        ("BG001", "term_one", "approved"),
        ("BG002", "term_two", "approved"),
    ])
    # BG001 owns fact_a; obt_x ref()s fact_a; BG002 owns obt_x.
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG002", "obt_x"),
    ])
    mf = _manifest({
        "fact_a": [],
        "obt_x": ["fact_a"],
    })
    impact = analyze_archive_impact(
        "BG001", manifest=mf, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.can_archive is False
    assert impact.sharing_blockers == []
    assert len(impact.downstream_blockers) == 1
    db = impact.downstream_blockers[0]
    assert db.model_name == "fact_a"
    assert db.downstream_model == "obt_x"
    assert [t.term_id for t in db.downstream_terms] == ["BG002"]


def test_08_downstream_within_target_set_is_not_a_blocker() -> None:
    """Algorithm refinement: if both upstream and downstream are owned
    by the term being archived, the downstream isn't a blocker — both
    move together."""
    gloss = _glossary([("BG001", "term_one", "approved")])
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG001", "obt_x"),  # BG001 also owns obt_x
    ])
    mf = _manifest({
        "fact_a": [],
        "obt_x": ["fact_a"],  # depends on fact_a but moves with it
    })
    impact = analyze_archive_impact(
        "BG001", manifest=mf, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.can_archive is True
    assert impact.downstream_blockers == []
    assert sorted(impact.exclusive_cascade) == ["fact_a", "obt_x"]


def test_09_multiple_blockers_all_blocking_terms_dedupes() -> None:
    gloss = _glossary([
        ("BG001", "term_one", "approved"),
        ("BG002", "term_two", "approved"),
        ("BG003", "term_three", "approved"),
    ])
    # BG001 owns fact_a AND fact_b.
    # BG002 also lists fact_a (sharing) AND owns obt_x which ref()s fact_b (downstream).
    # BG003 also lists fact_a (sharing again — must dedupe).
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG001", "fact_b"),
        ("BG002", "fact_a"),
        ("BG002", "obt_x"),
        ("BG003", "fact_a"),
    ])
    mf = _manifest({
        "fact_a": [],
        "fact_b": [],
        "obt_x": ["fact_b"],
    })
    impact = analyze_archive_impact(
        "BG001", manifest=mf, glossary_df=gloss, s2t_df=s2t,
    )
    assert impact.can_archive is False
    # Sharing on fact_a: BG002, BG003. Downstream of fact_b → obt_x → BG002.
    blocking_ids = sorted(t.term_id for t in impact.all_blocking_terms)
    assert blocking_ids == ["BG002", "BG003"]


def test_10_terms_using_model_filters_archived_and_excludes_self() -> None:
    gloss = _glossary([
        ("BG001", "self", "approved"),
        ("BG002", "active_user", "approved"),
        ("BG003", "old_user", "archived"),
        ("BG004", "draft_user", "draft"),
    ])
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG002", "fact_a"),
        ("BG003", "fact_a"),
        ("BG004", "fact_a"),
    ])
    result = terms_using_model(
        "fact_a", exclude_term_id="BG001", s2t_df=s2t, glossary_df=gloss,
    )
    ids = [r.term_id for r in result]
    # BG001 excluded (self), BG003 dropped (archived). BG002 + BG004 kept.
    assert ids == ["BG002", "BG004"]
    statuses = {r.term_id: r.status for r in result}
    assert statuses["BG002"] == "approved"
    assert statuses["BG004"] == "draft"


def test_11_get_downstream_models_walks_reverse_refs() -> None:
    mf = _manifest({
        "a": [],
        "b": ["a"],
        "c": ["a"],
        "d": ["b"],
    })
    assert get_downstream_models("a", mf) == ["b", "c"]
    assert get_downstream_models("b", mf) == ["d"]
    assert get_downstream_models("d", mf) == []
    assert get_downstream_models("missing", mf) == []  # not in manifest


def test_12_get_term_target_models_dedupes_and_strips() -> None:
    s2t = _s2t([
        ("BG001", "fact_a"),
        ("BG001", "fact_a"),  # dup
        ("BG001", " fact_b "),  # whitespace
        ("BG001", ""),  # empty
        ("BG002", "fact_z"),  # other term
    ])
    result = get_term_target_models("BG001", s2t_df=s2t)
    assert result == ["fact_a", "fact_b"]


def test_13_manifest_contains_models_helper() -> None:
    mf = _manifest({"fact_a": [], "fact_b": []})
    assert _manifest_contains_models(mf, ["fact_a", "fact_b"]) is True
    assert _manifest_contains_models(mf, ["fact_a"]) is True
    assert _manifest_contains_models(mf, []) is True
    assert _manifest_contains_models(mf, ["fact_a", "missing"]) is False
    assert _manifest_contains_models({"nodes": {}}, ["fact_a"]) is False


def test_14_manifest_freshness_gate_refuses_to_compile_when_forbidden() -> None:
    """When manifest is absent and compile_if_stale=False, the analyzer
    must raise rather than silently degrade. Tests the saga's option
    to assert no subprocess fires."""
    gloss = _glossary([("BG001", "term_one", "approved")])
    s2t = _s2t([("BG001", "fact_a")])
    # No manifest injected — analyzer will hit the freshness gate.
    try:
        analyze_archive_impact(
            "BG001",
            glossary_df=gloss,
            s2t_df=s2t,
            compile_if_stale=False,
        )
    except RuntimeError as e:
        assert "stale or missing" in str(e)
        return
    raise AssertionError(
        "expected RuntimeError when compile is forbidden and manifest is unfresh"
    )


# ---------------------------------------------------------------------------
# Standalone runner — mirrors test_term_status_utils.py
# ---------------------------------------------------------------------------

def _run_standalone() -> int:
    tests = [
        test_01_term_with_no_s2t_rows_is_vacuous_archive,
        test_02_already_archived_term_reports_no_op,
        test_03_term_not_in_glossary_raises,
        test_04_exclusive_archive_passes,
        test_05_sharing_blocker_when_other_approved_term_owns_model,
        test_06_sharing_with_archived_term_is_not_a_blocker,
        test_07_downstream_blocker_when_consumer_owned_by_other_term,
        test_08_downstream_within_target_set_is_not_a_blocker,
        test_09_multiple_blockers_all_blocking_terms_dedupes,
        test_10_terms_using_model_filters_archived_and_excludes_self,
        test_11_get_downstream_models_walks_reverse_refs,
        test_12_get_term_target_models_dedupes_and_strips,
        test_13_manifest_contains_models_helper,
        test_14_manifest_freshness_gate_refuses_to_compile_when_forbidden,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
