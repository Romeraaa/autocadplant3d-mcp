"""Tests for plant3d_query.pipe_length — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) that include the ``Pipe`` table required by
pipe_length.  No real project databases are ever touched.

Key invariants verified:
1.  Aggregation pura _build_pipe_length_aggregates (line/spec/size group_by).
2.  Separación por unidad de longitud: 'mm' e 'in' nunca se suman.
3.  Untagged: tramos sin LineNumberTag válido reportados siempre en 'untagged'
    y como grupo "(SIN LÍNEA)" cuando group_by="line".
4.  Redondeo: _round_lengths — escalar vs dict, 2 decimales.
5.  Filtros: line, spec, size con unidad (filtra) y sin unidad (ignora + nota).
6.  limit/omitted: acota grupos, no silencioso; 0 = sin tope.
7.  group_by inválido cae a "line" con nota.
8.  Degradación de esquema: sin tabla Pipe, sin columna Length, sin LengthUnit.
9.  data no se muta.
10. Solo PartCategory='Pipe' (la tabla Pipe solo tiene tubería por diseño).
11. Estructura de salida: claves requeridas, tipos correctos.
12. Garantía de solo lectura.
13. Test de integración (skipped si la ruta no es accesible).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import (
    _NO_LINE_LABEL,
    _build_pipe_length_aggregates,
    _round_lengths,
    pipe_length,
)


# ===========================================================================
# Helpers: adaptador de fila-dict para los tests puros (sin SQLite)
# ===========================================================================


class _Row(dict):
    """Dict cuyas claves también son accesibles como r["col"]."""


def _row(**kw) -> _Row:
    return _Row(**kw)


# ===========================================================================
# Helpers: construcción de proyectos SQLite mínimos en tmp_path
# ===========================================================================


def _make_piping_dcf(
    path: Path,
    prc_rows: list[tuple],
    ei_rows: list[tuple],
    pipe_rows: list[tuple],
    *,
    include_length_col: bool = True,
    include_length_unit_col: bool = True,
    create_pipe_table: bool = True,
) -> None:
    """Crea un Piping.dcf mínimo con PipeRunComponent, EngineeringItems y Pipe.

    prc_rows: (PnPID, LineNumberTag)
    ei_rows:  (PnPID, Spec, NominalDiameter, NominalUnit, LengthUnit?)
    pipe_rows: (PnPID, Length?)
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE PipeRunComponent (PnPID INTEGER, LineNumberTag TEXT)"
        )
        ei_cols = "PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT"
        if include_length_unit_col:
            ei_cols += ", LengthUnit TEXT"
        con.execute(f"CREATE TABLE EngineeringItems ({ei_cols})")

        if create_pipe_table:
            pipe_cols = "PnPID INTEGER"
            if include_length_col:
                pipe_cols += ", Length REAL"
            con.execute(f"CREATE TABLE Pipe ({pipe_cols})")

        for pnpid, tag in prc_rows:
            con.execute(
                "INSERT INTO PipeRunComponent (PnPID, LineNumberTag) VALUES (?, ?)",
                (pnpid, tag),
            )

        for ei_row in ei_rows:
            if include_length_unit_col:
                pnpid, spec, dia, dia_unit, lunit = ei_row
                con.execute(
                    "INSERT INTO EngineeringItems "
                    "(PnPID, Spec, NominalDiameter, NominalUnit, LengthUnit) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pnpid, spec, dia, dia_unit, lunit),
                )
            else:
                pnpid, spec, dia, dia_unit = ei_row
                con.execute(
                    "INSERT INTO EngineeringItems "
                    "(PnPID, Spec, NominalDiameter, NominalUnit) "
                    "VALUES (?, ?, ?, ?)",
                    (pnpid, spec, dia, dia_unit),
                )

        if create_pipe_table:
            for pipe_row in pipe_rows:
                if include_length_col:
                    pnpid, length = pipe_row
                    con.execute(
                        "INSERT INTO Pipe (PnPID, Length) VALUES (?, ?)",
                        (pnpid, length),
                    )
                else:
                    (pnpid,) = pipe_row
                    con.execute(
                        "INSERT INTO Pipe (PnPID) VALUES (?)",
                        (pnpid,),
                    )

        con.commit()
    finally:
        con.close()


def _make_project(
    base: Path,
    name: str,
    prc_rows: list[tuple],
    ei_rows: list[tuple],
    pipe_rows: list[tuple],
    **kw,
) -> Path:
    """Crea una carpeta de proyecto mínima con Project.xml + Piping.dcf."""
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", prc_rows, ei_rows, pipe_rows, **kw)
    return proj


# ---------------------------------------------------------------------------
# Dataset canónico de prueba
#
# PnPIDs en PipeRunComponent + EngineeringItems + Pipe:
#   1  L-001  CS150  2"  mm  1000.0  (tagged, spec CS150, 2 in)
#   2  L-001  CS150  2"  mm  1500.0  (tagged, spec CS150, 2 in)
#   3  L-002  CS150  4"  mm   800.0  (tagged, spec CS150, 4 in)
#   4  L-002  SS150  4"  mm   600.0  (tagged, spec SS150, 4 in)
#   5  L-003  SS150  2"  mm   200.0  (tagged, spec SS150, 2 in)
#   6  NULL   CS150  2"  mm   300.0  (untagged — sin línea)
#   7  ""     CS150  4"  mm   100.0  (untagged — tag vacío)
#   8  "?"    SS150  4"  mm    50.0  (untagged — tag ?)
#
# Totales esperados (group_by line):
#   L-001  -> 2500.0 mm  (2 tramos)
#   L-002  -> 1400.0 mm  (2 tramos)
#   L-003  ->  200.0 mm  (1 tramo)
#   (SIN LÍNEA) -> 450.0 mm (3 tramos)
# Total global: 4550.0 mm, 8 tramos
# Untagged: 450.0 mm, 3 tramos
#
# Totales esperados (group_by spec):
#   CS150  -> 1000+1500+800+300+100 = 3700.0 mm  (5 tramos)
#   SS150  -> 600+200+50             =  850.0 mm  (3 tramos)
#
# Totales esperados (group_by size):
#   2"  -> 1000+1500+200+300 = 3000.0 mm  (4 tramos, dia=2 in)
#   4"  -> 800+600+100+50    = 1550.0 mm  (4 tramos, dia=4 in)
# ---------------------------------------------------------------------------
_PRC_ROWS = [
    (1, "L-001"),
    (2, "L-001"),
    (3, "L-002"),
    (4, "L-002"),
    (5, "L-003"),
    (6, None),
    (7, ""),
    (8, "?"),
]

_EI_ROWS = [
    # (PnPID, Spec, NominalDiameter, NominalUnit, LengthUnit)
    (1, "CS150", 2.0, "in", "mm"),
    (2, "CS150", 2.0, "in", "mm"),
    (3, "CS150", 4.0, "in", "mm"),
    (4, "SS150", 4.0, "in", "mm"),
    (5, "SS150", 2.0, "in", "mm"),
    (6, "CS150", 2.0, "in", "mm"),
    (7, "CS150", 4.0, "in", "mm"),
    (8, "SS150", 4.0, "in", "mm"),
]

_PIPE_ROWS = [
    # (PnPID, Length)
    (1, 1000.0),
    (2, 1500.0),
    (3,  800.0),
    (4,  600.0),
    (5,  200.0),
    (6,  300.0),
    (7,  100.0),
    (8,   50.0),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Proyecto sintético canónico."""
    return _make_project(
        tmp_path, "PIPE_LEN_TEST",
        _PRC_ROWS, _EI_ROWS, _PIPE_ROWS,
    )


@pytest.fixture
def result_line(proj: Path) -> dict:
    """pipe_length con group_by='line' y sin filtros ni tope."""
    return pipe_length(str(proj), {"limit": 0, "group_by": "line"})


@pytest.fixture
def result_spec(proj: Path) -> dict:
    """pipe_length con group_by='spec' y sin filtros ni tope."""
    return pipe_length(str(proj), {"limit": 0, "group_by": "spec"})


@pytest.fixture
def result_size(proj: Path) -> dict:
    """pipe_length con group_by='size' y sin filtros ni tope."""
    return pipe_length(str(proj), {"limit": 0, "group_by": "size"})


# ===========================================================================
# Parte 1 — Agregación pura: _build_pipe_length_aggregates
# ===========================================================================


class TestBuildPipeLengthAggregates:
    """Tests unitarios de la función pura _build_pipe_length_aggregates."""

    # Filas de prueba que imitan el formato del SELECT de pipe_length.
    _ROWS = [
        _row(line="L-001", spec="CS150", dia=2.0, dia_unit="in", length_unit="mm", length=1000.0),
        _row(line="L-001", spec="CS150", dia=2.0, dia_unit="in", length_unit="mm", length=1500.0),
        _row(line="L-002", spec="CS150", dia=4.0, dia_unit="in", length_unit="mm", length=800.0),
        _row(line="L-002", spec="SS150", dia=4.0, dia_unit="in", length_unit="mm", length=600.0),
        _row(line="L-003", spec="SS150", dia=2.0, dia_unit="in", length_unit="mm", length=200.0),
        _row(line=None,    spec="CS150", dia=2.0, dia_unit="in", length_unit="mm", length=300.0),
        _row(line="",      spec="CS150", dia=4.0, dia_unit="in", length_unit="mm", length=100.0),
        _row(line="?",     spec="SS150", dia=4.0, dia_unit="in", length_unit="mm", length=50.0),
    ]

    # -- group_by = "line" --------------------------------------------------

    def test_line_group_count(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "line")
        # L-001, L-002, L-003, (SIN LÍNEA)
        assert len(groups) == 4

    def test_line_group_tagged_totals(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "line")
        assert groups["L-001"]["lengths"]["mm"] == pytest.approx(2500.0)
        assert groups["L-002"]["lengths"]["mm"] == pytest.approx(1400.0)
        assert groups["L-003"]["lengths"]["mm"] == pytest.approx(200.0)

    def test_line_group_untagged_goes_to_no_line_label(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "line")
        assert _NO_LINE_LABEL in groups
        assert groups[_NO_LINE_LABEL]["lengths"]["mm"] == pytest.approx(450.0)

    def test_line_group_pipe_counts(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "line")
        assert groups["L-001"]["pipe_count"] == 2
        assert groups["L-002"]["pipe_count"] == 2
        assert groups["L-003"]["pipe_count"] == 1
        assert groups[_NO_LINE_LABEL]["pipe_count"] == 3

    def test_line_total_count(self):
        _, _, total, _ = _build_pipe_length_aggregates(self._ROWS, "line")
        assert total == 8

    def test_line_totals_by_unit(self):
        _, totals, _, _ = _build_pipe_length_aggregates(self._ROWS, "line")
        assert totals["mm"] == pytest.approx(4550.0)

    # -- group_by = "spec" --------------------------------------------------

    def test_spec_group_count(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "spec")
        # CS150, SS150
        assert len(groups) == 2

    def test_spec_group_cs150_total(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "spec")
        assert groups["CS150"]["lengths"]["mm"] == pytest.approx(3700.0)

    def test_spec_group_ss150_total(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "spec")
        assert groups["SS150"]["lengths"]["mm"] == pytest.approx(850.0)

    def test_spec_group_pipe_counts(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "spec")
        assert groups["CS150"]["pipe_count"] == 5
        assert groups["SS150"]["pipe_count"] == 3

    # -- group_by = "size" --------------------------------------------------

    def test_size_group_count(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "size")
        # 2" y 4"
        assert len(groups) == 2

    def test_size_group_2in_total(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "size")
        assert groups['2"']["lengths"]["mm"] == pytest.approx(3000.0)

    def test_size_group_4in_total(self):
        groups, _, _, _ = _build_pipe_length_aggregates(self._ROWS, "size")
        assert groups['4"']["lengths"]["mm"] == pytest.approx(1550.0)

    # -- untagged (independiente de group_by) --------------------------------

    def test_untagged_pipe_count(self):
        for gb in ("line", "spec", "size"):
            _, _, _, untagged = _build_pipe_length_aggregates(self._ROWS, gb)
            assert untagged["pipe_count"] == 3, f"group_by={gb}"

    def test_untagged_length(self):
        for gb in ("line", "spec", "size"):
            _, _, _, untagged = _build_pipe_length_aggregates(self._ROWS, gb)
            assert untagged["lengths"]["mm"] == pytest.approx(450.0), f"group_by={gb}"

    # -- tramo sin longitud no se cuenta ------------------------------------

    def test_none_length_row_ignored(self):
        rows = [
            _row(line="L-001", spec="CS150", dia=2.0, dia_unit="in", length_unit="mm", length=None),
            _row(line="L-001", spec="CS150", dia=2.0, dia_unit="in", length_unit="mm", length=500.0),
        ]
        groups, totals, total_count, _ = _build_pipe_length_aggregates(rows, "line")
        assert total_count == 1
        assert totals["mm"] == pytest.approx(500.0)


# ===========================================================================
# Parte 2 — Separación por unidad de longitud
# ===========================================================================


class TestLengthUnitSeparation:
    """Longitudes en distintas unidades nunca se suman juntas."""

    def _rows_mixed_units(self):
        return [
            _row(line="L-001", spec="CS150", dia=2.0, dia_unit="in", length_unit="mm", length=1000.0),
            _row(line="L-001", spec="CS150", dia=2.0, dia_unit="in", length_unit="in", length=3.0),
        ]

    def test_totals_by_unit_has_two_keys(self):
        _, totals, _, _ = _build_pipe_length_aggregates(
            self._rows_mixed_units(), "line"
        )
        assert "mm" in totals and "in" in totals

    def test_totals_units_not_summed_together(self):
        _, totals, _, _ = _build_pipe_length_aggregates(
            self._rows_mixed_units(), "line"
        )
        assert totals["mm"] == pytest.approx(1000.0)
        assert totals["in"] == pytest.approx(3.0)
        # Comprobación adicional: los valores son distintos y nunca mezclados
        assert 1000.0 + 3.0 not in totals.values()

    def test_mixed_unit_project_total_length_is_dict(self, tmp_path):
        """Cuando el proyecto tiene mm e in, total_length es dict, no escalar."""
        prc = [(1, "L-001"), (2, "L-001")]
        ei = [
            (1, "CS150", 2.0, "in", "mm"),
            (2, "CS150", 2.0, "in", "in"),
        ]
        pipe = [(1, 1000.0), (2, 3.0)]
        proj = _make_project(tmp_path, "MIXED_UNIT", prc, ei, pipe)
        r = pipe_length(str(proj), {"limit": 0})
        assert isinstance(r["total_length"], dict)
        assert "mm" in r["total_length"]
        assert "in" in r["total_length"]

    def test_mixed_unit_note_present(self, tmp_path):
        """Con unidades mezcladas aparece nota explicativa."""
        prc = [(1, "L-001"), (2, "L-001")]
        ei = [
            (1, "CS150", 2.0, "in", "mm"),
            (2, "CS150", 2.0, "in", "in"),
        ]
        pipe = [(1, 1000.0), (2, 3.0)]
        proj = _make_project(tmp_path, "MIXED_UNIT_NOTE", prc, ei, pipe)
        r = pipe_length(str(proj), {"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert "unidad" in combined or "unit" in combined


# ===========================================================================
# Parte 3 — Untagged
# ===========================================================================


class TestUntagged:
    def test_untagged_pipe_count_in_result(self, result_line):
        assert result_line["untagged"]["pipe_count"] == 3

    def test_untagged_length_in_result(self, result_line):
        assert result_line["untagged"]["length"] == pytest.approx(450.0)

    def test_untagged_as_group_when_group_by_line(self, result_line):
        """(SIN LÍNEA) debe aparecer como grupo cuando group_by='line'."""
        group_names = [g["group"] for g in result_line["groups"]]
        assert _NO_LINE_LABEL in group_names

    def test_untagged_group_length_matches_untagged_field(self, result_line):
        """La longitud del grupo (SIN LÍNEA) debe coincidir con untagged.length."""
        no_line_group = next(
            g for g in result_line["groups"] if g["group"] == _NO_LINE_LABEL
        )
        assert no_line_group["length"] == result_line["untagged"]["length"]

    def test_untagged_pipe_count_matches_group(self, result_line):
        no_line_group = next(
            g for g in result_line["groups"] if g["group"] == _NO_LINE_LABEL
        )
        assert no_line_group["pipe_count"] == result_line["untagged"]["pipe_count"]

    def test_untagged_present_in_spec_group_by(self, result_spec):
        """Cuando group_by='spec', untagged sigue reportándose en el campo 'untagged'."""
        assert result_spec["untagged"]["pipe_count"] == 3
        assert result_spec["untagged"]["length"] == pytest.approx(450.0)

    def test_untagged_present_in_size_group_by(self, result_size):
        assert result_size["untagged"]["pipe_count"] == 3

    def test_untagged_not_lost_from_spec_group_totals(self, result_spec):
        """Los tramos sin línea siguen contando en los totales de spec."""
        # PnPID 6 (NULL) y 7 ("") son CS150; PnPID 8 ("?") es SS150
        cs150_group = next(
            g for g in result_spec["groups"] if g["group"] == "CS150"
        )
        # CS150: 1000+1500+800+300+100 = 3700
        assert cs150_group["length"] == pytest.approx(3700.0)

    def test_untagged_tag_variants_all_caught(self):
        """NULL, vacío y '?' son todos untagged."""
        rows = [
            _row(line=None,  spec="A", dia=2.0, dia_unit="in", length_unit="mm", length=10.0),
            _row(line="",    spec="A", dia=2.0, dia_unit="in", length_unit="mm", length=20.0),
            _row(line="?",   spec="A", dia=2.0, dia_unit="in", length_unit="mm", length=30.0),
            _row(line="L-1", spec="A", dia=2.0, dia_unit="in", length_unit="mm", length=40.0),
        ]
        _, _, _, untagged = _build_pipe_length_aggregates(rows, "line")
        assert untagged["pipe_count"] == 3
        assert untagged["lengths"]["mm"] == pytest.approx(60.0)


# ===========================================================================
# Parte 4 — Redondeo: _round_lengths
# ===========================================================================


class TestRoundLengths:
    def test_single_unit_returns_scalar(self):
        result = _round_lengths({"mm": 1234.5678})
        assert isinstance(result, float)
        assert result == pytest.approx(1234.57)

    def test_two_decimals_scalar(self):
        assert _round_lengths({"mm": 100.0}) == pytest.approx(100.0)
        assert _round_lengths({"mm": 33.3333}) == pytest.approx(33.33)
        assert _round_lengths({"mm": 99.999}) == pytest.approx(100.0)

    def test_multiple_units_returns_dict(self):
        result = _round_lengths({"mm": 1000.0, "in": 3.1415})
        assert isinstance(result, dict)
        assert result["mm"] == pytest.approx(1000.0)
        assert result["in"] == pytest.approx(3.14)

    def test_two_decimals_dict(self):
        result = _round_lengths({"mm": 1.1115, "in": 2.9999})
        assert result["mm"] == pytest.approx(1.11)
        assert result["in"] == pytest.approx(3.0)

    def test_empty_dict_returns_empty_dict(self):
        """Un dict vacío devuelve dict vacío (no escalar)."""
        result = _round_lengths({})
        assert result == {}

    def test_scalar_is_float_type(self):
        """El resultado escalar es float, no int aunque el valor sea entero."""
        result = _round_lengths({"mm": 500.0})
        assert isinstance(result, float)


# ===========================================================================
# Parte 5 — Estructura de salida
# ===========================================================================


class TestOutputStructure:
    def test_ok_flag_true(self, result_line):
        assert result_line["ok"] is True

    def test_required_top_level_keys(self, result_line):
        required = (
            "ok", "project", "path", "limit", "group_by", "filters",
            "length_unit", "total_pipe_count", "total_length",
            "untagged", "group_count", "omitted", "groups", "notes",
        )
        for key in required:
            assert key in result_line, f"Falta clave: {key}"

    def test_groups_is_list(self, result_line):
        assert isinstance(result_line["groups"], list)

    def test_each_group_has_required_keys(self, result_line):
        for g in result_line["groups"]:
            assert "group" in g
            assert "pipe_count" in g
            assert "length" in g
            assert "length_unit" in g

    def test_untagged_has_pipe_count_and_length(self, result_line):
        unt = result_line["untagged"]
        assert "pipe_count" in unt
        assert "length" in unt

    def test_notes_is_list(self, result_line):
        assert isinstance(result_line["notes"], list)

    def test_filters_empty_when_no_filters(self, result_line):
        assert result_line["filters"] == {}

    def test_group_by_reflected(self, result_line):
        assert result_line["group_by"] == "line"

    def test_default_limit_is_50(self, proj):
        r = pipe_length(str(proj))
        assert r["limit"] == 50

    def test_limit_zero_reflected(self, result_line):
        assert result_line["limit"] == 0

    def test_project_name(self, proj, result_line):
        assert result_line["project"] == proj.name

    def test_length_unit_scalar_when_single_unit(self, result_line):
        """Si todos los tramos usan la misma unidad, length_unit es un string."""
        assert isinstance(result_line["length_unit"], str)
        assert result_line["length_unit"] == "mm"

    def test_total_pipe_count(self, result_line):
        assert result_line["total_pipe_count"] == 8

    def test_total_length(self, result_line):
        assert result_line["total_length"] == pytest.approx(4550.0)

    def test_group_count_equals_len_groups_when_no_limit(self, result_line):
        assert result_line["group_count"] == len(result_line["groups"])

    def test_notes_nonempty(self, result_line):
        assert len(result_line["notes"]) >= 1

    def test_notes_mention_pipe_table(self, result_line):
        combined = " ".join(result_line["notes"]).lower()
        assert "pipe" in combined or "tubería" in combined or "longitud" in combined


# ===========================================================================
# Parte 6 — Filtros
# ===========================================================================


class TestFilters:
    def test_line_filter(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "line": "L-001"})
        # L-001: 2 tramos, 2500.0 mm
        assert r["total_pipe_count"] == 2
        assert r["total_length"] == pytest.approx(2500.0)

    def test_line_filter_case_insensitive(self, proj):
        r_upper = pipe_length(str(proj), {"limit": 0, "line": "L-001"})
        r_lower = pipe_length(str(proj), {"limit": 0, "line": "l-001"})
        assert r_upper["total_pipe_count"] == r_lower["total_pipe_count"]

    def test_line_filter_echoed_normalized(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "line": "l-001"})
        assert r["filters"]["line"] == "L-001"

    def test_spec_filter(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "spec": "CS150"})
        # CS150: 5 tramos, 3700.0 mm
        assert r["total_pipe_count"] == 5
        assert r["total_length"] == pytest.approx(3700.0)

    def test_spec_filter_case_insensitive(self, proj):
        r_upper = pipe_length(str(proj), {"limit": 0, "spec": "CS150"})
        r_lower = pipe_length(str(proj), {"limit": 0, "spec": "cs150"})
        assert r_upper["total_pipe_count"] == r_lower["total_pipe_count"]

    def test_spec_filter_echoed_normalized(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "spec": "cs150"})
        assert r["filters"]["spec"] == "CS150"

    def test_size_filter_with_unit(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        # 2": PnPIDs 1,2,5,6 -> 1000+1500+200+300 = 3000.0 mm
        assert r["total_pipe_count"] == 4
        assert r["total_length"] == pytest.approx(3000.0)

    def test_size_filter_echoed(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        assert "size" in r["filters"]
        assert r["filters"]["size"]["value"] == 2.0

    def test_size_filter_without_unit_is_ignored(self, proj):
        """Un size sin unidad se ignora; el total no cambia."""
        r_all = pipe_length(str(proj), {"limit": 0})
        r_bare = pipe_length(str(proj), {"limit": 0, "size": 2.0})
        assert r_bare["total_pipe_count"] == r_all["total_pipe_count"]

    def test_size_filter_without_unit_adds_note(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "size": 2.0})
        combined = " ".join(r["notes"]).lower()
        assert (
            "size" in combined or "unidad" in combined
            or "unit" in combined or "ignorado" in combined
        )

    def test_size_filter_without_unit_not_in_filters_echo(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "size": 2.0})
        assert "size" not in r["filters"]

    def test_combined_filter_line_and_spec(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "line": "L-002", "spec": "SS150"})
        # L-002 + SS150: solo PnPID 4 -> 600.0 mm
        assert r["total_pipe_count"] == 1
        assert r["total_length"] == pytest.approx(600.0)

    def test_nonexistent_line_returns_zero(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "line": "NONEXISTENT"})
        assert r["total_pipe_count"] == 0
        assert r["total_length"] == 0
        assert r["groups"] == []

    def test_no_mutation_of_data_dict(self, proj):
        """pipe_length no debe mutar el dict data del llamador."""
        data = {"limit": 0, "group_by": "spec", "line": "L-001"}
        original = dict(data)
        pipe_length(str(proj), data)
        assert data == original

    def test_none_data_does_not_crash(self, proj):
        r = pipe_length(str(proj), None)
        assert r["ok"] is True

    def test_empty_data_does_not_crash(self, proj):
        r = pipe_length(str(proj), {})
        assert r["ok"] is True


# ===========================================================================
# Parte 7 — limit / omitted
# ===========================================================================


@pytest.fixture
def many_lines_proj(tmp_path: Path) -> Path:
    """Proyecto con 60 líneas distintas para tests de limit."""
    prc = [(i, f"L-{i:03}") for i in range(1, 61)]
    ei = [(i, "CS150", 2.0, "in", "mm") for i in range(1, 61)]
    pipe = [(i, float(i * 100)) for i in range(1, 61)]
    return _make_project(tmp_path, "MANY_LINES", prc, ei, pipe)


class TestLimitOmitted:
    def test_default_limit_50_caps_groups(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"group_by": "line"})
        assert len(r["groups"]) == 50
        assert r["group_count"] == 60
        assert r["omitted"] == 10

    def test_limit_zero_returns_all_groups(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"limit": 0, "group_by": "line"})
        assert len(r["groups"]) == 60
        assert r["group_count"] == 60
        assert r["omitted"] == 0

    def test_limit_custom_caps_groups(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"limit": 10, "group_by": "line"})
        assert len(r["groups"]) == 10
        assert r["omitted"] == 50

    def test_omitted_formula(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"limit": 25, "group_by": "line"})
        assert r["omitted"] == r["group_count"] - len(r["groups"])

    def test_total_pipe_count_unaffected_by_limit(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"limit": 5, "group_by": "line"})
        assert r["total_pipe_count"] == 60

    def test_total_length_unaffected_by_limit(self, many_lines_proj):
        r_all = pipe_length(str(many_lines_proj), {"limit": 0, "group_by": "line"})
        r_cap = pipe_length(str(many_lines_proj), {"limit": 5, "group_by": "line"})
        assert r_cap["total_length"] == r_all["total_length"]

    def test_limit_reflected_in_output(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"limit": 15, "group_by": "line"})
        assert r["limit"] == 15

    def test_group_count_always_total_before_cap(self, many_lines_proj):
        r = pipe_length(str(many_lines_proj), {"limit": 5, "group_by": "line"})
        assert r["group_count"] == 60


# ===========================================================================
# Parte 8 — group_by inválido
# ===========================================================================


class TestGroupByInvalid:
    def test_invalid_group_by_falls_back_to_line(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "group_by": "category"})
        assert r["group_by"] == "line"

    def test_invalid_group_by_adds_note(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "group_by": "foobar"})
        combined = " ".join(r["notes"]).lower()
        assert (
            "reconoc" in combined or "válido" in combined
            or "line" in combined or "group_by" in combined
        )

    def test_invalid_group_by_still_returns_ok_true(self, proj):
        r = pipe_length(str(proj), {"limit": 0, "group_by": "INVALID"})
        assert r["ok"] is True


# ===========================================================================
# Parte 9 — Degradación de esquema
# ===========================================================================


class TestSchemaDegradation:
    def test_no_pipe_table_returns_ok(self, tmp_path):
        """Sin tabla Pipe: ok=True, groups=[], totales a 0."""
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in", "mm")]
        proj = _make_project(
            tmp_path, "NO_PIPE_TABLE", prc, ei, [],
            create_pipe_table=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["groups"] == []
        assert r["total_pipe_count"] == 0
        assert r["total_length"] == 0

    def test_no_pipe_table_adds_note(self, tmp_path):
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in", "mm")]
        proj = _make_project(
            tmp_path, "NO_PIPE_TABLE_NOTE", prc, ei, [],
            create_pipe_table=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert "pipe" in combined or "tabla" in combined or "longitud" in combined

    def test_no_length_col_returns_ok(self, tmp_path):
        """Sin columna Length en Pipe: ok=True, groups=[], totales a 0."""
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in", "mm")]
        pipe = [(1,)]  # Solo PnPID, sin Length
        proj = _make_project(
            tmp_path, "NO_LENGTH_COL", prc, ei, pipe,
            include_length_col=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["groups"] == []
        assert r["total_pipe_count"] == 0

    def test_no_length_col_adds_note(self, tmp_path):
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in", "mm")]
        pipe = [(1,)]
        proj = _make_project(
            tmp_path, "NO_LENGTH_COL_NOTE", prc, ei, pipe,
            include_length_col=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert "length" in combined or "longitud" in combined or "columna" in combined

    def test_no_length_unit_col_returns_ok(self, tmp_path):
        """Sin columna LengthUnit en EngineeringItems: ok=True, length_unit=None."""
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in")]  # 4 campos, sin LengthUnit
        pipe = [(1, 500.0)]
        proj = _make_project(
            tmp_path, "NO_LUNIT_COL", prc, ei, pipe,
            include_length_unit_col=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        assert r["ok"] is True

    def test_no_length_unit_col_length_unit_is_none_or_question(self, tmp_path):
        """Sin LengthUnit: length_unit es None o '?' (no se asume 'mm')."""
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in")]
        pipe = [(1, 500.0)]
        proj = _make_project(
            tmp_path, "NO_LUNIT_COL2", prc, ei, pipe,
            include_length_unit_col=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        # length_unit debe ser None (o "?"), NO "mm" asumido
        assert r["length_unit"] in (None, "?"), (
            f"Se esperaba None o '?', se obtuvo: {r['length_unit']!r}"
        )

    def test_no_length_unit_col_adds_note(self, tmp_path):
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in")]
        pipe = [(1, 500.0)]
        proj = _make_project(
            tmp_path, "NO_LUNIT_COL_NOTE", prc, ei, pipe,
            include_length_unit_col=False,
        )
        r = pipe_length(str(proj), {"limit": 0})
        combined = " ".join(r["notes"]).lower()
        assert (
            "lengthunit" in combined or "unidad" in combined
            or "length_unit" in combined or "null" in combined
        )

    def test_empty_pipe_table_no_crash(self, tmp_path):
        """Tabla Pipe vacía: ok=True, grupos vacíos."""
        prc = [(1, "L-001")]
        ei = [(1, "CS150", 2.0, "in", "mm")]
        pipe = []  # sin filas
        proj = _make_project(tmp_path, "EMPTY_PIPE", prc, ei, pipe)
        r = pipe_length(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["groups"] == []
        assert r["total_pipe_count"] == 0
        assert r["total_length"] == 0


# ===========================================================================
# Parte 10 — Solo PartCategory='Pipe' (join correcto por PnPID)
# ===========================================================================


class TestOnlyPipeCategoryJoined:
    """La tabla Pipe solo recoge tuberías; el join no suma longitudes ajenas."""

    def test_pipe_rows_joined_correctly(self, proj, result_line):
        """Los 8 PnPIDs de Pipe se casan correctamente con PipeRunComponent."""
        assert result_line["total_pipe_count"] == 8

    def test_no_extra_lengths_from_non_pipe_components(self, tmp_path):
        """Si una entidad no tiene fila en Pipe, no aparece en el total."""
        # PnPID 1 está en PipeRunComponent+EngineeringItems pero NO en Pipe.
        prc = [(1, "L-001"), (2, "L-001")]
        ei = [(1, "CS150", 2.0, "in", "mm"), (2, "CS150", 2.0, "in", "mm")]
        pipe = [(2, 500.0)]  # Solo PnPID 2 tiene longitud
        proj = _make_project(tmp_path, "PARTIAL_PIPE", prc, ei, pipe)
        r = pipe_length(str(proj), {"limit": 0})
        assert r["total_pipe_count"] == 1
        assert r["total_length"] == pytest.approx(500.0)


# ===========================================================================
# Parte 11 — Ordenación de grupos
# ===========================================================================


class TestGroupOrdering:
    def test_groups_ordered_by_length_desc(self, result_line):
        lengths = []
        for g in result_line["groups"]:
            ln = g["length"]
            lengths.append(ln if isinstance(ln, (int, float)) else sum(ln.values()))
        assert lengths == sorted(lengths, reverse=True)

    def test_same_length_ordered_by_group_name_asc(self, tmp_path):
        """Si dos grupos tienen la misma longitud, se ordenan por nombre asc."""
        prc = [(1, "L-AAA"), (2, "L-BBB")]
        ei = [(1, "CS150", 2.0, "in", "mm"), (2, "CS150", 2.0, "in", "mm")]
        pipe = [(1, 500.0), (2, 500.0)]
        proj = _make_project(tmp_path, "SAME_LEN", prc, ei, pipe)
        r = pipe_length(str(proj), {"limit": 0, "group_by": "line"})
        groups = [g["group"] for g in r["groups"]]
        assert groups[0] == "L-AAA"
        assert groups[1] == "L-BBB"


# ===========================================================================
# Parte 12 — Garantía de solo lectura
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, proj):
        db = proj / "Piping.dcf"
        before_bytes = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        pipe_length(str(proj), {"limit": 0})

        assert db.read_bytes() == before_bytes
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, proj):
        r = pipe_length(str(proj / "Piping.dcf"), {"limit": 0})
        assert r["ok"] is True


# ===========================================================================
# Parte 13 — Dispatch en server.py
# ===========================================================================


class TestServerDispatch:
    def test_pipe_length_callable_in_module(self):
        from autocad_mcp import plant3d_query
        assert callable(getattr(plant3d_query, "pipe_length", None))

    def test_pipe_length_operation_reachable(self, proj, monkeypatch):
        """El servidor despacha operation='pipe_length' a plant3d_query.pipe_length."""
        captured = {}

        def _fake(project, data=None):
            captured["project"] = project
            captured["data"] = data
            return {
                "ok": True, "project": "X", "path": "X", "limit": 0,
                "group_by": "line", "filters": {}, "length_unit": "mm",
                "total_pipe_count": 0, "total_length": 0,
                "untagged": {"pipe_count": 0, "length": 0},
                "group_count": 0, "omitted": 0, "groups": [], "notes": [],
            }

        import autocad_mcp.plant3d_query as pq
        monkeypatch.setattr(pq, "pipe_length", _fake)

        import importlib
        import autocad_mcp.server as srv
        importlib.reload(srv)

        import asyncio

        async def _run():
            return await srv.plant3d(
                operation="pipe_length",
                data={"project": str(proj), "limit": 0},
            )

        asyncio.run(_run())
        assert "project" in captured, "pipe_length() nunca fue invocado desde el servidor"


# ===========================================================================
# Parte 14 — Test de integración (real project, skipeable)
# ===========================================================================

_REAL_DCF = (
    r"\\172.16.0.220\Comun\06-INFORMÁTICA\3_UTILIDADES\MCP-Plant3D\Proyectos"
    r"\23099 - AIR LIQUIDE HUELVA\Piping.dcf"
)


def _real_dcf_accessible() -> bool:
    try:
        return Path(_REAL_DCF).is_file()
    except (OSError, PermissionError):
        return False


@pytest.mark.skipif(
    not _real_dcf_accessible(),
    reason="Proyecto real no accesible desde este entorno",
)
class TestIntegrationRealProject:
    """Sanity check contra AIR LIQUIDE HUELVA (solo si la ruta es accesible)."""

    @pytest.fixture(scope="class")
    def real_result(self):
        project_path = str(Path(_REAL_DCF).parent)
        return pipe_length(project_path, {"limit": 0, "group_by": "line"})

    def test_ok_true(self, real_result):
        assert real_result["ok"] is True

    def test_total_pipe_count_approx(self, real_result):
        """Se esperan ~1679 tramos de tubería."""
        assert 1600 <= real_result["total_pipe_count"] <= 1750

    def test_total_length_approx(self, real_result):
        """Se esperan ~1.635.082,3 mm."""
        total = real_result["total_length"]
        if isinstance(total, dict):
            total = sum(total.values())
        assert 1_500_000 <= total <= 1_800_000

    def test_untagged_count_approx(self, real_result):
        """Se esperan ~379 tramos sin línea."""
        assert 300 <= real_result["untagged"]["pipe_count"] <= 450

    def test_untagged_length_approx(self, real_result):
        """Se esperan ~301.922,6 mm de tubería sin línea."""
        unt_len = real_result["untagged"]["length"]
        if isinstance(unt_len, dict):
            unt_len = sum(unt_len.values())
        assert 250_000 <= unt_len <= 360_000

    def test_tagged_count_approx(self, real_result):
        """Se esperan ~1300 tramos con línea válida."""
        tagged = real_result["total_pipe_count"] - real_result["untagged"]["pipe_count"]
        assert 1200 <= tagged <= 1400
