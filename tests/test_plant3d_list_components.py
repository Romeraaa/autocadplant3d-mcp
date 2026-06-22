"""Tests for plant3d_query.list_components — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) and exercises list_components against them.
No real project databases are ever touched.

Key invariants verified:
- Canonical class mapping (pipe, valve, fitting, flange, instrument, support).
- Passthrough of non-canonical PartCategory values.
- Combination of multiple classes.
- Line filter (normalized TRIM+UPPER); without the filter, untagged lines are
  NOT excluded (list_components shows every component, not just tagged ones).
- Spec filter (normalized).
- Size filter (value+unit required; bare value skipped with note).
- Tag sanitization: NULL / '' / '?' / placeholder '?-?' -> None.
- by_class grouping with "(sin clase)" for NULL/empty PartCategory.
- limit/omitted (default 50, limit=0 no cap).
- Schema degradation: PipeRunComponent.Tag column absent -> tag=None + note.
- Output structure: required top-level keys, notes with standard NET note.
- Read-only guarantee.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import (
    _is_blank_tag,
    list_components,
)


# ===========================================================================
# Helpers: build minimal SQLite databases
# ===========================================================================


def _make_piping_dcf_full(
    path: Path,
    rows: list[tuple],
    *,
    include_tag_col: bool = True,
) -> None:
    """Create a Piping.dcf with PipeRunComponent + EngineeringItems.

    rows: (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription,
           Spec, NominalDiameter, NominalUnit)

    If include_tag_col is False the Tag column is omitted from
    PipeRunComponent to test graceful degradation.
    """
    con = sqlite3.connect(str(path))
    try:
        if include_tag_col:
            con.execute(
                "CREATE TABLE PipeRunComponent "
                "(PnPID INTEGER, LineNumberTag TEXT, Tag TEXT)"
            )
        else:
            con.execute(
                "CREATE TABLE PipeRunComponent "
                "(PnPID INTEGER, LineNumberTag TEXT)"
            )
        con.execute(
            "CREATE TABLE EngineeringItems "
            "(PnPID INTEGER, PartCategory TEXT, ShortDescription TEXT, "
            "Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
        )
        for pnpid, line_tag, comp_tag, cat, desc, spec, dia, unit in rows:
            if include_tag_col:
                con.execute(
                    "INSERT INTO PipeRunComponent "
                    "(PnPID, LineNumberTag, Tag) VALUES (?, ?, ?)",
                    (pnpid, line_tag, comp_tag),
                )
            else:
                con.execute(
                    "INSERT INTO PipeRunComponent "
                    "(PnPID, LineNumberTag) VALUES (?, ?)",
                    (pnpid, line_tag),
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


def _make_project(
    base: Path,
    name: str,
    rows: list[tuple],
    *,
    include_tag_col: bool = True,
) -> Path:
    """Build a minimal project folder with Project.xml + Piping.dcf."""
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf_full(proj / "Piping.dcf", rows, include_tag_col=include_tag_col)
    return proj


# ---------------------------------------------------------------------------
# Canonical test data
# (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription, Spec, dia, unit)
# ---------------------------------------------------------------------------
_ROWS = [
    # Pipes
    (1,  "L-001", "TAG-P1",  "Pipe",        "Tubo recto A",     "CS150",  2.0, "in"),
    (2,  "L-001", "TAG-P2",  "Pipe",        "Tubo recto B",     "CS150",  4.0, "in"),
    (3,  "L-002", "TAG-P3",  "Pipe",        "Tubo recto C",     "SS150",  2.0, "in"),
    # Valves
    (4,  "L-001", "TAG-V1",  "Valves",      "Valvula bola",     "CS150",  2.0, "in"),
    (5,  "L-002", "TAG-V2",  "Valves",      "Valvula compuerta", "SS150",  4.0, "in"),
    # Fittings
    (6,  "L-001", "TAG-F1",  "Fittings",    "Codo 90",          "CS150",  2.0, "in"),
    # Olet (also maps to "fitting" canonical)
    (7,  "L-002", "TAG-O1",  "Olet",        "Sockolet",         "SS150",  2.0, "in"),
    # Flanges
    (8,  "L-001", "TAG-FL1", "Flanges",     "Brida soldada",    "CS150",  2.0, "in"),
    # Instruments
    (9,  "L-002", "TAG-I1",  "Instruments", "Manometro",        "SS150",  2.0, "in"),
    # Support: PartCategory NULL  (no EI row) -> handled via LEFT JOIN; category=None
    (10, "L-001", "TAG-S1",  None,          None,               None,     None, None),
    # Support: PartCategory '' (empty)
    (11, "L-001", None,      "",            "Soporte generico", None,     None, None),
    # Support: PartCategory 'Default'
    (12, "L-002", "TAG-S3",  "Default",     "Soporte Default",  None,     None, None),
    # Passthrough (non-canonical PartCategory)
    (13, "L-001", "TAG-M1",  "Miscellaneous","Miscelánea",      "CS150",  4.0, "in"),
    # Untagged line (LineNumberTag = None) with valid component
    (14, None,    "TAG-X1",  "Pipe",        "Pipe sin linea",   "CS150",  2.0, "in"),
    # Component with blank tag placeholder '?-?'
    (15, "L-001", "?-?",     "Pipe",        "Pipe placeholder", "CS150",  2.0, "in"),
    # Component with tag '?'
    (16, "L-002", "?",       "Valves",      "Valve placeholder","SS150",  2.0, "in"),
    # Component with 50 mm diameter for size filter test
    (17, "L-003", "TAG-MM1", "Pipe",        "Tubo mm",          "CS300",  50.0, "mm"),
    # Another 50 mm pipe (different line) to validate unit separation
    (18, "L-003", "TAG-MM2", "Flanges",     "Brida mm",         "CS300",  50.0, "mm"),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Full synthetic project with canonical test data."""
    return _make_project(tmp_path, "COMP_TEST", _ROWS)


@pytest.fixture
def result_all(proj: Path) -> dict:
    """list_components with no filters and limit=0."""
    return list_components(str(proj), {"limit": 0})


# ===========================================================================
# Part 1 – Pure helper: _is_blank_tag
# ===========================================================================


class TestIsBlankTag:
    def test_none_is_blank(self):
        assert _is_blank_tag(None) is True

    def test_empty_string_is_blank(self):
        assert _is_blank_tag("") is True

    def test_single_question_mark_is_blank(self):
        assert _is_blank_tag("?") is True

    def test_question_dash_question_is_blank(self):
        assert _is_blank_tag("?-?") is True

    def test_question_spaces_dash_question_is_blank(self):
        assert _is_blank_tag("? - ?") is True

    def test_spaces_only_is_blank(self):
        assert _is_blank_tag("   ") is True

    def test_real_tag_is_not_blank(self):
        assert _is_blank_tag("TAG-001") is False

    def test_tag_containing_question_is_not_blank(self):
        # A valid tag that contains '?' but is not only ?/-/spaces
        assert _is_blank_tag('3"-P-001-ET?') is False

    def test_tag_with_dashes_only_is_blank(self):
        assert _is_blank_tag("---") is True

    def test_tag_with_letters_is_not_blank(self):
        assert _is_blank_tag("V-001") is False


# ===========================================================================
# Part 2 – Output structure
# ===========================================================================


class TestOutputStructure:
    def test_ok_flag_true(self, result_all):
        assert result_all["ok"] is True

    def test_project_name(self, proj, result_all):
        assert result_all["project"] == proj.name

    def test_required_top_level_keys(self, result_all):
        required = ("ok", "project", "path", "limit", "filters",
                    "count", "omitted", "by_class", "components", "notes")
        for key in required:
            assert key in result_all, f"Missing top-level key: {key}"

    def test_components_is_list(self, result_all):
        assert isinstance(result_all["components"], list)

    def test_by_class_is_list(self, result_all):
        assert isinstance(result_all["by_class"], list)

    def test_notes_is_list(self, result_all):
        assert isinstance(result_all["notes"], list)

    def test_notes_contain_net_limitation(self, result_all):
        combined = " ".join(result_all["notes"]).lower()
        assert "net" in combined or "plugin" in combined or "pnpid" in combined

    def test_each_component_has_required_keys(self, result_all):
        required = ("pnpid", "class", "tag", "description", "spec", "size", "line")
        for comp in result_all["components"]:
            for key in required:
                assert key in comp, f"Component {comp.get('pnpid')} missing key: {key}"

    def test_by_class_entries_have_class_and_count(self, result_all):
        for entry in result_all["by_class"]:
            assert "class" in entry
            assert "count" in entry

    def test_filters_empty_when_no_filters(self, result_all):
        assert result_all["filters"] == {}

    def test_limit_reflected_default(self, proj):
        r = list_components(str(proj))
        assert r["limit"] == 50


# ===========================================================================
# Part 3 – Class filter: canonical mapping
# ===========================================================================


class TestClassFilterCanonical:
    def test_pipe_returns_only_pipe_category(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["pipe"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Pipe"}

    def test_pipe_count(self, proj):
        # Rows with PartCategory='Pipe': PnPIDs 1,2,3,14,15,17
        r = list_components(str(proj), {"limit": 0, "classes": ["pipe"]})
        assert r["count"] == 6

    def test_valve_returns_only_valves_category(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["valve"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Valves"}

    def test_valve_count(self, proj):
        # PnPIDs 4,5,16 (16 has PartCategory='Valves' with tag='?')
        r = list_components(str(proj), {"limit": 0, "classes": ["valve"]})
        assert r["count"] == 3

    def test_fitting_returns_fittings_and_olet(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["fitting"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Fittings", "Olet"}

    def test_fitting_count(self, proj):
        # PnPIDs 6 (Fittings) + 7 (Olet)
        r = list_components(str(proj), {"limit": 0, "classes": ["fitting"]})
        assert r["count"] == 2

    def test_flange_returns_only_flanges_category(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["flange"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Flanges"}

    def test_flange_count(self, proj):
        # PnPIDs 8,18
        r = list_components(str(proj), {"limit": 0, "classes": ["flange"]})
        assert r["count"] == 2

    def test_instrument_returns_only_instruments_category(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["instrument"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Instruments"}

    def test_instrument_count(self, proj):
        # PnPID 9
        r = list_components(str(proj), {"limit": 0, "classes": ["instrument"]})
        assert r["count"] == 1

    def test_support_returns_null_empty_default_categories(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["support"]})
        # Must include PnPIDs 10 (None), 11 (''), 12 ('Default')
        pnpids = {c["pnpid"] for c in r["components"]}
        assert {10, 11, 12} <= pnpids

    def test_support_does_not_include_real_categories(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["support"]})
        # Supports can have PartCategory: None, '', or 'Default'.
        # Real classes like 'Pipe', 'Valves', 'Fittings', 'Flanges', 'Instruments'
        # must NOT appear when filtering by "support".
        real_classes = {"Pipe", "Valves", "Fittings", "Olet", "Flanges", "Instruments"}
        cats = {c["class"] for c in r["components"]}
        assert not (cats & real_classes), (
            f"Support filter returned components with non-support categories: "
            f"{cats & real_classes}"
        )

    def test_support_count(self, proj):
        # PnPIDs 10,11,12
        r = list_components(str(proj), {"limit": 0, "classes": ["support"]})
        assert r["count"] == 3


class TestClassFilterPassthrough:
    def test_miscellaneous_passthrough(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["Miscellaneous"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Miscellaneous"}

    def test_miscellaneous_count(self, proj):
        # PnPID 13
        r = list_components(str(proj), {"limit": 0, "classes": ["Miscellaneous"]})
        assert r["count"] == 1

    def test_passthrough_case_insensitive(self, proj):
        # 'miscellaneous' lowercase should match 'Miscellaneous' via _norm
        r = list_components(str(proj), {"limit": 0, "classes": ["miscellaneous"]})
        assert r["count"] == 1


class TestClassFilterCombined:
    def test_pipe_and_valve_combined(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["pipe", "valve"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Pipe", "Valves"}
        # 6 pipes + 3 valves
        assert r["count"] == 9

    def test_fitting_and_flange_combined(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["fitting", "flange"]})
        cats = {c["class"] for c in r["components"]}
        assert cats <= {"Fittings", "Olet", "Flanges"}
        # 2 fittings + 2 flanges
        assert r["count"] == 4

    def test_empty_classes_returns_all(self, proj):
        r_all = list_components(str(proj), {"limit": 0})
        r_empty = list_components(str(proj), {"limit": 0, "classes": []})
        # Both should return the same count (no filter)
        assert r_all["count"] == r_empty["count"]

    def test_filters_echo_classes(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["pipe", "valve"]})
        assert "classes" in r["filters"]
        assert set(r["filters"]["classes"]) == {"pipe", "valve"}


# ===========================================================================
# Part 4 – Line filter
# ===========================================================================


class TestLineFilter:
    def test_line_filter_exact_match(self, proj):
        r = list_components(str(proj), {"limit": 0, "line": "L-001"})
        for c in r["components"]:
            assert c["line"] == "L-001"

    def test_line_filter_case_insensitive(self, proj):
        # 'l-001' lowercase should match 'L-001'
        r_upper = list_components(str(proj), {"limit": 0, "line": "L-001"})
        r_lower = list_components(str(proj), {"limit": 0, "line": "l-001"})
        assert r_upper["count"] == r_lower["count"]

    def test_line_filter_with_spaces(self, proj):
        # Leading/trailing spaces must be trimmed before comparison
        r_plain = list_components(str(proj), {"limit": 0, "line": "L-001"})
        r_spaced = list_components(str(proj), {"limit": 0, "line": "  L-001  "})
        assert r_plain["count"] == r_spaced["count"]

    def test_line_filter_nonexistent_returns_zero(self, proj):
        r = list_components(str(proj), {"limit": 0, "line": "NONEXISTENT"})
        assert r["count"] == 0
        assert r["components"] == []

    def test_no_line_filter_includes_components_without_line(self, proj):
        # PnPID 14 has LineNumberTag=None; without line filter it should appear
        r = list_components(str(proj), {"limit": 0})
        pnpids = {c["pnpid"] for c in r["components"]}
        assert 14 in pnpids

    def test_line_filter_excludes_untagged_components(self, proj):
        # When a line filter is active, components with NULL LineNumberTag are excluded
        r = list_components(str(proj), {"limit": 0, "line": "L-001"})
        for c in r["components"]:
            assert c["line"] is not None

    def test_line_filter_echoed_normalized(self, proj):
        r = list_components(str(proj), {"limit": 0, "line": "l-001"})
        assert r["filters"]["line"] == "L-001"


# ===========================================================================
# Part 5 – Spec filter
# ===========================================================================


class TestSpecFilter:
    def test_spec_filter_exact_match(self, proj):
        r = list_components(str(proj), {"limit": 0, "spec": "CS150"})
        for c in r["components"]:
            assert c["spec"] == "CS150"

    def test_spec_filter_case_insensitive(self, proj):
        r_upper = list_components(str(proj), {"limit": 0, "spec": "CS150"})
        r_lower = list_components(str(proj), {"limit": 0, "spec": "cs150"})
        assert r_upper["count"] == r_lower["count"]

    def test_spec_filter_cs150_count(self, proj):
        # PnPIDs with Spec='CS150': 1,2,4,6,8,13,14,15  (8 rows)
        r = list_components(str(proj), {"limit": 0, "spec": "CS150"})
        assert r["count"] == 8

    def test_spec_filter_nonexistent_returns_zero(self, proj):
        r = list_components(str(proj), {"limit": 0, "spec": "NONEXISTENT_SPEC"})
        assert r["count"] == 0

    def test_spec_filter_echoed_normalized(self, proj):
        r = list_components(str(proj), {"limit": 0, "spec": "cs150"})
        assert r["filters"]["spec"] == "CS150"


# ===========================================================================
# Part 6 – Size filter
# ===========================================================================


class TestSizeFilter:
    def test_size_filter_in_2inch(self, proj):
        r = list_components(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        for c in r["components"]:
            assert c["size"] == '2"'

    def test_size_filter_mm_50(self, proj):
        r = list_components(str(proj), {"limit": 0, "size": {"value": 50.0, "unit": "mm"}})
        for c in r["components"]:
            assert c["size"] == "50 mm"

    def test_size_filter_in_does_not_return_mm(self, proj):
        # 2 in and 50 mm are different units; filtering by 2 in must not return mm rows
        r = list_components(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        pnpids = {c["pnpid"] for c in r["components"]}
        # PnPIDs 17,18 have 50mm, must be absent
        assert 17 not in pnpids
        assert 18 not in pnpids

    def test_size_filter_mm_does_not_return_in(self, proj):
        r = list_components(str(proj), {"limit": 0, "size": {"value": 50.0, "unit": "mm"}})
        pnpids = {c["pnpid"] for c in r["components"]}
        # Pipe PnPID 1 has 2in, must be absent
        assert 1 not in pnpids

    def test_size_filter_without_unit_is_ignored(self, proj):
        # Bare number without unit -> filter skipped, note added
        r_no_filter = list_components(str(proj), {"limit": 0})
        r_bare = list_components(str(proj), {"limit": 0, "size": 2.0})
        # Same count as unfiltered
        assert r_bare["count"] == r_no_filter["count"]

    def test_size_filter_without_unit_adds_note(self, proj):
        r = list_components(str(proj), {"limit": 0, "size": 2.0})
        combined = " ".join(r["notes"]).lower()
        assert "size" in combined or "unidad" in combined or "unit" in combined

    def test_size_filter_dict_without_unit_key_is_ignored(self, proj):
        # Dict with value but no unit -> filter skipped
        r_no_filter = list_components(str(proj), {"limit": 0})
        r_no_unit = list_components(str(proj), {"limit": 0, "size": {"value": 2.0}})
        assert r_no_unit["count"] == r_no_filter["count"]

    def test_size_filter_echoed_in_filters(self, proj):
        r = list_components(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        assert "size" in r["filters"]
        assert r["filters"]["size"]["value"] == 2.0

    def test_size_filter_unit_normalized_in_echo(self, proj):
        r = list_components(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "IN"}})
        # Unit should be normalized to uppercase in echo
        assert r["filters"]["size"]["unit"] == "IN"


# ===========================================================================
# Part 7 – Tag sanitization
# ===========================================================================


class TestTagSanitization:
    def test_null_tag_becomes_none(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # PnPID 11 has Tag=None
        comp = next(c for c in r["components"] if c["pnpid"] == 11)
        assert comp["tag"] is None

    def test_question_mark_tag_becomes_none(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # PnPID 16 has Tag='?'
        comp = next(c for c in r["components"] if c["pnpid"] == 16)
        assert comp["tag"] is None

    def test_placeholder_tag_becomes_none(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # PnPID 15 has Tag='?-?'
        comp = next(c for c in r["components"] if c["pnpid"] == 15)
        assert comp["tag"] is None

    def test_real_tag_preserved(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # PnPID 1 has Tag='TAG-P1'
        comp = next(c for c in r["components"] if c["pnpid"] == 1)
        assert comp["tag"] == "TAG-P1"

    def test_null_line_tag_becomes_none_in_output(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # PnPID 14 has LineNumberTag=None -> line should be None in output
        comp = next(c for c in r["components"] if c["pnpid"] == 14)
        assert comp["line"] is None


# ===========================================================================
# Part 8 – by_class grouping
# ===========================================================================


class TestByClass:
    def test_by_class_sorted_desc_by_count(self, result_all):
        counts = [entry["count"] for entry in result_all["by_class"]]
        assert counts == sorted(counts, reverse=True)

    def test_sin_clase_present_for_null_empty_categories(self, proj):
        # PnPIDs 10 (None) and 11 ('') map to '(sin clase)'; 12 ('Default') maps separately
        r = list_components(str(proj), {"limit": 0})
        labels = {entry["class"] for entry in r["by_class"]}
        assert "(sin clase)" in labels

    def test_sin_clase_count(self, proj):
        # PnPIDs 10 (None PartCategory) and 11 ('' PartCategory) -> "(sin clase)"
        r = list_components(str(proj), {"limit": 0})
        sin_clase = next(e for e in r["by_class"] if e["class"] == "(sin clase)")
        assert sin_clase["count"] == 2

    def test_pipe_count_in_by_class(self, proj):
        r = list_components(str(proj), {"limit": 0})
        pipe_entry = next((e for e in r["by_class"] if e["class"] == "Pipe"), None)
        assert pipe_entry is not None
        assert pipe_entry["count"] == 6  # PnPIDs 1,2,3,14,15,17

    def test_by_class_counts_sum_to_total(self, result_all):
        total = sum(e["count"] for e in result_all["by_class"])
        assert total == result_all["count"]

    def test_by_class_default_not_sin_clase(self, proj):
        # 'Default' PartCategory appears in by_class with its own label (not '(sin clase)')
        r = list_components(str(proj), {"limit": 0})
        labels = {entry["class"] for entry in r["by_class"]}
        assert "Default" in labels


# ===========================================================================
# Part 9 – limit and omitted
# ===========================================================================


@pytest.fixture
def many_components_project(tmp_path: Path) -> Path:
    """Project with 60 pipe components for limit/omitted tests."""
    rows = [
        (i, f"L-{i:03}", f"TAG-{i:03}", "Pipe", f"Tubo {i}", "CS150", 2.0, "in")
        for i in range(1, 61)
    ]
    return _make_project(tmp_path, "MANY_COMP", rows)


class TestLimitOmitted:
    def test_default_limit_50_caps_output(self, many_components_project):
        r = list_components(str(many_components_project))
        assert len(r["components"]) == 50
        assert r["count"] == 60
        assert r["omitted"] == 10

    def test_limit_zero_returns_all(self, many_components_project):
        r = list_components(str(many_components_project), {"limit": 0})
        assert len(r["components"]) == 60
        assert r["count"] == 60
        assert r["omitted"] == 0

    def test_limit_custom(self, many_components_project):
        r = list_components(str(many_components_project), {"limit": 10})
        assert len(r["components"]) == 10
        assert r["omitted"] == 50

    def test_omitted_never_silent(self, many_components_project):
        r = list_components(str(many_components_project), {"limit": 25})
        assert r["omitted"] == r["count"] - len(r["components"])

    def test_no_omission_when_few_components(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # 18 components < 50 default -> no omission
        r2 = list_components(str(proj))
        assert r2["omitted"] == 0
        assert len(r2["components"]) == r["count"]

    def test_limit_reflected_in_output(self, many_components_project):
        r = list_components(str(many_components_project), {"limit": 15})
        assert r["limit"] == 15


# ===========================================================================
# Part 10 – Schema degradation: PipeRunComponent.Tag absent
# ===========================================================================


class TestDegradationNoTagColumn:
    @pytest.fixture
    def no_tag_proj(self, tmp_path: Path) -> Path:
        rows = [
            (1, "L-001", None, "Pipe", "Tubo A", "CS150", 2.0, "in"),
            (2, "L-001", None, "Valves", "Valvula B", "CS150", 2.0, "in"),
        ]
        return _make_project(
            tmp_path, "NO_TAG_COL", rows, include_tag_col=False
        )

    def test_does_not_raise(self, no_tag_proj):
        r = list_components(str(no_tag_proj), {"limit": 0})
        assert r["ok"] is True

    def test_tag_is_none_for_all_components(self, no_tag_proj):
        r = list_components(str(no_tag_proj), {"limit": 0})
        for c in r["components"]:
            assert c["tag"] is None, f"Expected tag=None, got {c['tag']}"

    def test_note_mentions_tag_column_absent(self, no_tag_proj):
        r = list_components(str(no_tag_proj), {"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert "tag" in combined

    def test_other_fields_still_populated(self, no_tag_proj):
        r = list_components(str(no_tag_proj), {"limit": 0})
        for c in r["components"]:
            assert c["pnpid"] is not None
            assert c["class"] is not None
            assert c["spec"] is not None


# ===========================================================================
# Part 11 – Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, proj):
        db = proj / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        list_components(str(proj), {"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, proj):
        r = list_components(str(proj / "Piping.dcf"), {"limit": 0})
        assert r["ok"] is True

    def test_db_bytes_unchanged_degraded_no_tag_col(self, tmp_path):
        rows = [(1, "L-001", None, "Pipe", "T", "CS150", 2.0, "in")]
        proj = _make_project(tmp_path, "RO_DEGRADE", rows, include_tag_col=False)
        db = proj / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        list_components(str(proj), {"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before


# ===========================================================================
# Part 12 – Combined filters
# ===========================================================================


class TestCombinedFilters:
    def test_class_and_line_combined(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["pipe"], "line": "L-001"})
        for c in r["components"]:
            assert c["class"] == "Pipe"
            assert c["line"] == "L-001"

    def test_class_and_spec_combined(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["valve"], "spec": "SS150"})
        for c in r["components"]:
            assert c["class"] == "Valves"
            assert c["spec"] == "SS150"

    def test_class_and_size_combined(self, proj):
        r = list_components(
            str(proj), {"limit": 0, "classes": ["pipe"], "size": {"value": 2.0, "unit": "in"}}
        )
        for c in r["components"]:
            assert c["class"] == "Pipe"
            assert c["size"] == '2"'

    def test_multiple_filters_narrow_results(self, proj):
        r_all = list_components(str(proj), {"limit": 0})
        r_filtered = list_components(
            str(proj), {"limit": 0, "classes": ["pipe"], "spec": "CS150", "line": "L-001"}
        )
        assert r_filtered["count"] < r_all["count"]

    def test_filters_echo_all_active_filters(self, proj):
        r = list_components(
            str(proj),
            {
                "limit": 0,
                "classes": ["pipe"],
                "line": "L-001",
                "spec": "CS150",
                "size": {"value": 2.0, "unit": "in"},
            },
        )
        assert "classes" in r["filters"]
        assert "line" in r["filters"]
        assert "spec" in r["filters"]
        assert "size" in r["filters"]


# ===========================================================================
# Part 13 – Edge cases and correctness
# ===========================================================================


class TestEdgeCases:
    def test_empty_project_no_crash(self, tmp_path):
        proj = tmp_path / "EMPTY_PROJ"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        # Create Piping.dcf with empty tables
        db = proj / "Piping.dcf"
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE PipeRunComponent (PnPID INTEGER, LineNumberTag TEXT, Tag TEXT)"
            )
            con.execute(
                "CREATE TABLE EngineeringItems "
                "(PnPID INTEGER, PartCategory TEXT, ShortDescription TEXT, "
                "Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
            )
            con.commit()
        finally:
            con.close()

        r = list_components(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["count"] == 0
        assert r["components"] == []
        assert r["by_class"] == []

    def test_olet_included_in_fitting_class(self, proj):
        r = list_components(str(proj), {"limit": 0, "classes": ["fitting"]})
        pnpids = {c["pnpid"] for c in r["components"]}
        assert 7 in pnpids  # PnPID 7 is Olet

    def test_no_class_filter_returns_all_components(self, proj):
        r = list_components(str(proj), {"limit": 0})
        # All 18 rows from _ROWS
        assert r["count"] == 18

    def test_line_in_output_preserves_raw_value(self, proj):
        # When line filter is active, the raw LineNumberTag is preserved in output
        r = list_components(str(proj), {"limit": 0, "line": "L-001"})
        for c in r["components"]:
            assert c["line"] == "L-001"

    def test_fitting_canonical_key_case_insensitive(self, proj):
        # 'Fitting' (capital F) should also map to the canonical 'fitting' key
        r_lower = list_components(str(proj), {"limit": 0, "classes": ["fitting"]})
        r_upper = list_components(str(proj), {"limit": 0, "classes": ["Fitting"]})
        assert r_lower["count"] == r_upper["count"]
