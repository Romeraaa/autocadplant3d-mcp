"""Tests for plant3d_query.bom — headless, no AutoCAD, no network.

bom() is an aggregation layer on top of list_components: it calls
list_components with no inner cap and then groups the returned components in
Python by the tuple (class, spec, size, description).

These tests verify:

1. Grouping by (class, spec, size, description): repeats collapse, distinct
   combinations become separate BOM lines.
2. total_components = sum of all quantities (before any limit cap).
3. Class None / '' both collapse into "(sin clase)" label.
4. None in spec/size/description is preserved as-is (not replaced with a
   placeholder) and participates in grouping correctly.
5. Ordering: lines sorted by class asc, quantity desc, description asc
   (None treated as empty string for ordering); by_class sorted count desc.
6. limit / omitted: limit caps BOM lines (not components); limit=0 no cap;
   line_count reflects total lines before the cap.
7. Filter propagation: classes / line / spec / size scope the BOM correctly;
   filters key echoes the active filters.
8. Notes: the .NET-location note is NOT propagated; the size-without-unit
   note IS propagated when relevant; the grouping note is always present.
9. Output structure: all required top-level keys present for bom().
10. Server dispatch: operation="bom" calls plant3d_query.bom (smoke test).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import bom


# ===========================================================================
# Helpers: build minimal SQLite databases (same pattern as list_components)
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
# Canonical mixed dataset for grouping tests
# (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription, Spec, dia, unit)
#
# Grouping tuples that should collapse:
#   Group A: (Pipe, CS150, 2", "Tubo recto")       — PnPIDs 1, 2, 3  → qty 3
#   Group B: (Valves, CS150, 2", "Valvula bola")    — PnPIDs 4, 5     → qty 2
#   Group C: (Valves, SS150, 4", "Valvula compuerta")— PnPID 6         → qty 1
#   Group D: (Fittings, CS150, 2", "Codo 90")       — PnPID 7         → qty 1
#   Group E: (Flanges, SS150, 4", "Brida soldada")  — PnPID 8, 9      → qty 2
#   Group F: (None/empty → "(sin clase)", None, None, None) — PnPIDs 10,11,12 → qty 3
#   Group G: (Instruments, CS150, 50 mm, "Manometro")— PnPID 13       → qty 1
# ---------------------------------------------------------------------------
_ROWS_GROUPED = [
    # Group A: 3 identical pipes (class+spec+size+desc all same)
    (1,  "L-001", "TAG-P1",  "Pipe",    "Tubo recto",        "CS150", 2.0,  "in"),
    (2,  "L-001", "TAG-P2",  "Pipe",    "Tubo recto",        "CS150", 2.0,  "in"),
    (3,  "L-002", "TAG-P3",  "Pipe",    "Tubo recto",        "CS150", 2.0,  "in"),
    # Group B: 2 identical valves
    (4,  "L-001", "TAG-V1",  "Valves",  "Valvula bola",      "CS150", 2.0,  "in"),
    (5,  "L-001", "TAG-V2",  "Valves",  "Valvula bola",      "CS150", 2.0,  "in"),
    # Group C: 1 valve (different desc+spec+size)
    (6,  "L-002", "TAG-V3",  "Valves",  "Valvula compuerta", "SS150", 4.0,  "in"),
    # Group D: 1 fitting
    (7,  "L-001", "TAG-F1",  "Fittings","Codo 90",           "CS150", 2.0,  "in"),
    # Group E: 2 identical flanges (different line, same tuple)
    (8,  "L-001", "TAG-FL1", "Flanges", "Brida soldada",     "SS150", 4.0,  "in"),
    (9,  "L-002", "TAG-FL2", "Flanges", "Brida soldada",     "SS150", 4.0,  "in"),
    # Group F: class=None, class='', class=None → all collapse to "(sin clase)"
    # (spec=None, size=None, desc=None)
    (10, "L-001", "TAG-S1",  None,       None,               None,    None, None),
    (11, "L-001",  None,     "",         None,               None,    None, None),
    (12, "L-002", "TAG-S3",  None,       None,               None,    None, None),
    # Group G: instrument with 50 mm (different unit from "in")
    (13, "L-003", "TAG-I1",  "Instruments","Manometro",      "CS150", 50.0, "mm"),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Full synthetic project with canonical grouped test data."""
    return _make_project(tmp_path, "BOM_TEST", _ROWS_GROUPED)


@pytest.fixture
def result_bom(proj: Path) -> dict:
    """bom() with no filters and limit=0."""
    return bom(str(proj), {"limit": 0})


# ===========================================================================
# Part 1 – Output structure
# ===========================================================================


class TestOutputStructure:
    def test_ok_flag_true(self, result_bom):
        assert result_bom["ok"] is True

    def test_required_top_level_keys(self, result_bom):
        required = (
            "ok", "project", "path", "limit", "filters",
            "total_components", "line_count", "omitted", "by_class", "bom", "notes",
        )
        for key in required:
            assert key in result_bom, f"Missing top-level key: {key}"

    def test_project_name(self, proj, result_bom):
        assert result_bom["project"] == proj.name

    def test_bom_is_list(self, result_bom):
        assert isinstance(result_bom["bom"], list)

    def test_by_class_is_list(self, result_bom):
        assert isinstance(result_bom["by_class"], list)

    def test_notes_is_list(self, result_bom):
        assert isinstance(result_bom["notes"], list)

    def test_each_bom_line_has_required_keys(self, result_bom):
        required = ("class", "spec", "size", "description", "quantity")
        for line in result_bom["bom"]:
            for key in required:
                assert key in line, f"BOM line missing key: {key}"

    def test_by_class_entries_have_class_and_count(self, result_bom):
        for entry in result_bom["by_class"]:
            assert "class" in entry
            assert "count" in entry

    def test_filters_empty_when_no_filters(self, result_bom):
        assert result_bom["filters"] == {}

    def test_default_limit_is_50(self, proj):
        r = bom(str(proj))
        assert r["limit"] == 50

    def test_limit_zero_reflected(self, result_bom):
        assert result_bom["limit"] == 0

    def test_quantity_is_positive_int(self, result_bom):
        for line in result_bom["bom"]:
            assert isinstance(line["quantity"], int)
            assert line["quantity"] > 0


# ===========================================================================
# Part 2 – Grouping: repeats collapse, distinct tuples → separate lines
# ===========================================================================


class TestGrouping:
    def test_repeated_pipes_collapse_to_one_line(self, result_bom):
        """PnPIDs 1,2,3 share (Pipe, CS150, 2", "Tubo recto") → single BOM line."""
        pipe_lines = [
            b for b in result_bom["bom"]
            if b["class"] == "Pipe" and b["description"] == "Tubo recto"
        ]
        assert len(pipe_lines) == 1, (
            f"Expected 1 BOM line for Pipe/Tubo recto, got {len(pipe_lines)}"
        )

    def test_repeated_pipes_quantity_is_3(self, result_bom):
        pipe_line = next(
            b for b in result_bom["bom"]
            if b["class"] == "Pipe" and b["description"] == "Tubo recto"
        )
        assert pipe_line["quantity"] == 3

    def test_repeated_valves_collapse_to_one_line(self, result_bom):
        """PnPIDs 4,5 share (Valves, CS150, 2", "Valvula bola") → single BOM line."""
        valve_bola = [
            b for b in result_bom["bom"]
            if b["class"] == "Valves" and b["description"] == "Valvula bola"
        ]
        assert len(valve_bola) == 1

    def test_repeated_valves_quantity_is_2(self, result_bom):
        valve_bola = next(
            b for b in result_bom["bom"]
            if b["class"] == "Valves" and b["description"] == "Valvula bola"
        )
        assert valve_bola["quantity"] == 2

    def test_distinct_tuples_produce_separate_lines(self, result_bom):
        """Valvula bola and Valvula compuerta differ → must be two separate lines."""
        valve_lines = [b for b in result_bom["bom"] if b["class"] == "Valves"]
        descs = {b["description"] for b in valve_lines}
        assert "Valvula bola" in descs
        assert "Valvula compuerta" in descs

    def test_repeated_flanges_collapse(self, result_bom):
        """PnPIDs 8,9 share (Flanges, SS150, 4", "Brida soldada") → qty=2."""
        flange_line = next(
            b for b in result_bom["bom"]
            if b["class"] == "Flanges" and b["description"] == "Brida soldada"
        )
        assert flange_line["quantity"] == 2

    def test_total_bom_lines_count(self, result_bom):
        """With _ROWS_GROUPED there must be exactly 7 distinct BOM lines."""
        # Group A (Pipe/Tubo recto), B (Valve/Bola), C (Valve/Compuerta),
        # D (Fittings/Codo90), E (Flanges/Brida), F ((sin clase)/None/None),
        # G (Instruments/Manometro)
        assert result_bom["line_count"] == 7

    def test_line_count_matches_bom_list_when_no_limit(self, result_bom):
        assert result_bom["line_count"] == len(result_bom["bom"])


# ===========================================================================
# Part 3 – total_components = sum of quantities
# ===========================================================================


class TestTotalComponents:
    def test_total_components_equals_sum_of_quantities(self, result_bom):
        qty_sum = sum(b["quantity"] for b in result_bom["bom"])
        assert result_bom["total_components"] == qty_sum

    def test_total_components_equals_row_count(self, result_bom):
        """There are 13 rows in _ROWS_GROUPED."""
        assert result_bom["total_components"] == 13

    def test_total_components_unaffected_by_limit(self, proj):
        """total_components counts ALL components even when limit caps the BOM lines."""
        r_all = bom(str(proj), {"limit": 0})
        r_capped = bom(str(proj), {"limit": 3})
        # total_components must be the same regardless of the line cap
        assert r_capped["total_components"] == r_all["total_components"]


# ===========================================================================
# Part 4 – Class None / '' collapse into "(sin clase)"
# ===========================================================================


class TestSinClaseCollapse:
    def test_sin_clase_label_present_in_bom(self, result_bom):
        """PnPIDs 10 (None), 11 (''), 12 (None) all map to '(sin clase)'."""
        sin_clase_lines = [b for b in result_bom["bom"] if b["class"] == "(sin clase)"]
        assert len(sin_clase_lines) >= 1, "Expected at least one (sin clase) BOM line"

    def test_sin_clase_quantity_is_3(self, result_bom):
        """The three null/empty-class components should sum to quantity=3."""
        sin_clase_line = next(
            b for b in result_bom["bom"] if b["class"] == "(sin clase)"
        )
        assert sin_clase_line["quantity"] == 3

    def test_none_and_empty_class_in_same_group(self, tmp_path):
        """A row with class=None and one with class='' and same spec/size/desc
        must end up in the SAME BOM line."""
        rows = [
            (1, "L-001", "TAG-S1", None, "Soporte X", "CS150", 2.0, "in"),
            (2, "L-001", "TAG-S2", "",   "Soporte X", "CS150", 2.0, "in"),
        ]
        proj = _make_project(tmp_path, "SINCLASE_TEST", rows)
        r = bom(str(proj), {"limit": 0})
        sin_clase_lines = [b for b in r["bom"] if b["class"] == "(sin clase)"]
        assert len(sin_clase_lines) == 1, (
            "None and '' class with same rest-of-tuple must collapse into one BOM line"
        )
        assert sin_clase_lines[0]["quantity"] == 2

    def test_sin_clase_present_in_by_class(self, result_bom):
        labels = {entry["class"] for entry in result_bom["by_class"]}
        assert "(sin clase)" in labels

    def test_sin_clase_count_in_by_class(self, result_bom):
        sin_clase = next(
            e for e in result_bom["by_class"] if e["class"] == "(sin clase)"
        )
        assert sin_clase["count"] == 3


# ===========================================================================
# Part 5 – None in spec / size / description preserved and grouping correct
# ===========================================================================


class TestNoneFieldsPreserved:
    def test_none_spec_in_bom_line(self, result_bom):
        """The (sin clase) group has spec=None; output must be None, not a string."""
        sin_clase_line = next(
            b for b in result_bom["bom"] if b["class"] == "(sin clase)"
        )
        assert sin_clase_line["spec"] is None

    def test_none_size_in_bom_line(self, result_bom):
        sin_clase_line = next(
            b for b in result_bom["bom"] if b["class"] == "(sin clase)"
        )
        assert sin_clase_line["size"] is None

    def test_none_description_in_bom_line(self, result_bom):
        sin_clase_line = next(
            b for b in result_bom["bom"] if b["class"] == "(sin clase)"
        )
        assert sin_clase_line["description"] is None

    def test_none_spec_groups_correctly(self, tmp_path):
        """Two rows with spec=None and same rest-of-tuple → single BOM line, qty=2."""
        rows = [
            (1, "L-001", "TAG-X1", "Pipe", "Tubo sin spec", None, 2.0, "in"),
            (2, "L-001", "TAG-X2", "Pipe", "Tubo sin spec", None, 2.0, "in"),
        ]
        proj = _make_project(tmp_path, "NONE_SPEC_TEST", rows)
        r = bom(str(proj), {"limit": 0})
        pipe_lines = [b for b in r["bom"] if b["class"] == "Pipe"]
        assert len(pipe_lines) == 1
        assert pipe_lines[0]["spec"] is None
        assert pipe_lines[0]["quantity"] == 2

    def test_none_vs_real_spec_are_different_groups(self, tmp_path):
        """spec=None and spec='CS150' with same rest → two separate BOM lines."""
        rows = [
            (1, "L-001", "TAG-X1", "Pipe", "Tubo A", None,    2.0, "in"),
            (2, "L-001", "TAG-X2", "Pipe", "Tubo A", "CS150", 2.0, "in"),
        ]
        proj = _make_project(tmp_path, "NONE_VS_SPEC_TEST", rows)
        r = bom(str(proj), {"limit": 0})
        pipe_lines = [b for b in r["bom"] if b["class"] == "Pipe"]
        assert len(pipe_lines) == 2


# ===========================================================================
# Part 6 – Ordering
# ===========================================================================


class TestOrdering:
    def test_bom_lines_ordered_by_class_asc(self, result_bom):
        """BOM lines must be sorted by class label ascending."""
        classes = [b["class"] for b in result_bom["bom"]]
        assert classes == sorted(classes)

    def test_same_class_ordered_by_quantity_desc(self, tmp_path):
        """Within a class, lines with higher quantity come first."""
        rows = [
            # Valves: Valvula bola × 3, Valvula aguja × 1
            (1, "L-001", "TAG-V1", "Valves", "Valvula bola",  "CS150", 2.0, "in"),
            (2, "L-001", "TAG-V2", "Valves", "Valvula bola",  "CS150", 2.0, "in"),
            (3, "L-001", "TAG-V3", "Valves", "Valvula bola",  "CS150", 2.0, "in"),
            (4, "L-001", "TAG-V4", "Valves", "Valvula aguja", "CS150", 2.0, "in"),
        ]
        proj = _make_project(tmp_path, "ORDER_TEST", rows)
        r = bom(str(proj), {"limit": 0})
        valve_lines = [b for b in r["bom"] if b["class"] == "Valves"]
        assert valve_lines[0]["quantity"] >= valve_lines[-1]["quantity"]
        # First line must be Valvula bola (qty=3)
        assert valve_lines[0]["description"] == "Valvula bola"
        assert valve_lines[0]["quantity"] == 3

    def test_same_class_same_qty_ordered_by_desc_asc(self, tmp_path):
        """When class and quantity are equal, description ascending is the tiebreak."""
        rows = [
            (1, "L-001", "TAG-V1", "Valves", "Zeta valve",  "CS150", 2.0, "in"),
            (2, "L-001", "TAG-V2", "Valves", "Alpha valve", "CS150", 2.0, "in"),
        ]
        proj = _make_project(tmp_path, "DESC_ORDER_TEST", rows)
        r = bom(str(proj), {"limit": 0})
        valve_lines = [b for b in r["bom"] if b["class"] == "Valves"]
        assert valve_lines[0]["description"] == "Alpha valve"
        assert valve_lines[1]["description"] == "Zeta valve"

    def test_by_class_ordered_count_desc(self, result_bom):
        """by_class entries must be sorted by count descending."""
        counts = [entry["count"] for entry in result_bom["by_class"]]
        assert counts == sorted(counts, reverse=True)


# ===========================================================================
# Part 7 – limit / omitted
# ===========================================================================


@pytest.fixture
def many_bom_lines_project(tmp_path: Path) -> Path:
    """Project with 60 unique BOM lines (each component is unique) for limit tests."""
    rows = [
        (i, f"L-{i:03}", f"TAG-{i:03}", "Pipe", f"Tubo {i}", "CS150", float(i), "in")
        for i in range(1, 61)
    ]
    return _make_project(tmp_path, "MANY_BOM", rows)


class TestLimitOmitted:
    def test_default_limit_50_caps_bom_lines(self, many_bom_lines_project):
        r = bom(str(many_bom_lines_project))
        assert len(r["bom"]) == 50
        assert r["line_count"] == 60
        assert r["omitted"] == 10

    def test_limit_zero_returns_all_bom_lines(self, many_bom_lines_project):
        r = bom(str(many_bom_lines_project), {"limit": 0})
        assert len(r["bom"]) == 60
        assert r["line_count"] == 60
        assert r["omitted"] == 0

    def test_limit_custom_caps_bom_lines(self, many_bom_lines_project):
        r = bom(str(many_bom_lines_project), {"limit": 10})
        assert len(r["bom"]) == 10
        assert r["omitted"] == 50

    def test_omitted_formula(self, many_bom_lines_project):
        r = bom(str(many_bom_lines_project), {"limit": 25})
        assert r["omitted"] == r["line_count"] - len(r["bom"])

    def test_no_omission_when_few_lines(self, proj):
        r = bom(str(proj), {"limit": 0})
        r2 = bom(str(proj))
        assert r2["omitted"] == 0
        assert len(r2["bom"]) == r["line_count"]

    def test_limit_reflected_in_output(self, many_bom_lines_project):
        r = bom(str(many_bom_lines_project), {"limit": 15})
        assert r["limit"] == 15

    def test_line_count_always_total_before_cap(self, many_bom_lines_project):
        """line_count must reflect the total distinct BOM lines, regardless of limit."""
        r = bom(str(many_bom_lines_project), {"limit": 5})
        assert r["line_count"] == 60

    def test_total_components_unaffected_by_bom_limit(self, many_bom_lines_project):
        """total_components = all matched components (60 here), not capped by limit."""
        r = bom(str(many_bom_lines_project), {"limit": 5})
        assert r["total_components"] == 60


# ===========================================================================
# Part 8 – Filter propagation
# ===========================================================================


class TestFilterPropagation:
    def test_line_filter_scopes_bom(self, proj):
        """With line=L-001, BOM covers only components on that line."""
        r_all = bom(str(proj), {"limit": 0})
        r_line = bom(str(proj), {"limit": 0, "line": "L-001"})
        assert r_line["total_components"] < r_all["total_components"]

    def test_line_filter_total_components(self, proj):
        """L-001 in _ROWS_GROUPED: PnPIDs 1,2,4,5,7,8,10,11 = 8 components."""
        r = bom(str(proj), {"limit": 0, "line": "L-001"})
        assert r["total_components"] == 8

    def test_line_filter_echoed_in_filters(self, proj):
        r = bom(str(proj), {"limit": 0, "line": "l-001"})
        assert r["filters"].get("line") == "L-001"

    def test_classes_filter_valve_scopes_bom(self, proj):
        """With classes=['valve'], only valve rows appear in BOM."""
        r = bom(str(proj), {"limit": 0, "classes": ["valve"]})
        for line in r["bom"]:
            assert line["class"] == "Valves"

    def test_classes_filter_valve_total_components(self, proj):
        """Valve rows in _ROWS_GROUPED: PnPIDs 4,5,6 = 3 components."""
        r = bom(str(proj), {"limit": 0, "classes": ["valve"]})
        assert r["total_components"] == 3

    def test_classes_filter_echoed_in_filters(self, proj):
        r = bom(str(proj), {"limit": 0, "classes": ["valve"]})
        assert r["filters"].get("classes") == ["valve"]

    def test_spec_filter_scopes_bom(self, proj):
        """With spec=SS150, only SS150 components in BOM."""
        r = bom(str(proj), {"limit": 0, "spec": "SS150"})
        for line in r["bom"]:
            # spec column must be SS150 (None rows are excluded by spec filter)
            assert line["spec"] == "SS150"

    def test_spec_filter_echoed(self, proj):
        r = bom(str(proj), {"limit": 0, "spec": "ss150"})
        assert r["filters"].get("spec") == "SS150"

    def test_size_filter_scopes_bom(self, proj):
        """size={value:2.0, unit:'in'} — only 2in components in BOM."""
        r = bom(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        for line in r["bom"]:
            assert line["size"] == '2"'

    def test_size_filter_echoed(self, proj):
        r = bom(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        assert "size" in r["filters"]

    def test_line_and_classes_combined(self, proj):
        """line=L-001 + classes=['pipe'] → only pipes on L-001."""
        r = bom(str(proj), {"limit": 0, "line": "L-001", "classes": ["pipe"]})
        for line in r["bom"]:
            assert line["class"] == "Pipe"
        # L-001 pipes: PnPIDs 1,2 (PnPID 3 is on L-002)
        assert r["total_components"] == 2

    def test_nonexistent_line_returns_empty_bom(self, proj):
        r = bom(str(proj), {"limit": 0, "line": "NONEXISTENT"})
        assert r["bom"] == []
        assert r["total_components"] == 0
        assert r["line_count"] == 0

    def test_no_mutation_of_data_dict(self, proj):
        """bom() must not mutate the caller's data dict."""
        data = {"limit": 5, "classes": ["pipe"]}
        bom(str(proj), data)
        assert data == {"limit": 5, "classes": ["pipe"]}

    def test_none_data_does_not_crash(self, proj):
        r = bom(str(proj), None)
        assert r["ok"] is True

    def test_empty_data_does_not_crash(self, proj):
        r = bom(str(proj), {})
        assert r["ok"] is True


# ===========================================================================
# Part 9 – Notes
# ===========================================================================


class TestNotes:
    def test_net_location_note_not_present(self, result_bom):
        """The 'no se puede localizar... plugin .NET' note must NOT appear in bom."""
        combined = " ".join(result_bom["notes"])
        assert "plugin .NET" not in combined, (
            "bom() must filter out the .NET-location note from list_components"
        )

    def test_grouping_note_present(self, result_bom):
        """The grouping note (mentions 'BOM' or 'agrupa' or 'cantidad') must be present."""
        combined = " ".join(result_bom["notes"]).lower()
        assert (
            "bom" in combined or "agrupa" in combined or "cantidad" in combined
        ), "Expected a grouping note describing BOM aggregation semantics"

    def test_size_note_present_when_size_without_unit(self, proj):
        """When size is given without unit, the ignored-size note must appear."""
        r = bom(str(proj), {"limit": 0, "size": 2.0})
        combined = " ".join(r["notes"]).lower()
        assert (
            "size" in combined or "unidad" in combined or "unit" in combined or
            "ignorado" in combined or "ignored" in combined
        ), "Expected a note about ignored size-without-unit"

    def test_size_note_not_present_when_size_has_unit(self, proj):
        """When size has a valid unit, there must be no size-without-unit note."""
        r = bom(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        combined = " ".join(r["notes"]).lower()
        # The note is specifically about size WITHOUT unit; it should not appear here
        assert "ignorado" not in combined and "ignored" not in combined, (
            "size-without-unit note must not appear when size has a valid unit"
        )

    def test_notes_is_nonempty(self, result_bom):
        assert len(result_bom["notes"]) >= 1


# ===========================================================================
# Part 10 – Server dispatch: operation="bom"
# ===========================================================================


class TestServerDispatch:
    """Smoke test: ensure server.py routes operation='bom' to plant3d_query.bom."""

    def test_bom_operation_reachable(self, proj, monkeypatch):
        """Replace plant3d_query.bom with a spy to verify dispatch."""
        captured = {}

        def _fake_bom(project, data=None):
            captured["project"] = project
            captured["data"] = data
            return {
                "ok": True, "project": "X", "path": "X",
                "limit": 0, "filters": {}, "total_components": 0,
                "line_count": 0, "omitted": 0, "by_class": [], "bom": [], "notes": [],
            }

        import autocad_mcp.plant3d_query as pq
        monkeypatch.setattr(pq, "bom", _fake_bom)

        # Import server and exercise the routing function directly
        import importlib
        import autocad_mcp.server as srv
        importlib.reload(srv)  # ensure fresh import

        import asyncio

        async def _run():
            return await srv.plant3d(
                operation="bom",
                data={"project": str(proj), "limit": 0},
            )

        result_json = asyncio.run(_run())
        # The spy must have been called
        assert "project" in captured, "bom() was never called by server dispatch"

    def test_bom_in_server_module(self):
        """Verify that plant3d_query exports bom as a callable."""
        from autocad_mcp import plant3d_query
        assert callable(getattr(plant3d_query, "bom", None)), (
            "plant3d_query.bom must be a callable function"
        )


# ===========================================================================
# Part 11 – Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, proj):
        db = proj / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        bom(str(proj), {"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, proj):
        r = bom(str(proj / "Piping.dcf"), {"limit": 0})
        assert r["ok"] is True


# ===========================================================================
# Part 12 – Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_empty_project_no_crash(self, tmp_path):
        """An empty Piping.dcf (no rows) must return ok=True with empty BOM."""
        proj = tmp_path / "EMPTY_BOM"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
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

        r = bom(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["bom"] == []
        assert r["total_components"] == 0
        assert r["line_count"] == 0
        assert r["omitted"] == 0

    def test_single_component_one_bom_line(self, tmp_path):
        """A project with a single component must produce exactly one BOM line, qty=1."""
        rows = [(1, "L-001", "TAG-P1", "Pipe", "Tubo unico", "CS150", 2.0, "in")]
        proj = _make_project(tmp_path, "SINGLE_COMP", rows)
        r = bom(str(proj), {"limit": 0})
        assert r["line_count"] == 1
        assert r["total_components"] == 1
        assert r["bom"][0]["quantity"] == 1

    def test_all_components_unique_no_grouping(self, tmp_path):
        """When every component has a unique description, line_count == total_components."""
        rows = [
            (i, "L-001", f"TAG-{i}", "Pipe", f"Tubo {i}", "CS150", 2.0, "in")
            for i in range(1, 6)
        ]
        proj = _make_project(tmp_path, "UNIQUE_COMPS", rows)
        r = bom(str(proj), {"limit": 0})
        assert r["line_count"] == 5
        assert r["total_components"] == 5
        for line in r["bom"]:
            assert line["quantity"] == 1
