"""Tests for plant3d_query.list_valves — headless, no AutoCAD, no network.

list_valves is a thin preset wrapper around list_components that pins
classes=["valve"]. These tests verify:

1. Delegation with valve class filter: only Valves PartCategory returned.
2. No mutation of the caller's data dict.
3. classes supplied by the caller is ignored/overridden.
4. Other filters (line, spec, size, limit) are forwarded intact.
5. Output shape identical to list_components.
6. limit/omitted semantics propagated correctly.
7. Read-only guarantee.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import list_valves


# ===========================================================================
# Helpers: build minimal SQLite databases (reuse pattern from list_components)
# ===========================================================================


def _make_piping_dcf(path: Path, rows: list[tuple]) -> None:
    """Create a Piping.dcf with PipeRunComponent + EngineeringItems.

    rows: (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription,
           Spec, NominalDiameter, NominalUnit)
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE PipeRunComponent "
            "(PnPID INTEGER, LineNumberTag TEXT, Tag TEXT)"
        )
        con.execute(
            "CREATE TABLE EngineeringItems "
            "(PnPID INTEGER, PartCategory TEXT, ShortDescription TEXT, "
            "Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
        )
        for pnpid, line_tag, comp_tag, cat, desc, spec, dia, unit in rows:
            con.execute(
                "INSERT INTO PipeRunComponent "
                "(PnPID, LineNumberTag, Tag) VALUES (?, ?, ?)",
                (pnpid, line_tag, comp_tag),
            )
            con.execute(
                "INSERT INTO EngineeringItems "
                "(PnPID, PartCategory, ShortDescription, Spec, NominalDiameter, NominalUnit) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pnpid, cat, desc, spec, dia, unit),
            )
        con.commit()
    finally:
        con.close()


def _make_project(base: Path, name: str, rows: list[tuple]) -> Path:
    """Build a minimal project folder with Project.xml + Piping.dcf."""
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", rows)
    return proj


# ---------------------------------------------------------------------------
# Canonical mixed dataset (pipes + valves + fittings + flanges)
# (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription, Spec, dia, unit)
# ---------------------------------------------------------------------------
_MIXED_ROWS = [
    # Pipes
    (1,  "L-001", "TAG-P1",  "Pipe",        "Tubo recto A",      "CS150",  2.0,  "in"),
    (2,  "L-001", "TAG-P2",  "Pipe",        "Tubo recto B",      "CS150",  4.0,  "in"),
    (3,  "L-002", "TAG-P3",  "Pipe",        "Tubo recto C",      "SS150",  2.0,  "in"),
    # Valves
    (4,  "L-001", "TAG-V1",  "Valves",      "Valvula bola",      "CS150",  2.0,  "in"),
    (5,  "L-002", "TAG-V2",  "Valves",      "Valvula compuerta", "SS150",  4.0,  "in"),
    (6,  "L-001", "TAG-V3",  "Valves",      "Valvula mariposa",  "CS150",  2.0,  "in"),
    # Fittings
    (7,  "L-001", "TAG-F1",  "Fittings",    "Codo 90",           "CS150",  2.0,  "in"),
    # Flanges
    (8,  "L-002", "TAG-FL1", "Flanges",     "Brida soldada",     "SS150",  4.0,  "in"),
    # Instruments
    (9,  "L-002", "TAG-I1",  "Instruments", "Manometro",         "SS150",  2.0,  "in"),
    # Valve on L-003 with SS300 spec and 50 mm
    (10, "L-003", "TAG-V4",  "Valves",      "Valvula aguja",     "SS300",  50.0, "mm"),
    # Valve with blank tag placeholder
    (11, "L-001", "?-?",     "Valves",      "Valvula placeholder", "CS150", 2.0, "in"),
    # Valve without line tag
    (12, None,    "TAG-V5",  "Valves",      "Valvula sin linea", "CS150",  2.0,  "in"),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Full synthetic project with canonical mixed test data."""
    return _make_project(tmp_path, "VALVE_TEST", _MIXED_ROWS)


@pytest.fixture
def result_valves(proj: Path) -> dict:
    """list_valves with no extra filters and limit=0."""
    return list_valves(str(proj), {"limit": 0})


# ===========================================================================
# Part 1 – Delegation with valve class filter
# ===========================================================================


class TestDelegationValveFilter:
    def test_only_valves_category_returned(self, result_valves):
        """list_valves returns ONLY components with PartCategory='Valves'."""
        cats = {c["class"] for c in result_valves["components"]}
        assert cats <= {"Valves"}, f"Unexpected categories: {cats - {'Valves'}}"

    def test_no_pipes_in_result(self, result_valves):
        """Pipe components (PnPIDs 1-3) must not appear."""
        pnpids = {c["pnpid"] for c in result_valves["components"]}
        assert 1 not in pnpids
        assert 2 not in pnpids
        assert 3 not in pnpids

    def test_no_fittings_in_result(self, result_valves):
        """Fitting components (PnPID 7) must not appear."""
        pnpids = {c["pnpid"] for c in result_valves["components"]}
        assert 7 not in pnpids

    def test_no_flanges_in_result(self, result_valves):
        """Flange components (PnPID 8) must not appear."""
        pnpids = {c["pnpid"] for c in result_valves["components"]}
        assert 8 not in pnpids

    def test_no_instruments_in_result(self, result_valves):
        """Instrument components (PnPID 9) must not appear."""
        pnpids = {c["pnpid"] for c in result_valves["components"]}
        assert 9 not in pnpids

    def test_all_valve_pnpids_present(self, result_valves):
        """All valve PnPIDs (4,5,6,10,11,12) must be present."""
        pnpids = {c["pnpid"] for c in result_valves["components"]}
        assert {4, 5, 6, 10, 11, 12} <= pnpids

    def test_valve_count(self, result_valves):
        """Count must equal 6 (valves: PnPIDs 4,5,6,10,11,12)."""
        assert result_valves["count"] == 6

    def test_filters_classes_is_valve(self, result_valves):
        """filters dict must report classes=["valve"]."""
        assert result_valves["filters"].get("classes") == ["valve"]

    def test_by_class_contains_only_valves(self, result_valves):
        """by_class grouping must only contain the 'Valves' label."""
        labels = {entry["class"] for entry in result_valves["by_class"]}
        assert labels <= {"Valves"}


# ===========================================================================
# Part 2 – No mutation of caller's data dict
# ===========================================================================


class TestNoMutation:
    def test_data_dict_without_classes_not_mutated(self, proj):
        """Calling list_valves must NOT add 'classes' to the original data dict."""
        data = {"limit": 10}
        list_valves(str(proj), data)
        assert "classes" not in data, (
            "list_valves mutated the caller's data dict by adding 'classes'"
        )

    def test_data_dict_with_classes_not_mutated(self, proj):
        """The original 'classes' value in the caller's dict must be preserved."""
        data = {"classes": ["pipe"], "limit": 0}
        list_valves(str(proj), data)
        # The original dict must still have ["pipe"], not ["valve"]
        assert data["classes"] == ["pipe"], (
            "list_valves mutated the caller's data dict, overwriting 'classes'"
        )

    def test_none_data_does_not_crash(self, proj):
        """Passing data=None must not raise."""
        r = list_valves(str(proj), None)
        assert r["ok"] is True

    def test_empty_data_does_not_crash(self, proj):
        """Passing data={} must not raise."""
        r = list_valves(str(proj), {})
        assert r["ok"] is True


# ===========================================================================
# Part 3 – classes from caller is ignored/overridden
# ===========================================================================


class TestClassesOverride:
    def test_pipe_classes_overridden_to_valve(self, proj):
        """Even if caller passes classes=["pipe"], result must be only valves."""
        r = list_valves(str(proj), {"classes": ["pipe"], "limit": 0})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Valves"}, f"Expected only Valves, got: {cats}"

    def test_fitting_classes_overridden_to_valve(self, proj):
        """Even if caller passes classes=["fitting"], result must be only valves."""
        r = list_valves(str(proj), {"classes": ["fitting"], "limit": 0})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Valves"}

    def test_empty_classes_overridden_to_valve(self, proj):
        """Even if caller passes classes=[], result must be only valves."""
        r = list_valves(str(proj), {"classes": [], "limit": 0})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Valves"}

    def test_filters_echo_classes_valve(self, proj):
        """filters.classes must always be ['valve'] regardless of input."""
        for caller_classes in [["pipe"], ["fitting", "flange"], [], ["valve"]]:
            r = list_valves(str(proj), {"classes": caller_classes, "limit": 0})
            assert r["filters"].get("classes") == ["valve"], (
                f"With caller classes={caller_classes!r}, "
                f"got filters.classes={r['filters'].get('classes')!r}"
            )


# ===========================================================================
# Part 4 – Other filters forwarded intact
# ===========================================================================


class TestFilterForwarding:
    def test_line_filter_forwarded(self, proj):
        """Valves filtered by line=L-001 must all belong to L-001."""
        r = list_valves(str(proj), {"limit": 0, "line": "L-001"})
        # Only valves on L-001: PnPIDs 4, 6, 11
        assert r["count"] == 3
        for c in r["components"]:
            assert c["line"] == "L-001"

    def test_spec_filter_forwarded(self, proj):
        """Valves filtered by spec=CS150 must all have spec=CS150."""
        r = list_valves(str(proj), {"limit": 0, "spec": "CS150"})
        for c in r["components"]:
            assert c["spec"] == "CS150"

    def test_spec_filter_cs150_count(self, proj):
        """CS150 valves: PnPIDs 4,6,11,12 (4 rows)."""
        r = list_valves(str(proj), {"limit": 0, "spec": "CS150"})
        assert r["count"] == 4

    def test_spec_filter_ss150_count(self, proj):
        """SS150 valves: PnPID 5 only (1 row)."""
        r = list_valves(str(proj), {"limit": 0, "spec": "SS150"})
        assert r["count"] == 1

    def test_size_filter_forwarded_in(self, proj):
        """Valves filtered by size=2in must all have size='2\"'."""
        r = list_valves(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        for c in r["components"]:
            assert c["size"] == '2"'

    def test_size_filter_forwarded_mm(self, proj):
        """Valves filtered by size=50mm must all have size='50 mm'."""
        r = list_valves(str(proj), {"limit": 0, "size": {"value": 50.0, "unit": "mm"}})
        for c in r["components"]:
            assert c["size"] == "50 mm"

    def test_size_filter_mm_count(self, proj):
        """Only PnPID 10 is a 50mm valve."""
        r = list_valves(str(proj), {"limit": 0, "size": {"value": 50.0, "unit": "mm"}})
        assert r["count"] == 1

    def test_limit_forwarded(self, tmp_path):
        """limit=2 must cap the returned list and report omitted > 0."""
        rows = [
            (i, "L-001", f"TAG-V{i}", "Valves", f"Valvula {i}", "CS150", 2.0, "in")
            for i in range(1, 11)
        ]
        p = _make_project(tmp_path, "LIMIT_TEST", rows)
        r = list_valves(str(p), {"limit": 2})
        assert len(r["components"]) == 2
        assert r["count"] == 10
        assert r["omitted"] == 8

    def test_line_filter_echoed_in_filters(self, proj):
        """filters dict must include the normalized line value."""
        r = list_valves(str(proj), {"limit": 0, "line": "l-001"})
        assert r["filters"].get("line") == "L-001"

    def test_spec_filter_echoed_in_filters(self, proj):
        """filters dict must include the normalized spec value."""
        r = list_valves(str(proj), {"limit": 0, "spec": "cs150"})
        assert r["filters"].get("spec") == "CS150"

    def test_line_and_spec_combined(self, proj):
        """Combining line + spec filters: L-001 + CS150 valves only."""
        r = list_valves(str(proj), {"limit": 0, "line": "L-001", "spec": "CS150"})
        for c in r["components"]:
            assert c["line"] == "L-001"
            assert c["spec"] == "CS150"

    def test_nonexistent_line_returns_zero(self, proj):
        """A non-existent line filter yields count=0."""
        r = list_valves(str(proj), {"limit": 0, "line": "NONEXISTENT"})
        assert r["count"] == 0
        assert r["components"] == []

    def test_nonexistent_spec_returns_zero(self, proj):
        """A non-existent spec filter yields count=0."""
        r = list_valves(str(proj), {"limit": 0, "spec": "NONEXISTENT_SPEC"})
        assert r["count"] == 0


# ===========================================================================
# Part 5 – Output shape identical to list_components
# ===========================================================================


class TestOutputShape:
    def test_ok_flag_true(self, result_valves):
        assert result_valves["ok"] is True

    def test_required_top_level_keys(self, result_valves):
        required = ("ok", "project", "path", "limit", "filters",
                    "count", "omitted", "by_class", "components", "notes")
        for key in required:
            assert key in result_valves, f"Missing top-level key: {key}"

    def test_project_name(self, proj, result_valves):
        assert result_valves["project"] == proj.name

    def test_components_is_list(self, result_valves):
        assert isinstance(result_valves["components"], list)

    def test_by_class_is_list(self, result_valves):
        assert isinstance(result_valves["by_class"], list)

    def test_notes_is_list(self, result_valves):
        assert isinstance(result_valves["notes"], list)

    def test_notes_contain_net_limitation(self, result_valves):
        combined = " ".join(result_valves["notes"]).lower()
        assert "net" in combined or "plugin" in combined or "pnpid" in combined

    def test_each_component_has_required_keys(self, result_valves):
        required = ("pnpid", "class", "tag", "description", "spec", "size", "line")
        for comp in result_valves["components"]:
            for key in required:
                assert key in comp, f"Component {comp.get('pnpid')} missing key: {key}"

    def test_by_class_entries_have_class_and_count(self, result_valves):
        for entry in result_valves["by_class"]:
            assert "class" in entry
            assert "count" in entry

    def test_default_limit_is_50(self, proj):
        r = list_valves(str(proj))
        assert r["limit"] == 50

    def test_limit_zero_in_output(self, result_valves):
        assert result_valves["limit"] == 0


# ===========================================================================
# Part 6 – limit / omitted semantics
# ===========================================================================


@pytest.fixture
def many_valves_project(tmp_path: Path) -> Path:
    """Project with 60 valve components for limit/omitted tests."""
    rows = [
        (i, f"L-{i:03}", f"TAG-V{i:03}", "Valves", f"Valvula {i}", "CS150", 2.0, "in")
        for i in range(1, 61)
    ]
    return _make_project(tmp_path, "MANY_VALVES", rows)


class TestLimitOmitted:
    def test_default_limit_50_caps_output(self, many_valves_project):
        r = list_valves(str(many_valves_project))
        assert len(r["components"]) == 50
        assert r["count"] == 60
        assert r["omitted"] == 10

    def test_limit_zero_returns_all(self, many_valves_project):
        r = list_valves(str(many_valves_project), {"limit": 0})
        assert len(r["components"]) == 60
        assert r["count"] == 60
        assert r["omitted"] == 0

    def test_limit_custom(self, many_valves_project):
        r = list_valves(str(many_valves_project), {"limit": 10})
        assert len(r["components"]) == 10
        assert r["omitted"] == 50

    def test_omitted_formula(self, many_valves_project):
        r = list_valves(str(many_valves_project), {"limit": 25})
        assert r["omitted"] == r["count"] - len(r["components"])

    def test_no_omission_when_few_valves(self, proj):
        r = list_valves(str(proj), {"limit": 0})
        r2 = list_valves(str(proj))
        assert r2["omitted"] == 0
        assert len(r2["components"]) == r["count"]

    def test_limit_reflected_in_output(self, many_valves_project):
        r = list_valves(str(many_valves_project), {"limit": 15})
        assert r["limit"] == 15


# ===========================================================================
# Part 7 – Tag sanitization propagated
# ===========================================================================


class TestTagSanitization:
    def test_blank_tag_placeholder_becomes_none(self, proj):
        """PnPID 11 has Tag='?-?' -> must be None in output."""
        r = list_valves(str(proj), {"limit": 0})
        comp = next(c for c in r["components"] if c["pnpid"] == 11)
        assert comp["tag"] is None

    def test_real_tag_preserved(self, proj):
        """PnPID 4 has Tag='TAG-V1' -> must be preserved."""
        r = list_valves(str(proj), {"limit": 0})
        comp = next(c for c in r["components"] if c["pnpid"] == 4)
        assert comp["tag"] == "TAG-V1"

    def test_null_line_becomes_none(self, proj):
        """PnPID 12 has LineNumberTag=None -> line must be None in output."""
        r = list_valves(str(proj), {"limit": 0})
        comp = next(c for c in r["components"] if c["pnpid"] == 12)
        assert comp["line"] is None


# ===========================================================================
# Part 8 – Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, proj):
        db = proj / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        list_valves(str(proj), {"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, proj):
        r = list_valves(str(proj / "Piping.dcf"), {"limit": 0})
        assert r["ok"] is True
