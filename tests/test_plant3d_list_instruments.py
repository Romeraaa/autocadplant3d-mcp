"""Tests for plant3d_query.list_instruments — headless, no AutoCAD, no network.

list_instruments is a thin preset wrapper around list_components that pins
classes=["instrument"]. These tests verify:

1. Delegation with instrument class filter: only Instruments PartCategory returned.
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

from autocad_mcp.plant3d_query import list_instruments


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
# Canonical mixed dataset (pipes + valves + fittings + flanges + instruments)
# (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription, Spec, dia, unit)
# ---------------------------------------------------------------------------
_MIXED_ROWS = [
    # Pipes
    (1,  "L-001", "TAG-P1",  "Pipe",        "Tubo recto A",        "CS150",  2.0,  "in"),
    (2,  "L-001", "TAG-P2",  "Pipe",        "Tubo recto B",        "CS150",  4.0,  "in"),
    (3,  "L-002", "TAG-P3",  "Pipe",        "Tubo recto C",        "SS150",  2.0,  "in"),
    # Valves
    (4,  "L-001", "TAG-V1",  "Valves",      "Valvula bola",        "CS150",  2.0,  "in"),
    (5,  "L-002", "TAG-V2",  "Valves",      "Valvula compuerta",   "SS150",  4.0,  "in"),
    # Fittings
    (6,  "L-001", "TAG-F1",  "Fittings",    "Codo 90",             "CS150",  2.0,  "in"),
    # Flanges
    (7,  "L-002", "TAG-FL1", "Flanges",     "Brida soldada",       "SS150",  4.0,  "in"),
    # Instruments
    (8,  "L-001", "TAG-I1",  "Instruments", "Manometro",           "CS150",  2.0,  "in"),
    (9,  "L-002", "TAG-I2",  "Instruments", "Transmisor presion",  "SS150",  4.0,  "in"),
    (10, "L-001", "TAG-I3",  "Instruments", "Termometro",          "CS150",  2.0,  "in"),
    # Instrument on L-003 with SS300 spec and 50 mm
    (11, "L-003", "TAG-I4",  "Instruments", "Flujometro",          "SS300",  50.0, "mm"),
    # Instrument with blank tag placeholder
    (12, "L-001", "?-?",     "Instruments", "Instrumento placeholder", "CS150", 2.0, "in"),
    # Instrument without line tag
    (13, None,    "TAG-I5",  "Instruments", "Instrumento sin linea", "CS150", 2.0,  "in"),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Full synthetic project with canonical mixed test data."""
    return _make_project(tmp_path, "INSTRUMENT_TEST", _MIXED_ROWS)


@pytest.fixture
def result_instruments(proj: Path) -> dict:
    """list_instruments with no extra filters and limit=0."""
    return list_instruments(str(proj), {"limit": 0})


# ===========================================================================
# Part 1 – Delegation with instrument class filter
# ===========================================================================


class TestDelegationInstrumentFilter:
    def test_only_instruments_category_returned(self, result_instruments):
        """list_instruments returns ONLY components with PartCategory='Instruments'."""
        cats = {c["class"] for c in result_instruments["components"]}
        assert cats <= {"Instruments"}, f"Unexpected categories: {cats - {'Instruments'}}"

    def test_no_pipes_in_result(self, result_instruments):
        """Pipe components (PnPIDs 1-3) must not appear."""
        pnpids = {c["pnpid"] for c in result_instruments["components"]}
        assert 1 not in pnpids
        assert 2 not in pnpids
        assert 3 not in pnpids

    def test_no_valves_in_result(self, result_instruments):
        """Valve components (PnPIDs 4-5) must not appear."""
        pnpids = {c["pnpid"] for c in result_instruments["components"]}
        assert 4 not in pnpids
        assert 5 not in pnpids

    def test_no_fittings_in_result(self, result_instruments):
        """Fitting components (PnPID 6) must not appear."""
        pnpids = {c["pnpid"] for c in result_instruments["components"]}
        assert 6 not in pnpids

    def test_no_flanges_in_result(self, result_instruments):
        """Flange components (PnPID 7) must not appear."""
        pnpids = {c["pnpid"] for c in result_instruments["components"]}
        assert 7 not in pnpids

    def test_all_instrument_pnpids_present(self, result_instruments):
        """All instrument PnPIDs (8,9,10,11,12,13) must be present."""
        pnpids = {c["pnpid"] for c in result_instruments["components"]}
        assert {8, 9, 10, 11, 12, 13} <= pnpids

    def test_instrument_count(self, result_instruments):
        """Count must equal 6 (instruments: PnPIDs 8,9,10,11,12,13)."""
        assert result_instruments["count"] == 6

    def test_filters_classes_is_instrument(self, result_instruments):
        """filters dict must report classes=["instrument"]."""
        assert result_instruments["filters"].get("classes") == ["instrument"]

    def test_by_class_contains_only_instruments(self, result_instruments):
        """by_class grouping must only contain the 'Instruments' label."""
        labels = {entry["class"] for entry in result_instruments["by_class"]}
        assert labels <= {"Instruments"}


# ===========================================================================
# Part 2 – No mutation of caller's data dict
# ===========================================================================


class TestNoMutation:
    def test_data_dict_without_classes_not_mutated(self, proj):
        """Calling list_instruments must NOT add 'classes' to the original data dict."""
        data = {"limit": 10}
        list_instruments(str(proj), data)
        assert "classes" not in data, (
            "list_instruments mutated the caller's data dict by adding 'classes'"
        )

    def test_data_dict_with_classes_not_mutated(self, proj):
        """The original 'classes' value in the caller's dict must be preserved."""
        data = {"classes": ["valve"], "limit": 0}
        list_instruments(str(proj), data)
        # The original dict must still have ["valve"], not ["instrument"]
        assert data["classes"] == ["valve"], (
            "list_instruments mutated the caller's data dict, overwriting 'classes'"
        )

    def test_none_data_does_not_crash(self, proj):
        """Passing data=None must not raise."""
        r = list_instruments(str(proj), None)
        assert r["ok"] is True

    def test_empty_data_does_not_crash(self, proj):
        """Passing data={} must not raise."""
        r = list_instruments(str(proj), {})
        assert r["ok"] is True


# ===========================================================================
# Part 3 – classes from caller is ignored/overridden
# ===========================================================================


class TestClassesOverride:
    def test_pipe_classes_overridden_to_instrument(self, proj):
        """Even if caller passes classes=["pipe"], result must be only instruments."""
        r = list_instruments(str(proj), {"classes": ["pipe"], "limit": 0})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Instruments"}, f"Expected only Instruments, got: {cats}"

    def test_valve_classes_overridden_to_instrument(self, proj):
        """Even if caller passes classes=["valve"], result must be only instruments."""
        r = list_instruments(str(proj), {"classes": ["valve"], "limit": 0})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Instruments"}

    def test_empty_classes_overridden_to_instrument(self, proj):
        """Even if caller passes classes=[], result must be only instruments."""
        r = list_instruments(str(proj), {"classes": [], "limit": 0})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Instruments"}

    def test_filters_echo_classes_instrument(self, proj):
        """filters.classes must always be ['instrument'] regardless of input."""
        for caller_classes in [["pipe"], ["valve", "flange"], [], ["instrument"]]:
            r = list_instruments(str(proj), {"classes": caller_classes, "limit": 0})
            assert r["filters"].get("classes") == ["instrument"], (
                f"With caller classes={caller_classes!r}, "
                f"got filters.classes={r['filters'].get('classes')!r}"
            )


# ===========================================================================
# Part 4 – Other filters forwarded intact
# ===========================================================================


class TestFilterForwarding:
    def test_line_filter_forwarded(self, proj):
        """Instruments filtered by line=L-001 must all belong to L-001."""
        r = list_instruments(str(proj), {"limit": 0, "line": "L-001"})
        # Only instruments on L-001: PnPIDs 8, 10, 12
        assert r["count"] == 3
        for c in r["components"]:
            assert c["line"] == "L-001"

    def test_spec_filter_forwarded(self, proj):
        """Instruments filtered by spec=CS150 must all have spec=CS150."""
        r = list_instruments(str(proj), {"limit": 0, "spec": "CS150"})
        for c in r["components"]:
            assert c["spec"] == "CS150"

    def test_spec_filter_cs150_count(self, proj):
        """CS150 instruments: PnPIDs 8,10,12,13 (4 rows)."""
        r = list_instruments(str(proj), {"limit": 0, "spec": "CS150"})
        assert r["count"] == 4

    def test_spec_filter_ss150_count(self, proj):
        """SS150 instruments: PnPID 9 only (1 row)."""
        r = list_instruments(str(proj), {"limit": 0, "spec": "SS150"})
        assert r["count"] == 1

    def test_size_filter_forwarded_in(self, proj):
        """Instruments filtered by size=2in must all have size='2\"'."""
        r = list_instruments(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        for c in r["components"]:
            assert c["size"] == '2"'

    def test_size_filter_forwarded_mm(self, proj):
        """Instruments filtered by size=50mm must all have size='50 mm'."""
        r = list_instruments(str(proj), {"limit": 0, "size": {"value": 50.0, "unit": "mm"}})
        for c in r["components"]:
            assert c["size"] == "50 mm"

    def test_size_filter_mm_count(self, proj):
        """Only PnPID 11 is a 50mm instrument."""
        r = list_instruments(str(proj), {"limit": 0, "size": {"value": 50.0, "unit": "mm"}})
        assert r["count"] == 1

    def test_limit_forwarded(self, tmp_path):
        """limit=2 must cap the returned list and report omitted > 0."""
        rows = [
            (i, "L-001", f"TAG-I{i}", "Instruments", f"Instrumento {i}", "CS150", 2.0, "in")
            for i in range(1, 11)
        ]
        p = _make_project(tmp_path, "LIMIT_TEST", rows)
        r = list_instruments(str(p), {"limit": 2})
        assert len(r["components"]) == 2
        assert r["count"] == 10
        assert r["omitted"] == 8

    def test_line_filter_echoed_in_filters(self, proj):
        """filters dict must include the normalized line value."""
        r = list_instruments(str(proj), {"limit": 0, "line": "l-001"})
        assert r["filters"].get("line") == "L-001"

    def test_spec_filter_echoed_in_filters(self, proj):
        """filters dict must include the normalized spec value."""
        r = list_instruments(str(proj), {"limit": 0, "spec": "cs150"})
        assert r["filters"].get("spec") == "CS150"

    def test_line_and_spec_combined(self, proj):
        """Combining line + spec filters: L-001 + CS150 instruments only."""
        r = list_instruments(str(proj), {"limit": 0, "line": "L-001", "spec": "CS150"})
        for c in r["components"]:
            assert c["line"] == "L-001"
            assert c["spec"] == "CS150"

    def test_nonexistent_line_returns_zero(self, proj):
        """A non-existent line filter yields count=0."""
        r = list_instruments(str(proj), {"limit": 0, "line": "NONEXISTENT"})
        assert r["count"] == 0
        assert r["components"] == []

    def test_nonexistent_spec_returns_zero(self, proj):
        """A non-existent spec filter yields count=0."""
        r = list_instruments(str(proj), {"limit": 0, "spec": "NONEXISTENT_SPEC"})
        assert r["count"] == 0


# ===========================================================================
# Part 5 – Output shape identical to list_components
# ===========================================================================


class TestOutputShape:
    def test_ok_flag_true(self, result_instruments):
        assert result_instruments["ok"] is True

    def test_required_top_level_keys(self, result_instruments):
        required = ("ok", "project", "path", "limit", "filters",
                    "count", "omitted", "by_class", "components", "notes")
        for key in required:
            assert key in result_instruments, f"Missing top-level key: {key}"

    def test_project_name(self, proj, result_instruments):
        assert result_instruments["project"] == proj.name

    def test_components_is_list(self, result_instruments):
        assert isinstance(result_instruments["components"], list)

    def test_by_class_is_list(self, result_instruments):
        assert isinstance(result_instruments["by_class"], list)

    def test_notes_is_list(self, result_instruments):
        assert isinstance(result_instruments["notes"], list)

    def test_notes_contain_net_limitation(self, result_instruments):
        combined = " ".join(result_instruments["notes"]).lower()
        assert "net" in combined or "plugin" in combined or "pnpid" in combined

    def test_each_component_has_required_keys(self, result_instruments):
        required = ("pnpid", "class", "tag", "description", "spec", "size", "line")
        for comp in result_instruments["components"]:
            for key in required:
                assert key in comp, f"Component {comp.get('pnpid')} missing key: {key}"

    def test_by_class_entries_have_class_and_count(self, result_instruments):
        for entry in result_instruments["by_class"]:
            assert "class" in entry
            assert "count" in entry

    def test_default_limit_is_50(self, proj):
        r = list_instruments(str(proj))
        assert r["limit"] == 50

    def test_limit_zero_in_output(self, result_instruments):
        assert result_instruments["limit"] == 0


# ===========================================================================
# Part 6 – limit / omitted semantics
# ===========================================================================


@pytest.fixture
def many_instruments_project(tmp_path: Path) -> Path:
    """Project with 60 instrument components for limit/omitted tests."""
    rows = [
        (i, f"L-{i:03}", f"TAG-I{i:03}", "Instruments", f"Instrumento {i}", "CS150", 2.0, "in")
        for i in range(1, 61)
    ]
    return _make_project(tmp_path, "MANY_INSTRUMENTS", rows)


class TestLimitOmitted:
    def test_default_limit_50_caps_output(self, many_instruments_project):
        r = list_instruments(str(many_instruments_project))
        assert len(r["components"]) == 50
        assert r["count"] == 60
        assert r["omitted"] == 10

    def test_limit_zero_returns_all(self, many_instruments_project):
        r = list_instruments(str(many_instruments_project), {"limit": 0})
        assert len(r["components"]) == 60
        assert r["count"] == 60
        assert r["omitted"] == 0

    def test_limit_custom(self, many_instruments_project):
        r = list_instruments(str(many_instruments_project), {"limit": 10})
        assert len(r["components"]) == 10
        assert r["omitted"] == 50

    def test_omitted_formula(self, many_instruments_project):
        r = list_instruments(str(many_instruments_project), {"limit": 25})
        assert r["omitted"] == r["count"] - len(r["components"])

    def test_no_omission_when_few_instruments(self, proj):
        r = list_instruments(str(proj), {"limit": 0})
        r2 = list_instruments(str(proj))
        assert r2["omitted"] == 0
        assert len(r2["components"]) == r["count"]

    def test_limit_reflected_in_output(self, many_instruments_project):
        r = list_instruments(str(many_instruments_project), {"limit": 15})
        assert r["limit"] == 15


# ===========================================================================
# Part 7 – Tag sanitization propagated
# ===========================================================================


class TestTagSanitization:
    def test_blank_tag_placeholder_becomes_none(self, proj):
        """PnPID 12 has Tag='?-?' -> must be None in output."""
        r = list_instruments(str(proj), {"limit": 0})
        comp = next(c for c in r["components"] if c["pnpid"] == 12)
        assert comp["tag"] is None

    def test_real_tag_preserved(self, proj):
        """PnPID 8 has Tag='TAG-I1' -> must be preserved."""
        r = list_instruments(str(proj), {"limit": 0})
        comp = next(c for c in r["components"] if c["pnpid"] == 8)
        assert comp["tag"] == "TAG-I1"

    def test_null_line_becomes_none(self, proj):
        """PnPID 13 has LineNumberTag=None -> line must be None in output."""
        r = list_instruments(str(proj), {"limit": 0})
        comp = next(c for c in r["components"] if c["pnpid"] == 13)
        assert comp["line"] is None


# ===========================================================================
# Part 8 – Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, proj):
        db = proj / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        list_instruments(str(proj), {"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, proj):
        r = list_instruments(str(proj / "Piping.dcf"), {"limit": 0})
        assert r["ok"] is True
