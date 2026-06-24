"""Tests for plant3d_query.weld_list — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) that include the Buttweld / Socketweld / TapWeld
tables and the P3dLineGroup / P3dLineGroupPartRelationship tables required
by weld_list. No real project databases are ever touched.

Key invariants verified:
1.  Aggregación pura _build_weld_aggregates (5 group_by: line/size/spec/shop_field/type).
2.  _norm_shop_field: SHOP/shop/' Field '/None/valor raro → shop/field/(desconocido).
3.  Subtipo por tabla: Buttweld→butt, Socketweld→socket, TapWeld→tap; by_type refleja los tres.
4.  Untagged: Tag NULL/''/'?' o sin relación → untagged; grupo "(SIN LÍNEA)" en group_by=line.
5.  Filtros: line, spec, size con unidad (filtra) y sin unidad (ignora + nota),
    shop_field, weld_type, combinados. by_type/by_shop_field acotados al alcance filtrado.
6.  limit/omitted: acota grupos, 0 = sin tope, omitted correcto, totales no afectados.
7.  group_by inválido → cae a "line" con nota.
8.  Degradación de esquema:
    - Ninguna tabla de soldadura → ok:True, total_welds 0, listas vacías, nota.
    - Solo algunas tablas → usa presentes + nota de ausentes.
    - Shop_Field ausente en una tabla → "(desconocido)" + nota.
    - Faltan tablas/columnas de relación de línea → todas untagged/"(SIN LÍNEA)" + nota.
9.  data no se muta.
10. Solo lectura: bytes del .dcf sin cambios.
11. Test de integración con proyecto real (skip si no accesible).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import (
    _NO_LINE_LABEL,
    _build_weld_aggregates,
    _norm_shop_field,
    _UNKNOWN_SHOP_FIELD,
    weld_list,
)


# ===========================================================================
# Helpers: fila-dict sintética para tests unitarios puros (sin SQLite)
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
    *,
    butt_rows: list[tuple] | None = None,
    socket_rows: list[tuple] | None = None,
    tap_rows: list[tuple] | None = None,
    ei_rows: list[tuple] | None = None,
    line_group_rows: list[tuple] | None = None,
    rel_rows: list[tuple] | None = None,
    include_shop_field_butt: bool = True,
    include_shop_field_socket: bool = True,
    include_shop_field_tap: bool = True,
    create_buttweld: bool = True,
    create_socketweld: bool = True,
    create_tapweld: bool = True,
    create_line_group: bool = True,
    create_rel: bool = True,
) -> None:
    """Crea un Piping.dcf mínimo con las tablas de soldadura y relación de líneas.

    butt_rows / socket_rows / tap_rows: (PnPID, Shop_Field)
    ei_rows:              (PnPID, Spec, NominalDiameter, NominalUnit)
    line_group_rows:      (PnPID, Tag)
    rel_rows:             (Part, LineGroup)   — Part = weld PnPID, LineGroup = P3dLineGroup.PnPID
    """
    con = sqlite3.connect(str(path))
    try:
        # EngineeringItems
        con.execute(
            "CREATE TABLE EngineeringItems "
            "(PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
        )
        for pnpid, spec, dia, unit in (ei_rows or []):
            con.execute(
                "INSERT INTO EngineeringItems (PnPID, Spec, NominalDiameter, NominalUnit) "
                "VALUES (?, ?, ?, ?)",
                (pnpid, spec, dia, unit),
            )

        # Buttweld
        if create_buttweld:
            sf_col = ", Shop_Field TEXT" if include_shop_field_butt else ""
            con.execute(f"CREATE TABLE Buttweld (PnPID INTEGER, WeldNumber TEXT{sf_col})")
            for pnpid, sf in (butt_rows or []):
                if include_shop_field_butt:
                    con.execute(
                        "INSERT INTO Buttweld (PnPID, Shop_Field) VALUES (?, ?)", (pnpid, sf)
                    )
                else:
                    con.execute("INSERT INTO Buttweld (PnPID) VALUES (?)", (pnpid,))

        # Socketweld
        if create_socketweld:
            sf_col = ", Shop_Field TEXT" if include_shop_field_socket else ""
            con.execute(f"CREATE TABLE Socketweld (PnPID INTEGER, WeldNumber TEXT{sf_col})")
            for pnpid, sf in (socket_rows or []):
                if include_shop_field_socket:
                    con.execute(
                        "INSERT INTO Socketweld (PnPID, Shop_Field) VALUES (?, ?)", (pnpid, sf)
                    )
                else:
                    con.execute("INSERT INTO Socketweld (PnPID) VALUES (?)", (pnpid,))

        # TapWeld
        if create_tapweld:
            sf_col = ", Shop_Field TEXT" if include_shop_field_tap else ""
            con.execute(f"CREATE TABLE TapWeld (PnPID INTEGER, WeldNumber TEXT{sf_col})")
            for pnpid, sf in (tap_rows or []):
                if include_shop_field_tap:
                    con.execute(
                        "INSERT INTO TapWeld (PnPID, Shop_Field) VALUES (?, ?)", (pnpid, sf)
                    )
                else:
                    con.execute("INSERT INTO TapWeld (PnPID) VALUES (?)", (pnpid,))

        # P3dLineGroup
        if create_line_group:
            con.execute("CREATE TABLE P3dLineGroup (PnPID INTEGER, Tag TEXT)")
            for pnpid, tag in (line_group_rows or []):
                con.execute(
                    "INSERT INTO P3dLineGroup (PnPID, Tag) VALUES (?, ?)", (pnpid, tag)
                )

        # P3dLineGroupPartRelationship
        if create_rel:
            con.execute(
                "CREATE TABLE P3dLineGroupPartRelationship (Part INTEGER, LineGroup INTEGER)"
            )
            for part, linegroup in (rel_rows or []):
                con.execute(
                    "INSERT INTO P3dLineGroupPartRelationship (Part, LineGroup) VALUES (?, ?)",
                    (part, linegroup),
                )

        con.commit()
    finally:
        con.close()


def _make_project(
    base: Path,
    name: str,
    **dcf_kw,
) -> Path:
    """Crea una carpeta de proyecto mínima con Project.xml + Piping.dcf."""
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", **dcf_kw)
    return proj


# ---------------------------------------------------------------------------
# Dataset canónico de prueba
#
# PnPIDs:
#   Buttweld:   1 (SHOP, L-001, CS150, 2"), 2 (FIELD, L-001, CS150, 2"),
#               3 (SHOP, L-002, SS150, 4"), 4 (FIELD, L-002, SS150, 4"),
#               5 (SHOP, L-003, CS150, 2"),
#               6 (SHOP, untagged, CS150, 2"),
#               7 (SHOP, untagged, CS150, 4"),
#               8 (FIELD, untagged, SS150, 4")
#   Socketweld: 9 (SHOP, L-001, CS150, 2"), 10 (FIELD, L-002, SS150, 4")
#   TapWeld:    11 (SHOP, L-003, CS150, 2"), 12 (FIELD, L-003, SS150, 4")
#
# P3dLineGroup:  PnPID 100→"L-001", 200→"L-002", 300→"L-003"
# P3dLineGroupPartRelationship:
#   welds 1,2,9 → LineGroup 100 (L-001)
#   welds 3,4,10 → LineGroup 200 (L-002)
#   welds 5,11,12 → LineGroup 300 (L-003)
#   welds 6,7,8 NO tienen relación → untagged
#
# Totales:
#   Total = 12 soldaduras
#   by_type: butt=8, socket=2, tap=2
#   by_shop_field: shop=7, field=5
#     (butt: shop=5, field=3 ; socket: shop=1, field=1 ; tap: shop=1, field=1)
#   untagged: 3 (PnPIDs 6,7,8)
#
#   group_by="line":
#     L-001 → 3, L-002 → 3, L-003 → 3, (SIN LÍNEA) → 3
#   group_by="spec":
#     CS150: PnPIDs 1,2,5,6,7,9,11 → 7; SS150: PnPIDs 3,4,8,10,12 → 5
#   group_by="size":
#     2": PnPIDs 1,2,5,6,9,11 → 6; 4": PnPIDs 3,4,7,8,10,12 → 6
#     (formato: _fmt_size(2.0,"in")='2"', no "2 in")
#   group_by="shop_field":
#     shop → 7, field → 5
#   group_by="type":
#     butt → 8, socket → 2, tap → 2
# ---------------------------------------------------------------------------

_BUTT_ROWS = [
    (1, "SHOP"),   # L-001, CS150, 2in
    (2, "FIELD"),  # L-001, CS150, 2in
    (3, "SHOP"),   # L-002, SS150, 4in
    (4, "FIELD"),  # L-002, SS150, 4in
    (5, "SHOP"),   # L-003, CS150, 2in
    (6, "SHOP"),   # untagged, CS150, 2in
    (7, "SHOP"),   # untagged, CS150, 4in
    (8, "FIELD"),  # untagged, SS150, 4in
]
_SOCKET_ROWS = [
    (9, "SHOP"),   # L-001, CS150, 2in
    (10, "FIELD"), # L-002, SS150, 4in
]
_TAP_ROWS = [
    (11, "SHOP"),  # L-003, CS150, 2in
    (12, "FIELD"), # L-003, SS150, 4in
]
_EI_ROWS = [
    # (PnPID, Spec, NominalDiameter, NominalUnit)
    (1,  "CS150", 2.0, "in"),
    (2,  "CS150", 2.0, "in"),
    (3,  "SS150", 4.0, "in"),
    (4,  "SS150", 4.0, "in"),
    (5,  "CS150", 2.0, "in"),
    (6,  "CS150", 2.0, "in"),
    (7,  "CS150", 4.0, "in"),
    (8,  "SS150", 4.0, "in"),
    (9,  "CS150", 2.0, "in"),
    (10, "SS150", 4.0, "in"),
    (11, "CS150", 2.0, "in"),
    (12, "SS150", 4.0, "in"),
]
_LINE_GROUP_ROWS = [
    (100, "L-001"),
    (200, "L-002"),
    (300, "L-003"),
]
_REL_ROWS = [
    # (Part=weld PnPID, LineGroup=P3dLineGroup.PnPID)
    (1, 100), (2, 100), (9, 100),           # L-001
    (3, 200), (4, 200), (10, 200),          # L-002
    (5, 300), (11, 300), (12, 300),         # L-003
    # PnPIDs 6, 7, 8 → NO tienen relación → untagged
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Proyecto sintético canónico con las 3 tablas de soldadura."""
    return _make_project(
        tmp_path, "WELD_TEST",
        butt_rows=_BUTT_ROWS,
        socket_rows=_SOCKET_ROWS,
        tap_rows=_TAP_ROWS,
        ei_rows=_EI_ROWS,
        line_group_rows=_LINE_GROUP_ROWS,
        rel_rows=_REL_ROWS,
    )


@pytest.fixture
def result_line(proj: Path) -> dict:
    """weld_list con group_by='line' y sin filtros ni tope."""
    return weld_list(str(proj), {"limit": 0, "group_by": "line"})


@pytest.fixture
def result_spec(proj: Path) -> dict:
    """weld_list con group_by='spec' y sin filtros ni tope."""
    return weld_list(str(proj), {"limit": 0, "group_by": "spec"})


@pytest.fixture
def result_size(proj: Path) -> dict:
    """weld_list con group_by='size' y sin filtros ni tope."""
    return weld_list(str(proj), {"limit": 0, "group_by": "size"})


@pytest.fixture
def result_shop_field(proj: Path) -> dict:
    """weld_list con group_by='shop_field' y sin filtros ni tope."""
    return weld_list(str(proj), {"limit": 0, "group_by": "shop_field"})


@pytest.fixture
def result_type(proj: Path) -> dict:
    """weld_list con group_by='type' y sin filtros ni tope."""
    return weld_list(str(proj), {"limit": 0, "group_by": "type"})


# ===========================================================================
# Parte 1 — Agregación pura: _build_weld_aggregates
# ===========================================================================


class TestBuildWeldAggregatesGroupByLine:
    """Tests unitarios de _build_weld_aggregates con group_by='line'."""

    @pytest.fixture
    def rows(self):
        """12 filas canónicas (mismos datos que el proyecto SQLite canónico)."""
        return [
            # butt / L-001 / CS150 / 2in / shop
            _row(weld_type="butt", shop_field="shop", spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt", shop_field="field", spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            # butt / L-002 / SS150 / 4in
            _row(weld_type="butt", shop_field="shop", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="butt", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            # butt / L-003 / CS150 / 2in
            _row(weld_type="butt", shop_field="shop", spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            # butt / untagged (None, empty, "?")
            _row(weld_type="butt", shop_field="shop", spec="CS150", dia=2.0, dia_unit="in", line=None),
            _row(weld_type="butt", shop_field="shop", spec="CS150", dia=4.0, dia_unit="in", line=""),
            _row(weld_type="butt", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="?"),
            # socket / L-001, L-002
            _row(weld_type="socket", shop_field="shop", spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="socket", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            # tap / L-003
            _row(weld_type="tap", shop_field="shop", spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            _row(weld_type="tap", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-003"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_weld_aggregates(rows, "line")

    def test_total_count(self, agg):
        _, _, _, total, _ = agg
        assert total == 12

    def test_untagged_count(self, agg):
        _, _, _, _, untagged = agg
        assert untagged["weld_count"] == 3

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert set(groups.keys()) == {"L-001", "L-002", "L-003", _NO_LINE_LABEL}

    def test_group_l001(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-001"] == 3

    def test_group_l002(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-002"] == 3

    def test_group_l003(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-003"] == 3

    def test_group_no_line(self, agg):
        groups, _, _, _, _ = agg
        assert groups[_NO_LINE_LABEL] == 3

    def test_by_type_butt(self, agg):
        _, by_type, _, _, _ = agg
        assert by_type.get("butt") == 8

    def test_by_type_socket(self, agg):
        _, by_type, _, _, _ = agg
        assert by_type.get("socket") == 2

    def test_by_type_tap(self, agg):
        _, by_type, _, _, _ = agg
        assert by_type.get("tap") == 2

    def test_by_shop_field_shop(self, agg):
        # butt: shop=5, socket: shop=1, tap: shop=1 → 7
        _, _, by_sf, _, _ = agg
        assert by_sf.get("shop") == 7

    def test_by_shop_field_field(self, agg):
        # butt: field=3, socket: field=1, tap: field=1 → 5
        _, _, by_sf, _, _ = agg
        assert by_sf.get("field") == 5


class TestBuildWeldAggregatesGroupBySpec:
    @pytest.fixture
    def rows(self):
        return [
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt",   shop_field="field", spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt",   shop_field="shop",  spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="butt",   shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line=None),
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=4.0, dia_unit="in", line=""),
            _row(weld_type="butt",   shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="?"),
            _row(weld_type="socket", shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="socket", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="tap",    shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            _row(weld_type="tap",    shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-003"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_weld_aggregates(rows, "spec")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert set(groups.keys()) == {"CS150", "SS150"}

    def test_cs150_count(self, agg):
        # idx 0,1,4,5,6 (butt) + idx 8 (socket) + idx 10 (tap) = 7 filas CS150
        groups, _, _, _, _ = agg
        assert groups["CS150"] == 7

    def test_ss150_count(self, agg):
        # idx 2,3,7 (butt) + idx 9 (socket) + idx 11 (tap) = 5 filas SS150
        groups, _, _, _, _ = agg
        assert groups["SS150"] == 5

    def test_total(self, agg):
        _, _, _, total, _ = agg
        assert total == 12

    def test_untagged_in_spec_group(self, agg):
        """En group_by='spec', los untagged caen en su grupo de spec natural."""
        groups, _, _, _, _ = agg
        # PnPIDs 6→CS150, 7→CS150, 8→SS150 están en sus grupos de spec
        # No hay grupo separado "(SIN LÍNEA)" en group_by=spec
        assert _NO_LINE_LABEL not in groups


class TestBuildWeldAggregatesGroupBySize:
    @pytest.fixture
    def rows(self):
        return [
            # 2 in
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt",   shop_field="field", spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line=None),
            _row(weld_type="socket", shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="tap",    shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            # 4 in
            _row(weld_type="butt",   shop_field="shop",  spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="butt",   shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=4.0, dia_unit="in", line=""),
            _row(weld_type="butt",   shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="?"),
            _row(weld_type="socket", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="tap",    shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-003"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_weld_aggregates(rows, "size")

    def test_two_size_groups(self, agg):
        groups, _, _, _, _ = agg
        assert len(groups) == 2

    def test_2in_count(self, agg):
        # _fmt_size(2.0, "in") → '2"' (no "2 in")
        groups, _, _, _, _ = agg
        # idx 0,1,2,3,4,5 → 6 filas de 2"
        assert groups.get('2"') == 6

    def test_4in_count(self, agg):
        # _fmt_size(4.0, "in") → '4"'
        groups, _, _, _, _ = agg
        # idx 6,7,8,9,10,11 → 6 filas de 4"
        assert groups.get('4"') == 6

    def test_no_line_label_absent_in_size_group_by(self, agg):
        """En group_by='size', los untagged caen en su grupo de size."""
        groups, _, _, _, _ = agg
        assert _NO_LINE_LABEL not in groups


class TestBuildWeldAggregatesGroupByShopField:
    @pytest.fixture
    def rows(self):
        return [
            _row(weld_type="butt", shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt", shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt", shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-002"),
            _row(weld_type="butt", shop_field=_UNKNOWN_SHOP_FIELD, spec="CS150", dia=2.0, dia_unit="in", line=None),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_weld_aggregates(rows, "shop_field")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert "shop" in groups
        assert "field" in groups
        assert _UNKNOWN_SHOP_FIELD in groups

    def test_shop_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["shop"] == 2

    def test_field_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["field"] == 1

    def test_unknown_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups[_UNKNOWN_SHOP_FIELD] == 1


class TestBuildWeldAggregatesGroupByType:
    @pytest.fixture
    def rows(self):
        return [
            _row(weld_type="butt",   shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="butt",   shop_field="field", spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="socket", shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-001"),
            _row(weld_type="tap",    shop_field="shop",  spec="CS150", dia=2.0, dia_unit="in", line="L-003"),
            _row(weld_type="tap",    shop_field="field", spec="SS150", dia=4.0, dia_unit="in", line="L-003"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_weld_aggregates(rows, "type")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert set(groups.keys()) == {"butt", "socket", "tap"}

    def test_butt_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["butt"] == 2

    def test_socket_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["socket"] == 1

    def test_tap_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["tap"] == 2


class TestBuildWeldAggregatesEmpty:
    def test_empty_rows(self):
        groups, by_type, by_sf, total, untagged = _build_weld_aggregates([], "line")
        assert groups == {}
        assert by_type == {}
        assert by_sf == {}
        assert total == 0
        assert untagged == {"weld_count": 0}


# ===========================================================================
# Parte 2 — _norm_shop_field
# ===========================================================================


class TestNormShopField:
    def test_shop_upper(self):
        assert _norm_shop_field("SHOP") == "shop"

    def test_shop_lower(self):
        assert _norm_shop_field("shop") == "shop"

    def test_shop_mixed(self):
        assert _norm_shop_field("Shop") == "shop"

    def test_field_upper(self):
        assert _norm_shop_field("FIELD") == "field"

    def test_field_lower(self):
        assert _norm_shop_field("field") == "field"

    def test_field_with_spaces(self):
        assert _norm_shop_field(" Field ") == "field"

    def test_none_gives_unknown(self):
        assert _norm_shop_field(None) == _UNKNOWN_SHOP_FIELD

    def test_empty_string_gives_unknown(self):
        assert _norm_shop_field("") == _UNKNOWN_SHOP_FIELD

    def test_spaces_only_gives_unknown(self):
        assert _norm_shop_field("   ") == _UNKNOWN_SHOP_FIELD

    def test_random_value_gives_unknown(self):
        assert _norm_shop_field("whatever") == _UNKNOWN_SHOP_FIELD

    def test_return_value_is_always_string(self):
        for v in (None, "", "SHOP", "FIELD", "x"):
            result = _norm_shop_field(v)
            assert isinstance(result, str)


# ===========================================================================
# Parte 3 — Estructura de salida de weld_list
# ===========================================================================


class TestOutputStructure:
    def test_ok_true(self, result_line):
        assert result_line["ok"] is True

    def test_required_top_level_keys(self, result_line):
        required = (
            "ok", "project", "path", "limit", "group_by", "filters",
            "total_welds", "by_type", "by_shop_field", "untagged",
            "group_count", "omitted", "groups", "notes",
        )
        for key in required:
            assert key in result_line, f"Falta la clave: {key}"

    def test_project_name(self, proj, result_line):
        assert result_line["project"] == proj.name

    def test_path_is_string(self, result_line):
        assert isinstance(result_line["path"], str)

    def test_groups_is_list(self, result_line):
        assert isinstance(result_line["groups"], list)

    def test_by_type_is_list(self, result_line):
        assert isinstance(result_line["by_type"], list)

    def test_by_shop_field_is_list(self, result_line):
        assert isinstance(result_line["by_shop_field"], list)

    def test_notes_is_list(self, result_line):
        assert isinstance(result_line["notes"], list)

    def test_untagged_has_weld_count(self, result_line):
        assert "weld_count" in result_line["untagged"]

    def test_group_entries_have_group_and_weld_count(self, result_line):
        for g in result_line["groups"]:
            assert "group" in g
            assert "weld_count" in g

    def test_by_type_entries_have_type_and_count(self, result_line):
        for entry in result_line["by_type"]:
            assert "type" in entry
            assert "count" in entry

    def test_by_shop_field_entries_have_shop_field_and_count(self, result_line):
        for entry in result_line["by_shop_field"]:
            assert "shop_field" in entry
            assert "count" in entry

    def test_group_by_echoed(self, result_line):
        assert result_line["group_by"] == "line"

    def test_default_limit_is_50(self, proj):
        r = weld_list(str(proj))
        assert r["limit"] == 50

    def test_limit_zero_reflected(self, result_line):
        assert result_line["limit"] == 0

    def test_filters_empty_without_filters(self, result_line):
        assert result_line["filters"] == {}


# ===========================================================================
# Parte 4 — Totales y desgloses globales
# ===========================================================================


class TestTotalsAndBreakdowns:
    def test_total_welds(self, result_line):
        assert result_line["total_welds"] == 12

    def test_untagged_count(self, result_line):
        assert result_line["untagged"]["weld_count"] == 3

    def test_by_type_butt(self, result_line):
        butt = next((e for e in result_line["by_type"] if e["type"] == "butt"), None)
        assert butt is not None
        assert butt["count"] == 8

    def test_by_type_socket(self, result_line):
        sock = next((e for e in result_line["by_type"] if e["type"] == "socket"), None)
        assert sock is not None
        assert sock["count"] == 2

    def test_by_type_tap(self, result_line):
        tap = next((e for e in result_line["by_type"] if e["type"] == "tap"), None)
        assert tap is not None
        assert tap["count"] == 2

    def test_by_shop_field_shop(self, result_line):
        # butt shop=5, socket shop=1, tap shop=1 → 7
        shop = next((e for e in result_line["by_shop_field"] if e["shop_field"] == "shop"), None)
        assert shop is not None
        assert shop["count"] == 7

    def test_by_shop_field_field(self, result_line):
        # butt field=3, socket field=1, tap field=1 → 5
        field = next((e for e in result_line["by_shop_field"] if e["shop_field"] == "field"), None)
        assert field is not None
        assert field["count"] == 5

    def test_by_type_ranked_desc(self, result_line):
        counts = [e["count"] for e in result_line["by_type"]]
        assert counts == sorted(counts, reverse=True)

    def test_by_shop_field_ranked_desc(self, result_line):
        counts = [e["count"] for e in result_line["by_shop_field"]]
        assert counts == sorted(counts, reverse=True)


# ===========================================================================
# Parte 5 — group_by: agrupaciones correctas
# ===========================================================================


class TestGroupByLine:
    def test_group_count(self, result_line):
        # L-001, L-002, L-003, (SIN LÍNEA)
        assert result_line["group_count"] == 4

    def test_l001_count(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == "L-001"), None)
        assert g is not None
        assert g["weld_count"] == 3

    def test_l002_count(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == "L-002"), None)
        assert g is not None
        assert g["weld_count"] == 3

    def test_l003_count(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == "L-003"), None)
        assert g is not None
        assert g["weld_count"] == 3

    def test_no_line_group(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == _NO_LINE_LABEL), None)
        assert g is not None
        assert g["weld_count"] == 3

    def test_groups_ordered_desc(self, result_line):
        counts = [g["weld_count"] for g in result_line["groups"]]
        assert counts == sorted(counts, reverse=True)

    def test_total_equals_sum_of_groups(self, result_line):
        total = sum(g["weld_count"] for g in result_line["groups"])
        assert total == result_line["total_welds"]


class TestGroupBySpec:
    def test_group_count(self, result_spec):
        assert result_spec["group_count"] == 2

    def test_cs150_count(self, result_spec):
        g = next((g for g in result_spec["groups"] if g["group"] == "CS150"), None)
        assert g is not None
        # Recuento: butt PnPIDs 1,2,5,6,7 (5) + socket 9 (1) + tap 11 (1) = 7
        assert g["weld_count"] == 7

    def test_ss150_count(self, result_spec):
        g = next((g for g in result_spec["groups"] if g["group"] == "SS150"), None)
        assert g is not None
        # Recuento: butt 3,4,8 (3) + socket 10 (1) + tap 12 (1) = 5
        assert g["weld_count"] == 5

    def test_no_sin_linea_group(self, result_spec):
        groups = [g["group"] for g in result_spec["groups"]]
        assert _NO_LINE_LABEL not in groups

    def test_total_consistent(self, result_spec):
        assert result_spec["total_welds"] == 12


class TestGroupBySize:
    def test_group_count(self, result_size):
        # 2 in y 4 in
        assert result_size["group_count"] == 2

    def test_2in_count(self, result_size):
        # _fmt_size(2.0, "in") → '2"' ; butt 1,2,5,6 (4) + socket 9 (1) + tap 11 (1) = 6
        g = next((g for g in result_size["groups"] if g["group"] == '2"'), None)
        assert g is not None, f"Grupos disponibles: {[g['group'] for g in result_size['groups']]}"
        assert g["weld_count"] == 6

    def test_4in_count(self, result_size):
        # _fmt_size(4.0, "in") → '4"' ; butt 3,4,7,8 (4) + socket 10 (1) + tap 12 (1) = 6
        g = next((g for g in result_size["groups"] if g["group"] == '4"'), None)
        assert g is not None, f"Grupos disponibles: {[g['group'] for g in result_size['groups']]}"
        assert g["weld_count"] == 6

    def test_total_consistent(self, result_size):
        assert result_size["total_welds"] == 12


class TestGroupByShopField:
    def test_group_count(self, result_shop_field):
        assert result_shop_field["group_count"] == 2

    def test_shop_count(self, result_shop_field):
        # butt shop=5, socket shop=1, tap shop=1 → 7
        g = next((g for g in result_shop_field["groups"] if g["group"] == "shop"), None)
        assert g is not None
        assert g["weld_count"] == 7

    def test_field_count(self, result_shop_field):
        # butt field=3, socket field=1, tap field=1 → 5
        g = next((g for g in result_shop_field["groups"] if g["group"] == "field"), None)
        assert g is not None
        assert g["weld_count"] == 5


class TestGroupByType:
    def test_group_count(self, result_type):
        assert result_type["group_count"] == 3

    def test_butt_count(self, result_type):
        g = next((g for g in result_type["groups"] if g["group"] == "butt"), None)
        assert g is not None
        assert g["weld_count"] == 8

    def test_socket_count(self, result_type):
        g = next((g for g in result_type["groups"] if g["group"] == "socket"), None)
        assert g is not None
        assert g["weld_count"] == 2

    def test_tap_count(self, result_type):
        g = next((g for g in result_type["groups"] if g["group"] == "tap"), None)
        assert g is not None
        assert g["weld_count"] == 2

    def test_by_type_breakdown_matches_groups(self, result_type):
        """En group_by=type los grupos y by_type deben ser consistentes."""
        groups_by_type = {g["group"]: g["weld_count"] for g in result_type["groups"]}
        by_type_dict = {e["type"]: e["count"] for e in result_type["by_type"]}
        for t in ("butt", "socket", "tap"):
            assert groups_by_type.get(t) == by_type_dict.get(t)


# ===========================================================================
# Parte 6 — Filtros
# ===========================================================================


class TestFilterLine:
    def test_filter_line_l001(self, proj):
        r = weld_list(str(proj), {"limit": 0, "line": "L-001"})
        assert r["ok"] is True
        assert r["total_welds"] == 3
        assert r["filters"]["line"] == "L-001"

    def test_filter_line_case_insensitive(self, proj):
        r = weld_list(str(proj), {"limit": 0, "line": "l-001"})
        assert r["total_welds"] == 3

    def test_filter_line_l002(self, proj):
        r = weld_list(str(proj), {"limit": 0, "line": "L-002"})
        assert r["total_welds"] == 3

    def test_filter_line_nonexistent(self, proj):
        r = weld_list(str(proj), {"limit": 0, "line": "L-999"})
        assert r["total_welds"] == 0
        assert r["groups"] == []

    def test_filter_line_by_type_scoped(self, proj):
        """by_type se acota al alcance del filtro de línea."""
        r = weld_list(str(proj), {"limit": 0, "line": "L-001"})
        # L-001 tiene butt:2 + socket:1
        by_type = {e["type"]: e["count"] for e in r["by_type"]}
        assert by_type.get("butt") == 2
        assert by_type.get("socket") == 1
        assert "tap" not in by_type

    def test_filter_line_by_shop_field_scoped(self, proj):
        """by_shop_field se acota al alcance del filtro de línea."""
        r = weld_list(str(proj), {"limit": 0, "line": "L-001"})
        # L-001: PnP 1(shop), 2(field), 9(shop)
        by_sf = {e["shop_field"]: e["count"] for e in r["by_shop_field"]}
        assert by_sf.get("shop") == 2
        assert by_sf.get("field") == 1


class TestFilterSpec:
    def test_filter_spec_cs150(self, proj):
        r = weld_list(str(proj), {"limit": 0, "spec": "CS150"})
        assert r["ok"] is True
        assert r["total_welds"] == 7
        assert r["filters"]["spec"] == "CS150"

    def test_filter_spec_ss150(self, proj):
        r = weld_list(str(proj), {"limit": 0, "spec": "SS150"})
        assert r["total_welds"] == 5

    def test_filter_spec_case_insensitive(self, proj):
        r = weld_list(str(proj), {"limit": 0, "spec": "cs150"})
        assert r["total_welds"] == 7

    def test_filter_spec_nonexistent(self, proj):
        r = weld_list(str(proj), {"limit": 0, "spec": "X999"})
        assert r["total_welds"] == 0


class TestFilterSize:
    def test_filter_size_2in(self, proj):
        r = weld_list(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        assert r["ok"] is True
        assert r["total_welds"] == 6
        assert r["filters"]["size"] == {"value": 2.0, "unit": "IN"}

    def test_filter_size_4in(self, proj):
        r = weld_list(str(proj), {"limit": 0, "size": {"value": 4.0, "unit": "in"}})
        assert r["total_welds"] == 6

    def test_filter_size_without_unit_ignored(self, proj):
        """size sin unidad se ignora y se añade nota."""
        r = weld_list(str(proj), {"limit": 0, "size": 2.0})
        assert r["ok"] is True
        assert r["total_welds"] == 12  # filtro ignorado: todos los registros
        assert "size" not in r["filters"]
        assert any("size" in n.lower() or "unidad" in n.lower() for n in r["notes"])

    def test_filter_size_dict_without_unit_ignored(self, proj):
        """size como dict sin unidad se ignora."""
        r = weld_list(str(proj), {"limit": 0, "size": {"value": 2.0}})
        assert r["total_welds"] == 12
        assert "size" not in r["filters"]


class TestFilterShopField:
    def test_filter_shop(self, proj):
        # butt shop=5, socket shop=1, tap shop=1 → 7
        r = weld_list(str(proj), {"limit": 0, "shop_field": "shop"})
        assert r["ok"] is True
        assert r["total_welds"] == 7
        assert r["filters"]["shop_field"] == "shop"

    def test_filter_field(self, proj):
        # butt field=3, socket field=1, tap field=1 → 5
        r = weld_list(str(proj), {"limit": 0, "shop_field": "field"})
        assert r["total_welds"] == 5

    def test_filter_shop_uppercase(self, proj):
        r = weld_list(str(proj), {"limit": 0, "shop_field": "SHOP"})
        assert r["total_welds"] == 7

    def test_filter_shop_field_invalid_ignored(self, proj):
        r = weld_list(str(proj), {"limit": 0, "shop_field": "unknown_value"})
        assert r["total_welds"] == 12  # filtro ignorado
        assert "shop_field" not in r["filters"]
        assert any("shop_field" in n for n in r["notes"])


class TestFilterWeldType:
    def test_filter_butt(self, proj):
        r = weld_list(str(proj), {"limit": 0, "weld_type": "butt"})
        assert r["ok"] is True
        assert r["total_welds"] == 8
        assert r["filters"]["weld_type"] == "butt"

    def test_filter_socket(self, proj):
        r = weld_list(str(proj), {"limit": 0, "weld_type": "socket"})
        assert r["total_welds"] == 2

    def test_filter_tap(self, proj):
        r = weld_list(str(proj), {"limit": 0, "weld_type": "tap"})
        assert r["total_welds"] == 2

    def test_filter_weld_type_invalid_ignored(self, proj):
        r = weld_list(str(proj), {"limit": 0, "weld_type": "orbital"})
        assert r["total_welds"] == 12  # filtro ignorado
        assert "weld_type" not in r["filters"]
        assert any("weld_type" in n for n in r["notes"])

    def test_filter_butt_by_type_only_butt(self, proj):
        """Con weld_type=butt, by_type solo tiene 'butt'."""
        r = weld_list(str(proj), {"limit": 0, "weld_type": "butt"})
        types = {e["type"] for e in r["by_type"]}
        assert types == {"butt"}


class TestFilterCombined:
    def test_line_and_spec(self, proj):
        # L-001 tiene PnPIDs 1,2 (CS150) + 9 (CS150) = 3 soldaduras CS150
        r = weld_list(str(proj), {"limit": 0, "line": "L-001", "spec": "CS150"})
        assert r["total_welds"] == 3

    def test_line_and_weld_type(self, proj):
        # L-001 tiene butt:2, socket:1
        r = weld_list(str(proj), {"limit": 0, "line": "L-001", "weld_type": "butt"})
        assert r["total_welds"] == 2

    def test_spec_and_shop_field(self, proj):
        # CS150 + shop: PnPIDs 1(shop),5(shop),6(shop),7(shop),9(shop),11(shop) = 6
        r = weld_list(str(proj), {"limit": 0, "spec": "CS150", "shop_field": "shop"})
        assert r["total_welds"] == 6

    def test_weld_type_and_size(self, proj):
        # butt + 2 in: PnPIDs 1,2,5,6 = 4
        r = weld_list(
            str(proj),
            {"limit": 0, "weld_type": "butt", "size": {"value": 2.0, "unit": "in"}},
        )
        assert r["total_welds"] == 4


# ===========================================================================
# Parte 7 — limit / omitted
# ===========================================================================


class TestLimitOmitted:
    def test_limit_1_returns_1_group(self, proj):
        r = weld_list(str(proj), {"limit": 1, "group_by": "line"})
        assert len(r["groups"]) == 1

    def test_limit_1_omitted_is_3(self, proj):
        r = weld_list(str(proj), {"limit": 1, "group_by": "line"})
        assert r["omitted"] == 3

    def test_limit_0_no_cap(self, result_line):
        assert result_line["omitted"] == 0
        assert len(result_line["groups"]) == result_line["group_count"]

    def test_total_welds_not_affected_by_limit(self, proj):
        r = weld_list(str(proj), {"limit": 1, "group_by": "line"})
        assert r["total_welds"] == 12

    def test_by_type_not_affected_by_limit(self, proj):
        r = weld_list(str(proj), {"limit": 1, "group_by": "line"})
        by_type = {e["type"]: e["count"] for e in r["by_type"]}
        assert by_type.get("butt") == 8

    def test_untagged_not_affected_by_limit(self, proj):
        r = weld_list(str(proj), {"limit": 1, "group_by": "line"})
        assert r["untagged"]["weld_count"] == 3

    def test_group_count_reflects_total_groups(self, proj):
        """group_count es el total antes del cap, no el número de grupos devueltos."""
        r = weld_list(str(proj), {"limit": 2, "group_by": "line"})
        assert r["group_count"] == 4
        assert len(r["groups"]) == 2
        assert r["omitted"] == 2

    def test_default_limit_50(self, proj):
        # Con 4 grupos no se aplica el cap
        r = weld_list(str(proj))
        assert r["limit"] == 50
        assert r["omitted"] == 0


# ===========================================================================
# Parte 8 — group_by inválido cae a "line"
# ===========================================================================


class TestGroupByInvalid:
    def test_invalid_group_by_fallback(self, proj):
        r = weld_list(str(proj), {"limit": 0, "group_by": "invalid_key"})
        assert r["ok"] is True
        assert r["group_by"] == "line"

    def test_invalid_group_by_note(self, proj):
        r = weld_list(str(proj), {"limit": 0, "group_by": "invalid_key"})
        assert any("group_by" in n for n in r["notes"])

    def test_invalid_group_by_data_correct(self, proj):
        r = weld_list(str(proj), {"limit": 0, "group_by": "invalid_key"})
        # Debe comportarse como group_by=line
        assert r["total_welds"] == 12
        groups_keys = {g["group"] for g in r["groups"]}
        assert "L-001" in groups_keys


# ===========================================================================
# Parte 9 — Degradación de esquema
# ===========================================================================


class TestDegradationNoWeldTables:
    """Sin ninguna tabla de soldadura → ok:True, total_welds=0."""

    @pytest.fixture
    def proj_no_welds(self, tmp_path: Path) -> Path:
        proj = tmp_path / "NO_WELDS"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        # Piping.dcf solo con EngineeringItems, sin tablas de soldadura
        con = sqlite3.connect(str(proj / "Piping.dcf"))
        con.execute("CREATE TABLE EngineeringItems (PnPID INTEGER, Spec TEXT)")
        con.commit()
        con.close()
        return proj

    def test_ok_true(self, proj_no_welds):
        r = weld_list(str(proj_no_welds), {"limit": 0})
        assert r["ok"] is True

    def test_total_welds_zero(self, proj_no_welds):
        r = weld_list(str(proj_no_welds), {"limit": 0})
        assert r["total_welds"] == 0

    def test_groups_empty(self, proj_no_welds):
        r = weld_list(str(proj_no_welds), {"limit": 0})
        assert r["groups"] == []

    def test_by_type_empty(self, proj_no_welds):
        r = weld_list(str(proj_no_welds), {"limit": 0})
        assert r["by_type"] == []

    def test_by_shop_field_empty(self, proj_no_welds):
        r = weld_list(str(proj_no_welds), {"limit": 0})
        assert r["by_shop_field"] == []

    def test_note_present(self, proj_no_welds):
        r = weld_list(str(proj_no_welds), {"limit": 0})
        note_text = " ".join(r["notes"]).lower()
        assert "soldadura" in note_text or "buttweld" in note_text or "socketweld" in note_text


class TestDegradationOnlySomeTables:
    """Solo Buttweld presente → usa Buttweld, nota de Socketweld y TapWeld ausentes."""

    @pytest.fixture
    def proj_only_butt(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "ONLY_BUTT",
            butt_rows=[(1, "SHOP"), (2, "FIELD")],
            socket_rows=None,  # No se insertan filas
            tap_rows=None,
            ei_rows=[(1, "CS150", 2.0, "in"), (2, "CS150", 2.0, "in")],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100)],
            create_socketweld=False,  # No se crea la tabla
            create_tapweld=False,
        )

    def test_ok_true(self, proj_only_butt):
        r = weld_list(str(proj_only_butt), {"limit": 0})
        assert r["ok"] is True

    def test_total_welds(self, proj_only_butt):
        r = weld_list(str(proj_only_butt), {"limit": 0})
        assert r["total_welds"] == 2

    def test_by_type_only_butt(self, proj_only_butt):
        r = weld_list(str(proj_only_butt), {"limit": 0})
        types = {e["type"] for e in r["by_type"]}
        assert types == {"butt"}

    def test_note_about_absent_tables(self, proj_only_butt):
        r = weld_list(str(proj_only_butt), {"limit": 0})
        note_text = " ".join(r["notes"])
        # Debe mencionar que Socketweld y TapWeld están ausentes
        assert "Socketweld" in note_text or "socket" in note_text.lower()


class TestDegradationNoShopFieldColumn:
    """Shop_Field ausente en Buttweld → soldaduras de Buttweld = "(desconocido)"."""

    @pytest.fixture
    def proj_no_sf(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_SF",
            butt_rows=[(1, None), (2, None)],  # Shop_Field no se inserta
            socket_rows=[(3, "SHOP")],
            tap_rows=None,
            ei_rows=[
                (1, "CS150", 2.0, "in"),
                (2, "CS150", 2.0, "in"),
                (3, "CS150", 2.0, "in"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100), (3, 100)],
            include_shop_field_butt=False,  # Buttweld sin columna Shop_Field
            create_tapweld=False,
        )

    def test_ok_true(self, proj_no_sf):
        r = weld_list(str(proj_no_sf), {"limit": 0})
        assert r["ok"] is True

    def test_butt_welds_unknown_shop_field(self, proj_no_sf):
        r = weld_list(str(proj_no_sf), {"limit": 0})
        by_sf = {e["shop_field"]: e["count"] for e in r["by_shop_field"]}
        # Buttweld (2 soldaduras) → (desconocido), Socketweld (1) → shop
        assert by_sf.get(_UNKNOWN_SHOP_FIELD, 0) == 2
        assert by_sf.get("shop", 0) == 1

    def test_note_about_missing_shop_field(self, proj_no_sf):
        r = weld_list(str(proj_no_sf), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "Shop_Field" in note_text


class TestDegradationNoLineRelationship:
    """Sin tablas de relación de línea → todas las soldaduras en untagged."""

    @pytest.fixture
    def proj_no_rel(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_REL",
            butt_rows=[(1, "SHOP"), (2, "FIELD"), (3, "SHOP")],
            socket_rows=None,
            tap_rows=None,
            ei_rows=[
                (1, "CS150", 2.0, "in"),
                (2, "CS150", 2.0, "in"),
                (3, "SS150", 4.0, "in"),
            ],
            line_group_rows=None,
            rel_rows=None,
            create_line_group=False,
            create_rel=False,
        )

    def test_all_untagged(self, proj_no_rel):
        r = weld_list(str(proj_no_rel), {"limit": 0})
        assert r["untagged"]["weld_count"] == 3

    def test_all_in_no_line_group(self, proj_no_rel):
        r = weld_list(str(proj_no_rel), {"limit": 0, "group_by": "line"})
        groups = {g["group"]: g["weld_count"] for g in r["groups"]}
        assert groups.get(_NO_LINE_LABEL) == 3
        assert len(groups) == 1

    def test_note_about_missing_tables(self, proj_no_rel):
        r = weld_list(str(proj_no_rel), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "P3dLineGroup" in note_text or "línea" in note_text.lower()

    def test_total_welds_not_affected(self, proj_no_rel):
        r = weld_list(str(proj_no_rel), {"limit": 0})
        assert r["total_welds"] == 3

    def test_size_groupby_still_works_without_rel(self, proj_no_rel):
        r = weld_list(str(proj_no_rel), {"limit": 0, "group_by": "size"})
        assert r["ok"] is True
        assert r["total_welds"] == 3


class TestDegradationMissingLineGroupColumns:
    """P3dLineGroup sin columna Tag → todas las soldaduras a untagged."""

    @pytest.fixture
    def proj_no_tag_col(self, tmp_path: Path) -> Path:
        proj = tmp_path / "NO_TAG_COL"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        con = sqlite3.connect(str(proj / "Piping.dcf"))
        # Buttweld y EngineeringItems normales
        con.execute("CREATE TABLE Buttweld (PnPID INTEGER, Shop_Field TEXT)")
        con.execute("INSERT INTO Buttweld VALUES (1, 'SHOP')")
        con.execute("CREATE TABLE EngineeringItems (PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)")
        con.execute("INSERT INTO EngineeringItems VALUES (1, 'CS150', 2.0, 'in')")
        # P3dLineGroup sin columna Tag (solo PnPID)
        con.execute("CREATE TABLE P3dLineGroup (PnPID INTEGER)")
        con.execute("INSERT INTO P3dLineGroup VALUES (100)")
        # P3dLineGroupPartRelationship normal
        con.execute("CREATE TABLE P3dLineGroupPartRelationship (Part INTEGER, LineGroup INTEGER)")
        con.execute("INSERT INTO P3dLineGroupPartRelationship VALUES (1, 100)")
        con.commit()
        con.close()
        return proj

    def test_all_untagged_when_tag_col_missing(self, proj_no_tag_col):
        r = weld_list(str(proj_no_tag_col), {"limit": 0})
        assert r["ok"] is True
        assert r["untagged"]["weld_count"] == 1

    def test_note_about_missing_columns(self, proj_no_tag_col):
        r = weld_list(str(proj_no_tag_col), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "Tag" in note_text or "columna" in note_text.lower() or "línea" in note_text.lower()


# ===========================================================================
# Parte 10 — Untagged: soldaduras sin Tag válido
# ===========================================================================


class TestUntagged:
    """Soldaduras con Tag NULL/''/'?' deben aparecer siempre en untagged."""

    @pytest.fixture
    def proj_mixed_tags(self, tmp_path: Path) -> Path:
        """Proyecto con tags NULL, '', '?' y uno válido."""
        return _make_project(
            tmp_path, "MIXED_TAGS",
            butt_rows=[
                (1, "SHOP"),   # untagged: NULL (sin relación)
                (2, "FIELD"),  # untagged: NULL (sin relación)
                (3, "SHOP"),   # tagged: L-001
            ],
            socket_rows=None,
            tap_rows=None,
            ei_rows=[
                (1, "CS150", 2.0, "in"),
                (2, "CS150", 2.0, "in"),
                (3, "CS150", 2.0, "in"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(3, 100)],  # Solo PnPID 3 tiene relación
        )

    def test_untagged_count(self, proj_mixed_tags):
        r = weld_list(str(proj_mixed_tags), {"limit": 0})
        assert r["untagged"]["weld_count"] == 2

    def test_untagged_group_in_group_by_line(self, proj_mixed_tags):
        r = weld_list(str(proj_mixed_tags), {"limit": 0, "group_by": "line"})
        groups = {g["group"]: g["weld_count"] for g in r["groups"]}
        assert groups.get(_NO_LINE_LABEL) == 2
        assert groups.get("L-001") == 1

    def test_untagged_in_spec_group_by(self, proj_mixed_tags):
        """En group_by=spec, los untagged caen en su grupo de spec natural."""
        r = weld_list(str(proj_mixed_tags), {"limit": 0, "group_by": "spec"})
        groups = {g["group"]: g["weld_count"] for g in r["groups"]}
        # Todas son CS150 (incluyendo las untagged)
        assert groups.get("CS150") == 3
        assert _NO_LINE_LABEL not in groups

    def test_total_welds_includes_untagged(self, proj_mixed_tags):
        r = weld_list(str(proj_mixed_tags), {"limit": 0})
        assert r["total_welds"] == 3


# ===========================================================================
# Parte 11 — data no se muta
# ===========================================================================


class TestDataNotMutated:
    def test_data_not_mutated(self, proj):
        original = {"limit": 0, "group_by": "line", "spec": "CS150"}
        data_copy = dict(original)
        weld_list(str(proj), data_copy)
        assert data_copy == original

    def test_none_data_ok(self, proj):
        r = weld_list(str(proj), None)
        assert r["ok"] is True


# ===========================================================================
# Parte 12 — Solo lectura: bytes del .dcf sin cambios
# ===========================================================================


class TestReadOnly:
    def test_dcf_bytes_unchanged(self, proj):
        dcf = proj / "Piping.dcf"
        before = dcf.read_bytes()
        weld_list(str(proj), {"limit": 0})
        after = dcf.read_bytes()
        assert before == after


# ===========================================================================
# Parte 13 — Test de integración (skip si la ruta no es accesible)
# ===========================================================================

_REAL_PROJECT = (
    r"\\172.16.0.220\Comun\06-INFORMÁTICA\3_UTILIDADES\MCP-Plant3D\Proyectos"
    r"\23099 - AIR LIQUIDE HUELVA"
)
_REAL_DCF = Path(_REAL_PROJECT) / "Piping.dcf"

pytestmark_integration = pytest.mark.skipif(
    not _REAL_DCF.exists(),
    reason="Proyecto real AIR LIQUIDE HUELVA no accesible",
)


@pytest.mark.skipif(
    not _REAL_DCF.exists(),
    reason="Proyecto real AIR LIQUIDE HUELVA no accesible",
)
class TestIntegrationAirLiquideHuelva:
    """Integración contra el proyecto real; solo se ejecuta si la ruta es accesible."""

    @pytest.fixture(scope="class")
    def result_real(self) -> dict:
        return weld_list(_REAL_PROJECT, {"limit": 0})

    def test_ok_true(self, result_real):
        assert result_real["ok"] is True

    def test_total_welds_approx(self, result_real):
        """Total ≈ 2953 soldaduras (margen ±50 por variaciones de datos)."""
        total = result_real["total_welds"]
        assert 2900 <= total <= 3010, f"Total inesperado: {total}"

    def test_buttweld_approx(self, result_real):
        butt = next((e for e in result_real["by_type"] if e["type"] == "butt"), None)
        assert butt is not None
        assert 2330 <= butt["count"] <= 2440, f"Buttweld inesperado: {butt['count']}"

    def test_socketweld_approx(self, result_real):
        sock = next((e for e in result_real["by_type"] if e["type"] == "socket"), None)
        assert sock is not None
        assert 340 <= sock["count"] <= 395, f"Socketweld inesperado: {sock['count']}"

    def test_tapweld_approx(self, result_real):
        tap = next((e for e in result_real["by_type"] if e["type"] == "tap"), None)
        assert tap is not None
        assert 190 <= tap["count"] <= 225, f"TapWeld inesperado: {tap['count']}"

    def test_untagged_below_20pct(self, result_real):
        """La mayoría de soldaduras deben tener línea resuelta (<20% untagged)."""
        total = result_real["total_welds"]
        untagged = result_real["untagged"]["weld_count"]
        if total > 0:
            pct = untagged / total * 100
            assert pct < 20, f"Porcentaje untagged inesperadamente alto: {pct:.1f}%"

    def test_dcf_unchanged(self):
        before = _REAL_DCF.read_bytes()
        weld_list(_REAL_PROJECT, {"limit": 0})
        after = _REAL_DCF.read_bytes()
        assert before == after
