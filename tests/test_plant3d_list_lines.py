"""Tests for plant3d_query.list_lines — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) and exercises list_lines and its pure helpers
against them. No real project databases are ever touched.

Key invariants verified:
- service/nominal_spec/nominal_size/insulation come from P3dLineGroup header,
  NOT from PipeRunComponent.Service (which is contaminated by branches).
- Sizes are kept per unit: inches and mm are never merged into a range.
- Schema degradation: missing tables/columns degrade gracefully with notes.
- Read-only: Piping.dcf bytes and mtime are unchanged after any call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from autocad_mcp.plant3d_query import (
    _DEFAULT_IGNORE_SPECS,
    _LINEGROUP_HEADER_COLS,
    _build_line_aggregates,
    _format_sizes,
    _spec_mixed,
    list_lines,
)
from autocad_mcp.plant3d_query import _norm


# ===========================================================================
# Helpers: dict-row adapter for pure helper tests (no SQLite needed)
# ===========================================================================


class _Row(dict):
    """A dict that also supports attribute-style access.

    sqlite3.Row supports both ``r["col"]`` and ``r[index]``; for our pure
    helper tests we only need ``r["col"]``, so a plain dict subclass is
    sufficient — _build_line_aggregates only uses ``r["line"]``, ``r["spec"]``,
    ``r["dia"]`` and ``r["unit"]``.
    """


# ===========================================================================
# Helpers: build minimal SQLite databases
# ===========================================================================


def _make_piping_dcf(path: Path, prc_rows: list[tuple], ei_rows: list[tuple]) -> None:
    """Create a Piping.dcf with PipeRunComponent + EngineeringItems.

    prc_rows: (PnPID, LineNumberTag)
    ei_rows:  (PnPID, Spec, NominalDiameter, NominalUnit)
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE PipeRunComponent (PnPID INTEGER, LineNumberTag TEXT)"
        )
        con.execute(
            "CREATE TABLE EngineeringItems "
            "(PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
        )
        for pnpid, tag in prc_rows:
            con.execute(
                "INSERT INTO PipeRunComponent (PnPID, LineNumberTag) VALUES (?, ?)",
                (pnpid, tag),
            )
        for pnpid, spec, dia, unit in ei_rows:
            con.execute(
                "INSERT INTO EngineeringItems (PnPID, Spec, NominalDiameter, NominalUnit) "
                "VALUES (?, ?, ?, ?)",
                (pnpid, spec, dia, unit),
            )
        con.commit()
    finally:
        con.close()


def _add_linegroup_table(
    path: Path,
    lg_rows: list[tuple],
    columns: tuple[str, ...] = _LINEGROUP_HEADER_COLS,
) -> None:
    """Add P3dLineGroup to an existing Piping.dcf.

    lg_rows: (PnPID, Tag, Service, NominalSpec, NominalSize,
               InsulationType, InsulationThickness)  — one per header entry.
    Only the columns in ``columns`` are created (for degradation testing).
    """
    col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            f"CREATE TABLE P3dLineGroup (PnPID INTEGER, \"Tag\" TEXT, {col_defs})"
        )
        for row in lg_rows:
            pnpid, tag, service, nom_spec, nom_size, ins_type, ins_thick = row
            vals: dict[str, Any] = {
                "PnPID": pnpid,
                "Tag": tag,
                "Service": service,
                "NominalSpec": nom_spec,
                "NominalSize": nom_size,
                "InsulationType": ins_type,
                "InsulationThickness": ins_thick,
            }
            present = {c: vals[c] for c in ("PnPID", "Tag", *columns)}
            placeholders = ", ".join("?" for _ in present)
            cols_str = ", ".join(f'"{k}"' for k in present)
            con.execute(
                f"INSERT INTO P3dLineGroup ({cols_str}) VALUES ({placeholders})",
                list(present.values()),
            )
        con.commit()
    finally:
        con.close()


def _add_drawing_tables(path: Path, dwg_rows: list[tuple]) -> None:
    """Add P3dDrawingLineGroupRelationship + PnPDrawings to an existing Piping.dcf.

    dwg_rows: (rel_PnPID, LineGroup_PnPID, Drawing_PnPID, dwg_name)
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE P3dDrawingLineGroupRelationship "
            "(PnPID INTEGER, LineGroup INTEGER, Drawing INTEGER)"
        )
        con.execute(
            'CREATE TABLE PnPDrawings (PnPID INTEGER, "Dwg Name" TEXT)'
        )
        for rel_id, lg_pnpid, dwg_pnpid, dwg_name in dwg_rows:
            con.execute(
                "INSERT INTO P3dDrawingLineGroupRelationship "
                "(PnPID, LineGroup, Drawing) VALUES (?, ?, ?)",
                (rel_id, lg_pnpid, dwg_pnpid),
            )
            con.execute(
                'INSERT INTO PnPDrawings (PnPID, "Dwg Name") VALUES (?, ?)',
                (dwg_pnpid, dwg_name),
            )
        con.commit()
    finally:
        con.close()


# ===========================================================================
# Part 1 – Pure helpers (no I/O)
# ===========================================================================


class TestFormatSizes:
    """_format_sizes: (dia, unit)->count histogram -> (main_size, sizes)."""

    def test_empty_histogram(self):
        main, sizes = _format_sizes({})
        assert main is None
        assert sizes == []

    def test_single_inch_entry(self):
        main, sizes = _format_sizes({(2.0, "in"): 3})
        assert main == '2"'
        assert sizes == ['2"']

    def test_single_mm_entry(self):
        main, sizes = _format_sizes({(50.0, "mm"): 5})
        assert main == "50 mm"
        assert sizes == ["50 mm"]

    def test_most_frequent_is_main(self):
        hist = {(2.0, "in"): 1, (4.0, "in"): 5}
        main, sizes = _format_sizes(hist)
        assert main == '4"'
        # Most frequent first in sizes list.
        assert sizes[0] == '4"'

    def test_tiebreak_by_diameter(self):
        # Same count: smaller diameter should come first.
        hist = {(4.0, "in"): 3, (2.0, "in"): 3}
        main, sizes = _format_sizes(hist)
        assert main == '2"'

    def test_integer_diameter_drops_decimal(self):
        # 4.0 should render as '4"', not '4.0"'.
        _, sizes = _format_sizes({(4.0, "in"): 1})
        assert '4"' in sizes
        assert '4.0"' not in sizes

    def test_mixed_units_not_merged(self):
        """Inches and mm in the same line must appear as separate entries."""
        hist = {(2.0, "in"): 4, (50.0, "mm"): 3}
        main, sizes = _format_sizes(hist)
        # main is the most frequent (inches here)
        assert main == '2"'
        # Both units present, not merged into a single range string.
        assert '2"' in sizes
        assert "50 mm" in sizes
        assert len(sizes) == 2

    def test_mixed_units_each_size_keeps_its_unit(self):
        """Neither entry absorbs the other's unit."""
        hist = {(2.0, "in"): 2, (80.0, "mm"): 2, (100.0, "mm"): 3}
        main, sizes = _format_sizes(hist)
        # 100 mm is most frequent
        assert main == "100 mm"
        # All three present; no entry has both units.
        assert "100 mm" in sizes
        assert "80 mm" in sizes
        assert '2"' in sizes
        # No size string contains both "mm" and '"'
        for s in sizes:
            assert not ("mm" in s and '"' in s)

    def test_none_unit_renders_without_unit(self):
        """A None unit falls back to bare number."""
        main, sizes = _format_sizes({(6.0, None): 1})
        assert main == "6"
        assert sizes == ["6"]


class TestSpecMixed:
    """_spec_mixed: True iff >1 distinct non-auxiliary spec."""

    def _ignore(self, extras: list[str] | None = None) -> set[str]:
        base = {_norm(s) for s in _DEFAULT_IGNORE_SPECS}
        if extras:
            base |= {_norm(s) for s in extras}
        return base

    def test_single_real_spec_false(self):
        assert _spec_mixed({"CS150"}, self._ignore()) is False

    def test_two_real_specs_true(self):
        assert _spec_mixed({"CS150", "SS150"}, self._ignore()) is True

    def test_empty_set_false(self):
        assert _spec_mixed(set(), self._ignore()) is False

    def test_only_auxiliary_false(self):
        # PlaceHolder Metric is auxiliary; after exclusion, no real spec remains.
        assert _spec_mixed({"PlaceHolder Metric"}, self._ignore()) is False

    def test_real_plus_auxiliary_false(self):
        # CS150 (real) + PlaceHolder Metric (aux) -> only 1 real spec.
        assert _spec_mixed({"CS150", "PlaceHolder Metric"}, self._ignore()) is False

    def test_two_real_plus_auxiliary_true(self):
        # CS150 + SS150 (both real) + PlaceHolder Metric (aux) -> 2 real specs.
        assert _spec_mixed({"CS150", "SS150", "PlaceHolder Metric"}, self._ignore()) is True

    def test_case_variants_collapse_to_one(self):
        # "cs150" and " CS150 " normalize to the same key -> 1 real spec.
        assert _spec_mixed({"cs150", " CS150 "}, self._ignore()) is False

    def test_custom_ignore_specs(self):
        # MY_AUX is not in _DEFAULT_IGNORE_SPECS; passing it explicitly excludes it.
        ignore_with_custom = self._ignore(["MY_AUX"])
        assert _spec_mixed({"CS150", "MY_AUX"}, ignore_with_custom) is False

    def test_custom_ignore_empty_treats_all_as_real(self):
        # Empty ignore set -> PlaceHolder Metric is treated as a real spec.
        assert _spec_mixed({"CS150", "PlaceHolder Metric"}, set()) is True


class TestBuildLineAggregates:
    """_build_line_aggregates: groups rows into per-line aggregates."""

    def _rows(self, data: list[dict]) -> list[_Row]:
        return [_Row(d) for d in data]

    def test_single_line_single_component(self):
        rows = self._rows([{"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"}])
        aggs = _build_line_aggregates(rows)
        assert "L-001" in aggs
        assert aggs["L-001"]["components"] == 1

    def test_multiple_components_same_line(self):
        rows = self._rows([
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "CS150", "dia": 4.0, "unit": "in"},
        ])
        aggs = _build_line_aggregates(rows)
        assert aggs["L-001"]["components"] == 3

    def test_two_lines_are_separate(self):
        rows = self._rows([
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-002", "spec": "SS150", "dia": 4.0, "unit": "in"},
        ])
        aggs = _build_line_aggregates(rows)
        assert len(aggs) == 2
        assert aggs["L-001"]["components"] == 1
        assert aggs["L-002"]["components"] == 1

    def test_spec_set_deduplicates(self):
        rows = self._rows([
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
        ])
        aggs = _build_line_aggregates(rows)
        assert aggs["L-001"]["_specs"] == {"CS150"}

    def test_spec_none_or_empty_not_added(self):
        rows = self._rows([
            {"line": "L-001", "spec": None, "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "  ", "dia": 2.0, "unit": "in"},
        ])
        aggs = _build_line_aggregates(rows)
        assert aggs["L-001"]["_specs"] == set()

    def test_size_histogram_built(self):
        rows = self._rows([
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "CS150", "dia": 4.0, "unit": "in"},
        ])
        aggs = _build_line_aggregates(rows)
        hist = aggs["L-001"]["_sizes"]
        assert hist[(2.0, "in")] == 2
        assert hist[(4.0, "in")] == 1

    def test_dia_none_not_added_to_histogram(self):
        rows = self._rows([
            {"line": "L-001", "spec": "CS150", "dia": None, "unit": "in"},
        ])
        aggs = _build_line_aggregates(rows)
        assert aggs["L-001"]["_sizes"] == {}

    def test_empty_rows(self):
        aggs = _build_line_aggregates([])
        assert aggs == {}

    def test_mixed_units_separate_histogram_keys(self):
        rows = self._rows([
            {"line": "L-001", "spec": "CS150", "dia": 2.0, "unit": "in"},
            {"line": "L-001", "spec": "CS150", "dia": 50.0, "unit": "mm"},
        ])
        aggs = _build_line_aggregates(rows)
        hist = aggs["L-001"]["_sizes"]
        assert (2.0, "in") in hist
        assert (50.0, "mm") in hist
        assert (2.0, "mm") not in hist


# ===========================================================================
# Part 2 – list_lines with real SQLite fixtures
# ===========================================================================


@pytest.fixture
def full_project(tmp_path: Path) -> Path:
    """
    Complete synthetic project with all four tables.

    Lines:
      L-001: 3 components, CS150, 2" (x2) + 4" (x1), Service=WATER,
             NominalSpec=CS150-H, NominalSize=2", ins=HOT/25mm,
             model_dwg=AREA1.dwg
      L-002: 2 components, CS150 + SS150 (mixed specs), 4" (x2), Service=STEAM,
             NominalSpec=CS150-H, NominalSize=4", ins=None,
             model_dwg=AREA2.dwg
      L-003: 1 component, PlaceHolder Metric only (auxiliary), 6" (x1),
             Service=AIR, no model_dwg

    PRC row with Service=WRONG_SERVICE to verify header wins over PRC.Service.
    """
    proj = tmp_path / "FULL_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")

    prc_rows = [
        # L-001
        (1, "L-001"),
        (2, "L-001"),
        (3, "L-001"),
        # L-002
        (4, "L-002"),
        (5, "L-002"),
        # L-003
        (6, "L-003"),
    ]
    ei_rows = [
        # L-001: 2x 2", 1x 4", all CS150
        (1, "CS150", 2.0, "in"),
        (2, "CS150", 2.0, "in"),
        (3, "CS150", 4.0, "in"),
        # L-002: 2x 4", mixed CS150+SS150
        (4, "CS150", 4.0, "in"),
        (5, "SS150", 4.0, "in"),
        # L-003: 1x 6", PlaceHolder Metric only
        (6, "PlaceHolder Metric", 6.0, "in"),
    ]
    db = proj / "Piping.dcf"
    _make_piping_dcf(db, prc_rows, ei_rows)

    # P3dLineGroup: PnPID, Tag, Service, NominalSpec, NominalSize, InsType, InsThick
    lg_rows = [
        (100, "L-001", "WATER",  "CS150-H", '2"',  "HOT", "25mm"),
        (200, "L-002", "STEAM",  "CS150-H", '4"',  None,  None),
        (300, "L-003", "AIR",    "PH-SPEC", '6"',  None,  None),
    ]
    _add_linegroup_table(db, lg_rows)

    # Drawings: rel_id, lg_PnPID, dwg_PnPID, dwg_name
    dwg_rows = [
        (1, 100, 10, "AREA1.dwg"),
        (2, 200, 20, "AREA2.dwg"),
        # L-003 has no drawing
    ]
    _add_drawing_tables(db, dwg_rows)
    return proj


class TestListLinesBasicShape:
    def test_ok_flag(self, full_project):
        r = list_lines(str(full_project))
        assert r["ok"] is True

    def test_project_name(self, full_project):
        r = list_lines(str(full_project))
        assert r["project"] == "FULL_PROJ"

    def test_count_equals_three_lines(self, full_project):
        # Three distinct valid tags: L-001, L-002, L-003.
        r = list_lines(str(full_project), data={"limit": 0})
        assert r["count"] == 3

    def test_lines_sorted_alphabetically(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        tags = [l["line"] for l in r["lines"]]
        assert tags == sorted(tags)

    def test_notes_field_is_list(self, full_project):
        r = list_lines(str(full_project))
        assert isinstance(r["notes"], list)

    def test_required_top_level_keys_present(self, full_project):
        r = list_lines(str(full_project))
        for key in ("ok", "project", "path", "limit", "ignore_specs",
                    "count", "omitted", "lines", "notes"):
            assert key in r, f"Missing key: {key}"

    def test_each_line_has_required_keys(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        required = ("line", "components", "service", "nominal_spec", "nominal_size",
                    "specs", "spec_mixed", "main_size", "sizes",
                    "insulation_type", "insulation_thickness", "model_dwgs")
        for entry in r["lines"]:
            for key in required:
                assert key in entry, f"Line {entry['line']} missing key: {key}"


class TestListLinesComponents:
    def test_l001_three_components(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["components"] == 3

    def test_l002_two_components(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l002 = next(l for l in r["lines"] if l["line"] == "L-002")
        assert l002["components"] == 2

    def test_l003_one_component(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l003 = next(l for l in r["lines"] if l["line"] == "L-003")
        assert l003["components"] == 1


class TestListLinesHeaderOverPRC:
    """service/nominal_spec/nominal_size come from P3dLineGroup header, not PRC."""

    def test_service_from_header(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["service"] == "WATER"

    def test_nominal_spec_from_header(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["nominal_spec"] == "CS150-H"

    def test_nominal_size_from_header(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["nominal_size"] == '2"'

    def test_service_header_wins_over_prc(self, tmp_path):
        """Explicitly verify header.Service beats PRC.Service when they differ."""
        proj = tmp_path / "HEADER_PRIORITY"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        # Add Service column to PRC with a different (contaminated) value.
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE PipeRunComponent "
                "(PnPID INTEGER, LineNumberTag TEXT, Service TEXT)"
            )
            con.execute(
                "CREATE TABLE EngineeringItems "
                "(PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
            )
            con.execute("INSERT INTO PipeRunComponent VALUES (1, 'L-001', 'WRONG_SERVICE')")
            con.execute("INSERT INTO EngineeringItems VALUES (1, 'CS150', 2.0, 'in')")
            con.commit()
        finally:
            con.close()

        # Header has the correct service.
        lg_rows = [(100, "L-001", "CORRECT_SERVICE", "CS150", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)
        _add_drawing_tables(db, [])

        r = list_lines(str(proj), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        # Must use header value, not PRC.Service.
        assert l001["service"] == "CORRECT_SERVICE"


class TestListLinesInsulation:
    def test_insulation_type_single_value(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["insulation_type"] == "HOT"
        assert l001["insulation_thickness"] == "25mm"

    def test_insulation_none_when_absent(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l002 = next(l for l in r["lines"] if l["line"] == "L-002")
        assert l002["insulation_type"] is None
        assert l002["insulation_thickness"] is None

    def test_insulation_multiple_values_returns_list(self, tmp_path):
        """Tag maps to two header groups with different InsulationType -> list."""
        proj = tmp_path / "MULTI_INS"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])

        # Two P3dLineGroup rows for the same Tag with different InsulationType.
        lg_rows = [
            (100, "L-001", "WATER", "CS150", '2"', "HOT",  "25mm"),
            (101, "L-001", "WATER", "CS150", '2"', "COLD", "50mm"),
        ]
        _add_linegroup_table(db, lg_rows)
        _add_drawing_tables(db, [])

        r = list_lines(str(proj), data={"limit": 0})
        l001 = r["lines"][0]
        # Both InsulationType values are distinct: result is a list.
        assert isinstance(l001["insulation_type"], list)
        assert sorted(l001["insulation_type"]) == ["COLD", "HOT"]
        assert isinstance(l001["insulation_thickness"], list)
        assert sorted(l001["insulation_thickness"]) == ["25mm", "50mm"]

    def test_insulation_same_value_two_rows_not_list(self, tmp_path):
        """Two header rows agree on InsulationType -> single string, not list."""
        proj = tmp_path / "SAME_INS"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])
        lg_rows = [
            (100, "L-001", "WATER", "CS150", '2"', "HOT", "25mm"),
            (101, "L-001", "WATER", "CS150", '2"', "HOT", "25mm"),
        ]
        _add_linegroup_table(db, lg_rows)
        _add_drawing_tables(db, [])

        r = list_lines(str(proj), data={"limit": 0})
        l001 = r["lines"][0]
        assert l001["insulation_type"] == "HOT"
        assert l001["insulation_thickness"] == "25mm"


class TestListLinesSizes:
    def test_l001_main_size_is_most_frequent(self, full_project):
        # L-001 has 2x 2" and 1x 4" -> main_size = '2"'
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["main_size"] == '2"'

    def test_l001_sizes_list(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert '2"' in l001["sizes"]
        assert '4"' in l001["sizes"]
        # Most frequent is first.
        assert l001["sizes"][0] == '2"'

    def test_mixed_units_separate_sizes(self, tmp_path):
        """Line with both in and mm components keeps units separate."""
        proj = tmp_path / "MIXED_UNITS"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        prc_rows = [(1, "L-001"), (2, "L-001"), (3, "L-001")]
        ei_rows = [
            (1, "CS150", 2.0, "in"),
            (2, "CS150", 2.0, "in"),
            (3, "CS150", 50.0, "mm"),
        ]
        _make_piping_dcf(db, prc_rows, ei_rows)
        lg_rows = [(100, "L-001", "WATER", "CS150", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)
        _add_drawing_tables(db, [])

        r = list_lines(str(proj), data={"limit": 0})
        l001 = r["lines"][0]

        # Both units present as separate entries.
        assert '2"' in l001["sizes"]
        assert "50 mm" in l001["sizes"]
        # No entry should have both units.
        for s in l001["sizes"]:
            assert not ("mm" in s and '"' in s)
        # main_size is the most frequent (2" x2 > 50mm x1).
        assert l001["main_size"] == '2"'


class TestListLinesSpecs:
    def test_l001_not_spec_mixed(self, full_project):
        # L-001 has only CS150 -> not mixed.
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["spec_mixed"] is False

    def test_l002_spec_mixed(self, full_project):
        # L-002 has CS150 + SS150 -> mixed.
        r = list_lines(str(full_project), data={"limit": 0})
        l002 = next(l for l in r["lines"] if l["line"] == "L-002")
        assert l002["spec_mixed"] is True

    def test_l003_auxiliary_only_not_mixed(self, full_project):
        # L-003 has only PlaceHolder Metric (auxiliary) -> not mixed.
        r = list_lines(str(full_project), data={"limit": 0})
        l003 = next(l for l in r["lines"] if l["line"] == "L-003")
        assert l003["spec_mixed"] is False

    def test_l001_specs_list(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert "CS150" in l001["specs"]

    def test_specs_sorted(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l002 = next(l for l in r["lines"] if l["line"] == "L-002")
        assert l002["specs"] == sorted(l002["specs"])


class TestListLinesModelDwgs:
    def test_l001_has_model_dwg(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert "AREA1.dwg" in l001["model_dwgs"]

    def test_l002_has_model_dwg(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l002 = next(l for l in r["lines"] if l["line"] == "L-002")
        assert "AREA2.dwg" in l002["model_dwgs"]

    def test_l003_no_model_dwg(self, full_project):
        r = list_lines(str(full_project), data={"limit": 0})
        l003 = next(l for l in r["lines"] if l["line"] == "L-003")
        assert l003["model_dwgs"] == []

    def test_model_dwgs_sorted(self, tmp_path):
        """Multiple DWGs for one tag -> alphabetically sorted list."""
        proj = tmp_path / "MULTI_DWG"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])
        lg_rows = [(100, "L-001", "WATER", "CS150", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)
        # Two drawings for the same line group.
        dwg_rows = [
            (1, 100, 10, "ZONE_B.dwg"),
            (2, 100, 11, "ZONE_A.dwg"),
        ]
        _add_drawing_tables(db, dwg_rows)

        r = list_lines(str(proj), data={"limit": 0})
        l001 = r["lines"][0]
        assert l001["model_dwgs"] == sorted(l001["model_dwgs"])
        assert "ZONE_A.dwg" in l001["model_dwgs"]
        assert "ZONE_B.dwg" in l001["model_dwgs"]


# ===========================================================================
# Part 3 – Schema degradation (CRITICAL)
# ===========================================================================


class TestDegradationNoP3dLineGroup:
    """DB without P3dLineGroup: lines still returned from PRC+EI, headers null."""

    @pytest.fixture
    def no_header_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "NO_HEADER"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        _make_piping_dcf(
            db,
            [(1, "L-001"), (2, "L-001"), (3, "L-002")],
            [(1, "CS150", 2.0, "in"), (2, "CS150", 4.0, "in"), (3, "SS150", 4.0, "in")],
        )
        # No P3dLineGroup, no drawing tables.
        return proj

    def test_does_not_raise(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        assert r["ok"] is True

    def test_correct_line_count(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        assert r["count"] == 2

    def test_components_correct(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        assert l001["components"] == 2

    def test_header_fields_are_null(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        for field in ("service", "nominal_spec", "nominal_size",
                      "insulation_type", "insulation_thickness"):
            assert l001[field] is None, f"Expected None for {field}"

    def test_model_dwgs_empty(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        for line in r["lines"]:
            assert line["model_dwgs"] == []

    def test_note_mentions_missing_header(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        combined = " ".join(r["notes"]).lower()
        # Should mention that P3dLineGroup was not found.
        assert "p3dlinegroup" in combined or "cabecera" in combined

    def test_sizes_still_aggregated(self, no_header_project):
        r = list_lines(str(no_header_project), data={"limit": 0})
        l001 = next(l for l in r["lines"] if l["line"] == "L-001")
        # Even without header, sizes come from EngineeringItems.
        assert l001["main_size"] is not None
        assert len(l001["sizes"]) >= 1


class TestDegradationMissingOptionalColumn:
    """P3dLineGroup exists but InsulationThickness column is absent."""

    @pytest.fixture
    def no_thick_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "NO_THICK"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])

        # Create P3dLineGroup WITHOUT InsulationThickness column.
        cols_without_thick = tuple(
            c for c in _LINEGROUP_HEADER_COLS if c != "InsulationThickness"
        )
        lg_rows = [(100, "L-001", "WATER", "CS150", '2"', "HOT", None)]
        _add_linegroup_table(db, lg_rows, columns=cols_without_thick)
        _add_drawing_tables(db, [])
        return proj

    def test_does_not_raise(self, no_thick_project):
        r = list_lines(str(no_thick_project), data={"limit": 0})
        assert r["ok"] is True

    def test_insulation_thickness_null(self, no_thick_project):
        r = list_lines(str(no_thick_project), data={"limit": 0})
        l001 = r["lines"][0]
        assert l001["insulation_thickness"] is None

    def test_insulation_type_still_present(self, no_thick_project):
        r = list_lines(str(no_thick_project), data={"limit": 0})
        l001 = r["lines"][0]
        assert l001["insulation_type"] == "HOT"

    def test_note_mentions_missing_column(self, no_thick_project):
        r = list_lines(str(no_thick_project), data={"limit": 0})
        combined = " ".join(r["notes"])
        assert "InsulationThickness" in combined


class TestDegradationNoDrawingRelation:
    """P3dLineGroup present but drawing tables absent -> model_dwgs=[]."""

    @pytest.fixture
    def no_dwg_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "NO_DWG"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])
        lg_rows = [(100, "L-001", "WATER", "CS150", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)
        # Drawing tables NOT added.
        return proj

    def test_model_dwgs_empty(self, no_dwg_project):
        r = list_lines(str(no_dwg_project), data={"limit": 0})
        assert r["lines"][0]["model_dwgs"] == []

    def test_does_not_raise(self, no_dwg_project):
        r = list_lines(str(no_dwg_project), data={"limit": 0})
        assert r["ok"] is True

    def test_note_mentions_missing_relation(self, no_dwg_project):
        r = list_lines(str(no_dwg_project), data={"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert "model_dwgs" in combined or "dibujo" in combined or "relaci" in combined

    def test_header_fields_still_returned(self, no_dwg_project):
        r = list_lines(str(no_dwg_project), data={"limit": 0})
        l001 = r["lines"][0]
        assert l001["service"] == "WATER"
        assert l001["nominal_spec"] == "CS150"


class TestDegradationPnPDrawingsNoDwgNameCol:
    """PnPDrawings exists but lacks 'Dwg Name' column -> model_dwgs=[] + note."""

    @pytest.fixture
    def bad_dwgs_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "BAD_DWG_COL"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])
        lg_rows = [(100, "L-001", "WATER", "CS150", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)

        # Add drawing tables but WITHOUT 'Dwg Name' column.
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE P3dDrawingLineGroupRelationship "
                "(PnPID INTEGER, LineGroup INTEGER, Drawing INTEGER)"
            )
            con.execute(
                "CREATE TABLE PnPDrawings (PnPID INTEGER, OtherCol TEXT)"
            )
            con.commit()
        finally:
            con.close()
        return proj

    def test_model_dwgs_empty(self, bad_dwgs_project):
        r = list_lines(str(bad_dwgs_project), data={"limit": 0})
        assert r["lines"][0]["model_dwgs"] == []

    def test_note_mentions_dwg_name(self, bad_dwgs_project):
        r = list_lines(str(bad_dwgs_project), data={"limit": 0})
        combined = " ".join(r["notes"])
        assert "Dwg Name" in combined


# ===========================================================================
# Part 4 – Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, full_project):
        db = full_project / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        list_lines(str(full_project), data={"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, full_project):
        """resolve_project_dir accepts a .dcf path and uses its parent."""
        r = list_lines(str(full_project / "Piping.dcf"), data={"limit": 0})
        assert r["ok"] is True
        assert r["count"] == 3

    def test_db_bytes_unchanged_after_degraded_call(self, tmp_path):
        """Read-only holds even when P3dLineGroup is absent."""
        proj = tmp_path / "RO_DEGRADE"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        _make_piping_dcf(db, [(1, "L-001")], [(1, "CS150", 2.0, "in")])
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        list_lines(str(proj), data={"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before


# ===========================================================================
# Part 5 – Parametrisation: limit, ignore_specs, omitted
# ===========================================================================


@pytest.fixture
def many_lines_project(tmp_path: Path) -> Path:
    """Project with 10 valid lines for limit/omitted tests."""
    proj = tmp_path / "MANY_LINES"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    db = proj / "Piping.dcf"

    prc_rows = [(i, f"L-{i:03}") for i in range(1, 11)]
    ei_rows = [(i, "CS150", 2.0, "in") for i in range(1, 11)]
    _make_piping_dcf(db, prc_rows, ei_rows)
    return proj


class TestParametrisation:
    def test_default_limit_50_no_omission_when_few_lines(self, full_project):
        # full_project has 3 lines, default limit 50 -> no omission.
        r = list_lines(str(full_project))
        assert r["omitted"] == 0
        assert len(r["lines"]) == r["count"]

    def test_limit_caps_lines(self, many_lines_project):
        r = list_lines(str(many_lines_project), data={"limit": 3})
        assert len(r["lines"]) == 3
        assert r["count"] == 10
        assert r["omitted"] == 7

    def test_limit_zero_no_cap(self, many_lines_project):
        r = list_lines(str(many_lines_project), data={"limit": 0})
        assert len(r["lines"]) == 10
        assert r["count"] == 10
        assert r["omitted"] == 0

    def test_limit_reflected_in_output(self, many_lines_project):
        r = list_lines(str(many_lines_project), data={"limit": 5})
        assert r["limit"] == 5

    def test_omitted_never_silent(self, many_lines_project):
        """count always reflects total; omitted = count - len(lines)."""
        r = list_lines(str(many_lines_project), data={"limit": 4})
        assert r["omitted"] == r["count"] - len(r["lines"])

    def test_capped_lines_are_alphabetically_first(self, many_lines_project):
        """With limit=3 and alphabetical ordering, L-001, L-002, L-003 come first."""
        r = list_lines(str(many_lines_project), data={"limit": 3})
        tags = [l["line"] for l in r["lines"]]
        assert tags == ["L-001", "L-002", "L-003"]

    def test_custom_ignore_specs_affects_spec_mixed(self, tmp_path):
        """Custom ignore_specs excludes MY_AUX: CS150+MY_AUX not flagged as mixed."""
        proj = tmp_path / "CUSTOM_IGNORE"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        prc_rows = [(1, "L-001"), (2, "L-001")]
        ei_rows = [
            (1, "CS150",  2.0, "in"),
            (2, "MY_AUX", 2.0, "in"),
        ]
        _make_piping_dcf(db, prc_rows, ei_rows)
        # No header tables needed for this test.

        # Without ignore: CS150+MY_AUX -> spec_mixed=True
        r_default = list_lines(str(proj), data={"limit": 0})
        l001_default = r_default["lines"][0]
        assert l001_default["spec_mixed"] is True

        # With MY_AUX ignored -> only CS150 remains -> spec_mixed=False
        r_custom = list_lines(str(proj), data={"limit": 0, "ignore_specs": ["MY_AUX"]})
        l001_custom = r_custom["lines"][0]
        assert l001_custom["spec_mixed"] is False

    def test_ignore_specs_reflected_in_output(self, full_project):
        custom = ["MY_AUX", "ANOTHER"]
        r = list_lines(str(full_project), data={"ignore_specs": custom})
        assert r["ignore_specs"] == sorted(custom)

    def test_untagged_excluded(self, tmp_path):
        """NULL / '' / '?' tags must not appear in lines."""
        proj = tmp_path / "UNTAGGED_EXCL"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        prc_rows = [
            (1, None),       # NULL
            (2, ""),         # empty
            (3, "?"),        # literal ?
            (4, "L-001"),    # valid
        ]
        ei_rows = [(i, "CS150", 2.0, "in") for i in range(1, 5)]
        _make_piping_dcf(db, prc_rows, ei_rows)

        r = list_lines(str(proj), data={"limit": 0})
        tags = [l["line"] for l in r["lines"]]
        assert tags == ["L-001"]
        assert r["count"] == 1


# ===========================================================================
# Part 6 – Corrección 1: match de Tag normalizado (TRIM + UPPER)
# ===========================================================================
#
# P3dLineGroup.Tag puede diferir del PipeRunComponent.LineNumberTag en
# espacios y/o mayúsculas. El cruce se hace con _norm() en ambos lados;
# el valor ``line`` de salida es el LineNumberTag crudo del componente.


class TestNormalizedTagMatch:
    """Header + DWGs se cruzan aunque Tag difiera en caja o espacios."""

    @pytest.fixture
    def case_mismatch_project(self, tmp_path: Path) -> Path:
        """PRC.LineNumberTag = "pipe-001"  /  P3dLineGroup.Tag = " PIPE-001 "."""
        proj = tmp_path / "CASE_MISMATCH"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        # Dos componentes cuyo tag está en minúsculas
        _make_piping_dcf(
            db,
            [(1, "pipe-001"), (2, "pipe-001")],
            [(1, "CS150", 2.0, "in"), (2, "CS150", 4.0, "in")],
        )

        # Header con Tag en mayúsculas y con espacios al principio/final
        lg_rows = [
            (100, " PIPE-001 ", "WATER", "CS150-H", '2"', "HOT", "25mm"),
        ]
        _add_linegroup_table(db, lg_rows)

        # DWG relationship también con Tag en otra caja
        #   lg PnPID=100 -> dwg PnPID=10 -> "MODEL.dwg"
        # Creamos las tablas manualmente para controlar el Tag de P3dLineGroup
        # (ya insertado arriba con PnPID=100).
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE P3dDrawingLineGroupRelationship "
                "(PnPID INTEGER, LineGroup INTEGER, Drawing INTEGER)"
            )
            con.execute(
                'CREATE TABLE PnPDrawings (PnPID INTEGER, "Dwg Name" TEXT)'
            )
            con.execute(
                "INSERT INTO P3dDrawingLineGroupRelationship VALUES (1, 100, 10)"
            )
            con.execute("INSERT INTO PnPDrawings VALUES (10, 'MODEL.dwg')")
            con.commit()
        finally:
            con.close()

        return proj

    def test_header_fields_populated_despite_case_mismatch(self, case_mismatch_project):
        """service/nominal_spec/nominal_size/insulation se rellenan desde el header."""
        r = list_lines(str(case_mismatch_project), data={"limit": 0})
        assert r["count"] == 1
        line = r["lines"][0]

        assert line["service"] == "WATER", (
            "service debe rellenarse desde P3dLineGroup aunque los Tags difieran en caja"
        )
        assert line["nominal_spec"] == "CS150-H"
        assert line["nominal_size"] == '2"'
        assert line["insulation_type"] == "HOT"
        assert line["insulation_thickness"] == "25mm"

    def test_model_dwgs_populated_despite_case_mismatch(self, case_mismatch_project):
        """model_dwgs se resuelve correctamente con Tag normalizado."""
        r = list_lines(str(case_mismatch_project), data={"limit": 0})
        line = r["lines"][0]
        assert "MODEL.dwg" in line["model_dwgs"], (
            "model_dwgs debe resolverse aunque el Tag en P3dLineGroup tenga caja distinta"
        )

    def test_output_line_preserves_raw_tag(self, case_mismatch_project):
        """El campo ``line`` de salida conserva el LineNumberTag crudo del componente."""
        r = list_lines(str(case_mismatch_project), data={"limit": 0})
        line = r["lines"][0]
        assert line["line"] == "pipe-001", (
            "El valor 'line' de salida debe ser el LineNumberTag crudo, no el normalizado"
        )

    def test_components_counted_correctly(self, case_mismatch_project):
        """Ambos componentes se cuentan bajo la misma línea."""
        r = list_lines(str(case_mismatch_project), data={"limit": 0})
        line = r["lines"][0]
        assert line["components"] == 2

    def test_space_only_in_header_tag(self, tmp_path):
        """Tag = '  pipe-002  ' (dobles espacios) cruza con LineNumberTag = 'PIPE-002'."""
        proj = tmp_path / "SPACE_TAG"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        _make_piping_dcf(
            db,
            [(1, "PIPE-002")],
            [(1, "SS150", 3.0, "in")],
        )
        # Header con espacios dobles al principio y al final
        lg_rows = [
            (200, "  pipe-002  ", "STEAM", "SS150-C", '3"', None, None),
        ]
        _add_linegroup_table(db, lg_rows)
        _add_drawing_tables(db, [])

        r = list_lines(str(proj), data={"limit": 0})
        assert r["count"] == 1
        line = r["lines"][0]
        assert line["line"] == "PIPE-002"       # valor crudo del componente
        assert line["service"] == "STEAM"        # cruzado desde header
        assert line["nominal_spec"] == "SS150-C"


# ===========================================================================
# Part 7 – Corrección 2: JOIN de DWG protegido (columnas ausentes)
# ===========================================================================
#
# Si P3dDrawingLineGroupRelationship carece de LineGroup/Drawing, o si
# PnPDrawings carece de PnPID/"Dwg Name", list_lines NO debe lanzar excepción.
# En cambio: model_dwgs=[] en todas las líneas + nota en ``notes``.
# La cabecera (service, etc.) sí debe rellenarse si P3dLineGroup es válida.


class TestDegradationDwgMissingColumns:
    """JOIN de DWG protegido: columnas faltantes -> model_dwgs=[] + nota."""

    @pytest.fixture
    def rel_missing_drawing_col(self, tmp_path: Path) -> Path:
        """P3dDrawingLineGroupRelationship sin la columna 'Drawing'."""
        proj = tmp_path / "REL_NO_DRAWING"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        _make_piping_dcf(
            db,
            [(1, "L-001"), (2, "L-001")],
            [(1, "CS150", 2.0, "in"), (2, "CS150", 2.0, "in")],
        )
        lg_rows = [(100, "L-001", "WATER", "CS150-H", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)

        # Tabla de relación SIN columna 'Drawing'
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE P3dDrawingLineGroupRelationship "
                "(PnPID INTEGER, LineGroup INTEGER)"   # falta 'Drawing'
            )
            con.execute(
                'CREATE TABLE PnPDrawings (PnPID INTEGER, "Dwg Name" TEXT)'
            )
            con.execute(
                "INSERT INTO P3dDrawingLineGroupRelationship VALUES (1, 100)"
            )
            con.execute("INSERT INTO PnPDrawings VALUES (10, 'MODEL.dwg')")
            con.commit()
        finally:
            con.close()

        return proj

    def test_does_not_raise(self, rel_missing_drawing_col):
        """list_lines no lanza excepción cuando falta la columna 'Drawing'."""
        r = list_lines(str(rel_missing_drawing_col), data={"limit": 0})
        assert r["ok"] is True

    def test_model_dwgs_empty_all_lines(self, rel_missing_drawing_col):
        """model_dwgs es [] en todas las líneas cuando falta la columna 'Drawing'."""
        r = list_lines(str(rel_missing_drawing_col), data={"limit": 0})
        for line in r["lines"]:
            assert line["model_dwgs"] == [], (
                f"Esperaba model_dwgs=[] para la línea '{line['line']}'"
            )

    def test_note_mentions_problem(self, rel_missing_drawing_col):
        """Debe haber una nota que mencione el problema del JOIN de dibujos."""
        r = list_lines(str(rel_missing_drawing_col), data={"limit": 0})
        combined = " ".join(r["notes"]).lower()
        # La nota debe mencionar algo relacionado con columns/dibujos/relación
        assert any(
            keyword in combined
            for keyword in ("drawing", "dibujo", "columna", "model_dwgs", "relaci")
        ), f"Ninguna nota menciona el problema del JOIN; notes={r['notes']}"

    def test_header_still_populated(self, rel_missing_drawing_col):
        """El header (service, nominal_spec, etc.) sí se rellena a pesar del fallo de DWG."""
        r = list_lines(str(rel_missing_drawing_col), data={"limit": 0})
        line = r["lines"][0]
        assert line["service"] == "WATER"
        assert line["nominal_spec"] == "CS150-H"

    @pytest.fixture
    def pnpdrawings_missing_pnpid_col(self, tmp_path: Path) -> Path:
        """PnPDrawings sin la columna 'PnPID'."""
        proj = tmp_path / "DWG_NO_PNPID"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"

        _make_piping_dcf(
            db,
            [(1, "L-001")],
            [(1, "CS150", 2.0, "in")],
        )
        lg_rows = [(100, "L-001", "WATER", "CS150-H", '2"', None, None)]
        _add_linegroup_table(db, lg_rows)

        # PnPDrawings SIN columna PnPID
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE P3dDrawingLineGroupRelationship "
                "(PnPID INTEGER, LineGroup INTEGER, Drawing INTEGER)"
            )
            con.execute(
                'CREATE TABLE PnPDrawings ("Dwg Name" TEXT)'   # falta PnPID
            )
            con.execute(
                "INSERT INTO P3dDrawingLineGroupRelationship VALUES (1, 100, 10)"
            )
            con.execute("INSERT INTO PnPDrawings VALUES ('MODEL.dwg')")
            con.commit()
        finally:
            con.close()

        return proj

    def test_pnpdrawings_missing_pnpid_does_not_raise(self, pnpdrawings_missing_pnpid_col):
        """No lanza excepción cuando PnPDrawings carece de PnPID."""
        r = list_lines(str(pnpdrawings_missing_pnpid_col), data={"limit": 0})
        assert r["ok"] is True

    def test_pnpdrawings_missing_pnpid_model_dwgs_empty(self, pnpdrawings_missing_pnpid_col):
        """model_dwgs=[] cuando PnPDrawings carece de PnPID."""
        r = list_lines(str(pnpdrawings_missing_pnpid_col), data={"limit": 0})
        for line in r["lines"]:
            assert line["model_dwgs"] == []

    def test_pnpdrawings_missing_pnpid_note_present(self, pnpdrawings_missing_pnpid_col):
        """Nota que menciona el problema cuando PnPDrawings carece de PnPID."""
        r = list_lines(str(pnpdrawings_missing_pnpid_col), data={"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert any(
            keyword in combined
            for keyword in ("pnpid", "drawing", "dibujo", "columna", "model_dwgs", "relaci")
        ), f"Ninguna nota menciona el problema; notes={r['notes']}"
