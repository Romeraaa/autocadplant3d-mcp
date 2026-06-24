"""Tests for plant3d_query.bolt_gasket_list — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) that include the BoltSet/Gasket tables and the
P3dLineGroup / P3dLineGroupPartRelationship tables required by
bolt_gasket_list.  No real project databases are ever touched.

Key invariants verified:
1.  Agregación pura _build_bolt_gasket_aggregates (7 group_by: line/size/spec/
    material/item_type/shop_field/bolt_size): recuentos en todas las métricas;
    by_item_type (con individual_bolts) y by_shop_field correctos; totals; untagged.
2.  _bg_empty_metrics y _bg_accumulate: shapes correctas, individual_bolts=Σ NumberInSet
    (int redondeado), juntas suman 0 a individual_bolts y 1 a gaskets/item_count.
3.  NumberInSet no numérico (p.ej. 'N/A', '', None): no lanza; contribuye 0 a
    individual_bolts; el set SÍ cuenta en bolt_sets/item_count; nota presente.
4.  Material None y '' → None en group_by="material" caen en "(sin)".
5.  Untagged: Tag NULL/''/'?' o sin relación → untagged; grupo "(SIN LÍNEA)" en
    group_by=line; no se pierden en spec/size; métricas untagged correctas.
6.  Filtros: item_type (bolt→solo BoltSet; gasket→solo Gasket), line, spec,
    size con unidad (filtra) y sin unidad (ignora + nota), shop_field, combinados.
    Verifica que totals/by_* quedan acotados al alcance filtrado.
7.  bolt_size: group_by="bolt_size" agrupa pernos por BoltSize; juntas → "(sin)".
8.  limit/omitted: acota grupos, 0 = sin tope, omitted correcto;
    totales/desgloses NO afectados por el cap.
9.  group_by inválido → cae a "line" con nota.
10. Degradación de esquema: ninguna tabla → ok:True, totales 0, listas vacías,
    nota; solo BoltSet o solo Gasket → usa esa + nota; columnas opcionales ausentes
    (NumberInSet, BoltSize, Shop_Field) → degradan a None/0 + nota; sin tablas de
    relación de línea → todo a untagged/"(SIN LÍNEA)" + nota, sin excepción.
11. data no se muta.
12. Solo lectura: bytes del .dcf sin cambios.
13. Test de integración con proyecto real AIR LIQUIDE HUELVA (skip si no accesible).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import (
    _NO_LINE_LABEL,
    _UNKNOWN_SHOP_FIELD,
    _bg_accumulate,
    _bg_empty_metrics,
    _build_bolt_gasket_aggregates,
    bolt_gasket_list,
)


# ===========================================================================
# Helpers: fila-dict sintética para tests unitarios puros (sin SQLite)
# ===========================================================================


class _Row(dict):
    """Dict cuyas claves también son accesibles como r["col"]."""


def _bolt(**kw) -> _Row:
    """Fila de perno con defaults razonables."""
    defaults = {
        "item_type": "bolt",
        "shop_field": "shop",
        "num_in_set": 4.0,
        "bolt_size": '5/8"',
        "spec": "CS150",
        "material": "A193-B7",
        "dia": 2.0,
        "dia_unit": "in",
        "line": "L-001",
    }
    defaults.update(kw)
    return _Row(**defaults)


def _gasket(**kw) -> _Row:
    """Fila de junta con defaults razonables."""
    defaults = {
        "item_type": "gasket",
        "shop_field": "shop",
        "num_in_set": 0.0,
        "bolt_size": None,
        "spec": "CS150",
        "material": "GRAPHITE",
        "dia": 2.0,
        "dia_unit": "in",
        "line": "L-001",
    }
    defaults.update(kw)
    return _Row(**defaults)


# ===========================================================================
# Helpers: construcción de proyectos SQLite mínimos en tmp_path
# ===========================================================================


def _make_piping_dcf(
    path: Path,
    *,
    bolt_rows: list[tuple] | None = None,
    gasket_rows: list[tuple] | None = None,
    ei_rows: list[tuple] | None = None,
    line_group_rows: list[tuple] | None = None,
    rel_rows: list[tuple] | None = None,
    create_boltset: bool = True,
    create_gasket: bool = True,
    create_line_group: bool = True,
    create_rel: bool = True,
    include_num_in_set: bool = True,
    include_bolt_size: bool = True,
    include_shop_field_bolt: bool = True,
    include_shop_field_gasket: bool = True,
) -> None:
    """Crea un Piping.dcf mínimo con las tablas BoltSet/Gasket y de relación.

    bolt_rows:        (PnPID, Shop_Field, NumberInSet, BoltSize)
    gasket_rows:      (PnPID, Shop_Field)
    ei_rows:          (PnPID, Spec, NominalDiameter, NominalUnit, Material)
    line_group_rows:  (PnPID, Tag)
    rel_rows:         (Part, LineGroup)   — Part = item PnPID, LineGroup = P3dLineGroup.PnPID
    """
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        # EngineeringItems
        con.execute(
            "CREATE TABLE EngineeringItems "
            "(PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT, Material TEXT)"
        )
        for row in (ei_rows or []):
            pnpid, spec, dia, unit, mat = row
            con.execute(
                "INSERT INTO EngineeringItems "
                "(PnPID, Spec, NominalDiameter, NominalUnit, Material) "
                "VALUES (?, ?, ?, ?, ?)",
                (pnpid, spec, dia, unit, mat),
            )

        # BoltSet
        if create_boltset:
            cols = "PnPID INTEGER"
            if include_shop_field_bolt:
                cols += ", Shop_Field TEXT"
            if include_num_in_set:
                cols += ", NumberInSet TEXT"
            if include_bolt_size:
                cols += ", BoltSize TEXT"
            con.execute(f"CREATE TABLE BoltSet ({cols})")
            for row in (bolt_rows or []):
                pnpid, sf, num, bsize = row
                ins_cols = ["PnPID"]
                ins_vals: list = [pnpid]
                if include_shop_field_bolt:
                    ins_cols.append("Shop_Field")
                    ins_vals.append(sf)
                if include_num_in_set:
                    ins_cols.append("NumberInSet")
                    ins_vals.append(num)
                if include_bolt_size:
                    ins_cols.append("BoltSize")
                    ins_vals.append(bsize)
                placeholders = ", ".join("?" * len(ins_cols))
                col_str = ", ".join(ins_cols)
                con.execute(
                    f"INSERT INTO BoltSet ({col_str}) VALUES ({placeholders})",
                    ins_vals,
                )

        # Gasket
        if create_gasket:
            cols = "PnPID INTEGER"
            if include_shop_field_gasket:
                cols += ", Shop_Field TEXT"
            con.execute(f"CREATE TABLE Gasket ({cols})")
            for row in (gasket_rows or []):
                pnpid, sf = row
                if include_shop_field_gasket:
                    con.execute(
                        "INSERT INTO Gasket (PnPID, Shop_Field) VALUES (?, ?)",
                        (pnpid, sf),
                    )
                else:
                    con.execute("INSERT INTO Gasket (PnPID) VALUES (?)", (pnpid,))

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


def _make_project(base: Path, name: str, **dcf_kw) -> Path:
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
#   BoltSet:  1 (SHOP, L-001, CS150, 2", M16, 4 pernos, A193-B7)
#             2 (FIELD, L-001, CS150, 2", M16, 8 pernos, A193-B7)
#             3 (SHOP, L-002, SS150, 4", 5/8", 12 pernos, A193-B8)
#             4 (FIELD, L-002, SS150, 4", 5/8", 16 pernos, A193-B8)
#             5 (SHOP, L-003, CS150, 2", M16, 4 pernos, A193-B7)
#             6 (SHOP, untagged, CS150, 2", M16, 4 pernos, A193-B7)
#             7 (SHOP, untagged, CS150, 4", 5/8", 8 pernos, None)
#             8 (FIELD, untagged, SS150, 4", 5/8", 16 pernos, A193-B8)
#   Gasket:   9 (SHOP, L-001, CS150, 2", GRAPHITE)
#            10 (FIELD, L-002, SS150, 4", SPIRAL)
#            11 (SHOP, L-003, CS150, 2", GRAPHITE)
#            12 (FIELD, untagged, SS150, 4", SPIRAL)
#
# P3dLineGroup:  PnPID 100→"L-001", 200→"L-002", 300→"L-003"
# P3dLineGroupPartRelationship:
#   items 1,2,9  → LineGroup 100 (L-001)
#   items 3,4,10 → LineGroup 200 (L-002)
#   items 5,11   → LineGroup 300 (L-003)
#   items 6,7,8,12 NO tienen relación → untagged
#
# Totales:
#   Total items = 12 (8 bolt sets + 4 gaskets)
#   individual_bolts = 4+8+12+16+4+4+8+16 = 72
#   by_item_type: bolt={item_count:8, bolt_sets:8, individual_bolts:72, gaskets:0}
#                 gasket={item_count:4, bolt_sets:0, individual_bolts:0, gaskets:4}
#   by_shop_field: shop=7, field=5
#   untagged: 4 items (PnPIDs 6,7,8 bolt + 12 gasket)
#
#   group_by="line":
#     L-001 → 3, L-002 → 3, L-003 → 2, (SIN LÍNEA) → 4
#   group_by="spec":
#     CS150: 1,2,5,6,7(bolt) + 9,11(gasket) = 7
#     SS150: 3,4,8(bolt) + 10,12(gasket) = 5
#   group_by="size":
#     2": 1,2,5,6(bolt) + 9,11(gasket) = 6
#     4": 3,4,7,8(bolt) + 10,12(gasket) = 6
#   group_by="shop_field":
#     shop: 1,3,5,6(bolt) + 9,11(gasket) = 7
#     field: 2,4,8(bolt) + 10,12(gasket) = 5
#   group_by="item_type":
#     bolt → 8, gasket → 4
#   group_by="material":
#     A193-B7: 1,2,5,6(bolt) + (gaskets son GRAPHITE/SPIRAL) = 4 bolts
#     A193-B8: 3,4,8(bolt) = 3
#     None (empty material for PnPID 7): 1
#     GRAPHITE: 9,11(gasket) = 2
#     SPIRAL: 10,12(gasket) = 2
#   group_by="bolt_size":
#     M16: 1,2,5,6(bolt) = 4; bolt_size None/gaskets → "(sin)": 4(gaskets)
#     5/8": 3,4,7,8(bolt) = 4
# ---------------------------------------------------------------------------

_BOLT_ROWS = [
    # (PnPID, Shop_Field, NumberInSet, BoltSize)
    (1,  "SHOP",  "4",  "M16"),    # L-001, CS150, 2in, A193-B7
    (2,  "FIELD", "8",  "M16"),    # L-001, CS150, 2in, A193-B7
    (3,  "SHOP",  "12", '5/8"'),   # L-002, SS150, 4in, A193-B8
    (4,  "FIELD", "16", '5/8"'),   # L-002, SS150, 4in, A193-B8
    (5,  "SHOP",  "4",  "M16"),    # L-003, CS150, 2in, A193-B7
    (6,  "SHOP",  "4",  "M16"),    # untagged, CS150, 2in, A193-B7
    (7,  "SHOP",  "8",  '5/8"'),   # untagged, CS150, 4in, None material
    (8,  "FIELD", "16", '5/8"'),   # untagged, SS150, 4in, A193-B8
]
_GASKET_ROWS = [
    # (PnPID, Shop_Field)
    (9,  "SHOP"),   # L-001, CS150, 2in, GRAPHITE
    (10, "FIELD"),  # L-002, SS150, 4in, SPIRAL
    (11, "SHOP"),   # L-003, CS150, 2in, GRAPHITE
    (12, "FIELD"),  # untagged, SS150, 4in, SPIRAL
]
_EI_ROWS = [
    # (PnPID, Spec, NominalDiameter, NominalUnit, Material)
    (1,  "CS150", 2.0, "in", "A193-B7"),
    (2,  "CS150", 2.0, "in", "A193-B7"),
    (3,  "SS150", 4.0, "in", "A193-B8"),
    (4,  "SS150", 4.0, "in", "A193-B8"),
    (5,  "CS150", 2.0, "in", "A193-B7"),
    (6,  "CS150", 2.0, "in", "A193-B7"),
    (7,  "CS150", 4.0, "in", None),        # material None
    (8,  "SS150", 4.0, "in", "A193-B8"),
    (9,  "CS150", 2.0, "in", "GRAPHITE"),
    (10, "SS150", 4.0, "in", "SPIRAL"),
    (11, "CS150", 2.0, "in", "GRAPHITE"),
    (12, "SS150", 4.0, "in", "SPIRAL"),
]
_LINE_GROUP_ROWS = [
    (100, "L-001"),
    (200, "L-002"),
    (300, "L-003"),
]
_REL_ROWS = [
    (1,  100), (2,  100), (9,  100),   # L-001
    (3,  200), (4,  200), (10, 200),   # L-002
    (5,  300), (11, 300),              # L-003
    # PnPIDs 6,7,8,12 → NO tienen relación → untagged
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Proyecto sintético canónico con BoltSet y Gasket."""
    return _make_project(
        tmp_path, "BG_TEST",
        bolt_rows=_BOLT_ROWS,
        gasket_rows=_GASKET_ROWS,
        ei_rows=_EI_ROWS,
        line_group_rows=_LINE_GROUP_ROWS,
        rel_rows=_REL_ROWS,
    )


@pytest.fixture
def result_line(proj: Path) -> dict:
    """bolt_gasket_list con group_by='line' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "line"})


@pytest.fixture
def result_spec(proj: Path) -> dict:
    """bolt_gasket_list con group_by='spec' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "spec"})


@pytest.fixture
def result_size(proj: Path) -> dict:
    """bolt_gasket_list con group_by='size' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "size"})


@pytest.fixture
def result_item_type(proj: Path) -> dict:
    """bolt_gasket_list con group_by='item_type' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "item_type"})


@pytest.fixture
def result_shop_field(proj: Path) -> dict:
    """bolt_gasket_list con group_by='shop_field' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "shop_field"})


@pytest.fixture
def result_material(proj: Path) -> dict:
    """bolt_gasket_list con group_by='material' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "material"})


@pytest.fixture
def result_bolt_size(proj: Path) -> dict:
    """bolt_gasket_list con group_by='bolt_size' y sin filtros ni tope."""
    return bolt_gasket_list(str(proj), {"limit": 0, "group_by": "bolt_size"})


# ===========================================================================
# Parte 1 — _bg_empty_metrics y _bg_accumulate
# ===========================================================================


class TestBgEmptyMetrics:
    def test_has_item_count(self):
        m = _bg_empty_metrics()
        assert "item_count" in m

    def test_has_bolt_sets(self):
        m = _bg_empty_metrics()
        assert "bolt_sets" in m

    def test_has_individual_bolts(self):
        m = _bg_empty_metrics()
        assert "individual_bolts" in m

    def test_has_gaskets(self):
        m = _bg_empty_metrics()
        assert "gaskets" in m

    def test_all_zeros(self):
        m = _bg_empty_metrics()
        assert m["item_count"] == 0
        assert m["bolt_sets"] == 0
        assert m["individual_bolts"] == 0
        assert m["gaskets"] == 0

    def test_returns_new_dict_each_call(self):
        m1 = _bg_empty_metrics()
        m2 = _bg_empty_metrics()
        assert m1 is not m2


class TestBgAccumulate:
    def test_bolt_increments_item_count(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _bolt(num_in_set=4.0))
        assert m["item_count"] == 1

    def test_bolt_increments_bolt_sets(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _bolt(num_in_set=4.0))
        assert m["bolt_sets"] == 1

    def test_bolt_adds_to_individual_bolts(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _bolt(num_in_set=4.0))
        assert m["individual_bolts"] == 4.0

    def test_bolt_does_not_increment_gaskets(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _bolt(num_in_set=4.0))
        assert m["gaskets"] == 0

    def test_gasket_increments_item_count(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _gasket())
        assert m["item_count"] == 1

    def test_gasket_increments_gaskets(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _gasket())
        assert m["gaskets"] == 1

    def test_gasket_does_not_increment_bolt_sets(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _gasket())
        assert m["bolt_sets"] == 0

    def test_gasket_does_not_increment_individual_bolts(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _gasket(num_in_set=0.0))
        assert m["individual_bolts"] == 0

    def test_num_in_set_float_04_sums_to_4(self):
        """NumberInSet='4.0' parseado a float debe sumar 4 a individual_bolts."""
        m = _bg_empty_metrics()
        _bg_accumulate(m, _bolt(num_in_set=4.0))
        _bg_accumulate(m, _bolt(num_in_set=0.0))  # '4.0' → 4.0, luego 0.0
        assert m["individual_bolts"] == 4.0

    def test_multiple_bolts_sum_individual_bolts(self):
        m = _bg_empty_metrics()
        for n in (4.0, 8.0, 12.0):
            _bg_accumulate(m, _bolt(num_in_set=n))
        assert m["individual_bolts"] == 24.0
        assert m["bolt_sets"] == 3

    def test_mixed_bolt_gasket(self):
        m = _bg_empty_metrics()
        _bg_accumulate(m, _bolt(num_in_set=4.0))
        _bg_accumulate(m, _gasket())
        assert m["item_count"] == 2
        assert m["bolt_sets"] == 1
        assert m["individual_bolts"] == 4.0
        assert m["gaskets"] == 1


# ===========================================================================
# Parte 2 — Agregación pura: _build_bolt_gasket_aggregates
# ===========================================================================


class TestBuildBoltGasketAggregatesGroupByLine:
    """Tests unitarios de _build_bolt_gasket_aggregates con group_by='line'."""

    @pytest.fixture
    def rows(self):
        """12 filas canónicas (mismos datos que el proyecto SQLite canónico)."""
        return [
            # Bolts con línea
            _bolt(line="L-001", shop_field="shop", spec="CS150", dia=2.0, num_in_set=4.0,  material="A193-B7", bolt_size="M16"),
            _bolt(line="L-001", shop_field="field", spec="CS150", dia=2.0, num_in_set=8.0,  material="A193-B7", bolt_size="M16"),
            _bolt(line="L-002", shop_field="shop", spec="SS150", dia=4.0, num_in_set=12.0, material="A193-B8", bolt_size='5/8"'),
            _bolt(line="L-002", shop_field="field", spec="SS150", dia=4.0, num_in_set=16.0, material="A193-B8", bolt_size='5/8"'),
            _bolt(line="L-003", shop_field="shop", spec="CS150", dia=2.0, num_in_set=4.0,  material="A193-B7", bolt_size="M16"),
            # Bolts sin línea (untagged)
            _bolt(line=None,  shop_field="shop",  spec="CS150", dia=2.0, num_in_set=4.0,  material="A193-B7", bolt_size="M16"),
            _bolt(line="",    shop_field="shop",  spec="CS150", dia=4.0, num_in_set=8.0,  material=None,      bolt_size='5/8"'),
            _bolt(line="?",   shop_field="field", spec="SS150", dia=4.0, num_in_set=16.0, material="A193-B8", bolt_size='5/8"'),
            # Gaskets con línea
            _gasket(line="L-001", shop_field="shop",  spec="CS150", dia=2.0, material="GRAPHITE"),
            _gasket(line="L-002", shop_field="field", spec="SS150", dia=4.0, material="SPIRAL"),
            _gasket(line="L-003", shop_field="shop",  spec="CS150", dia=2.0, material="GRAPHITE"),
            # Gasket sin línea (untagged)
            _gasket(line="?",    shop_field="field", spec="SS150", dia=4.0, material="SPIRAL"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "line")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert set(groups.keys()) == {"L-001", "L-002", "L-003", _NO_LINE_LABEL}

    def test_total_item_count(self, agg):
        _, _, _, totals, _ = agg
        assert totals["item_count"] == 12

    def test_total_bolt_sets(self, agg):
        _, _, _, totals, _ = agg
        assert totals["bolt_sets"] == 8

    def test_total_individual_bolts(self, agg):
        # 4+8+12+16+4+4+8+16 = 72
        _, _, _, totals, _ = agg
        assert totals["individual_bolts"] == 72.0

    def test_total_gaskets(self, agg):
        _, _, _, totals, _ = agg
        assert totals["gaskets"] == 4

    def test_untagged_item_count(self, agg):
        _, _, _, _, untagged = agg
        assert untagged["item_count"] == 4

    def test_untagged_bolt_sets(self, agg):
        _, _, _, _, untagged = agg
        assert untagged["bolt_sets"] == 3

    def test_untagged_individual_bolts(self, agg):
        # 4+8+16 = 28
        _, _, _, _, untagged = agg
        assert untagged["individual_bolts"] == 28.0

    def test_untagged_gaskets(self, agg):
        _, _, _, _, untagged = agg
        assert untagged["gaskets"] == 1

    def test_group_l001_item_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-001"]["item_count"] == 3

    def test_group_l001_bolt_sets(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-001"]["bolt_sets"] == 2

    def test_group_l001_individual_bolts(self, agg):
        # 4+8 = 12
        groups, _, _, _, _ = agg
        assert groups["L-001"]["individual_bolts"] == 12.0

    def test_group_l001_gaskets(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-001"]["gaskets"] == 1

    def test_group_l002_item_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-002"]["item_count"] == 3

    def test_group_l003_item_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-003"]["item_count"] == 2

    def test_group_l003_bolt_sets(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-003"]["bolt_sets"] == 1

    def test_group_l003_gaskets(self, agg):
        groups, _, _, _, _ = agg
        assert groups["L-003"]["gaskets"] == 1

    def test_group_no_line_item_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups[_NO_LINE_LABEL]["item_count"] == 4

    def test_by_item_type_bolt(self, agg):
        _, by_item_type, _, _, _ = agg
        bolt = by_item_type.get("bolt", {})
        assert bolt.get("item_count") == 8
        assert bolt.get("bolt_sets") == 8
        assert bolt.get("individual_bolts") == 72.0
        assert bolt.get("gaskets") == 0

    def test_by_item_type_gasket(self, agg):
        _, by_item_type, _, _, _ = agg
        gasket = by_item_type.get("gasket", {})
        assert gasket.get("item_count") == 4
        assert gasket.get("bolt_sets") == 0
        assert gasket.get("individual_bolts") == 0.0
        assert gasket.get("gaskets") == 4

    def test_by_shop_field_shop(self, agg):
        # shop: bolts 1,3,5,6 + gaskets 9,11 = 7
        _, _, by_sf, _, _ = agg
        assert by_sf.get("shop", {}).get("item_count") == 7

    def test_by_shop_field_field(self, agg):
        # field: bolts 2,4,8 + gaskets 10,12 = 5
        _, _, by_sf, _, _ = agg
        assert by_sf.get("field", {}).get("item_count") == 5


class TestBuildBoltGasketAggregatesGroupBySpec:
    @pytest.fixture
    def rows(self):
        return [
            _bolt(line="L-001", spec="CS150", dia=2.0, num_in_set=4.0),
            _bolt(line="L-001", spec="CS150", dia=2.0, num_in_set=8.0),
            _bolt(line="L-002", spec="SS150", dia=4.0, num_in_set=12.0),
            _bolt(line="L-002", spec="SS150", dia=4.0, num_in_set=16.0),
            _bolt(line="L-003", spec="CS150", dia=2.0, num_in_set=4.0),
            _bolt(line=None,    spec="CS150", dia=2.0, num_in_set=4.0),
            _bolt(line="",     spec="CS150", dia=4.0, num_in_set=8.0),
            _bolt(line="?",    spec="SS150", dia=4.0, num_in_set=16.0),
            _gasket(line="L-001", spec="CS150", dia=2.0),
            _gasket(line="L-002", spec="SS150", dia=4.0),
            _gasket(line="L-003", spec="CS150", dia=2.0),
            _gasket(line="?",    spec="SS150", dia=4.0),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "spec")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert set(groups.keys()) == {"CS150", "SS150"}

    def test_cs150_item_count(self, agg):
        # bolts 1,2,5,6,7 + gaskets 9,11 = 7
        groups, _, _, _, _ = agg
        assert groups["CS150"]["item_count"] == 7

    def test_ss150_item_count(self, agg):
        # bolts 3,4,8 + gaskets 10,12 = 5
        groups, _, _, _, _ = agg
        assert groups["SS150"]["item_count"] == 5

    def test_no_sin_linea_group(self, agg):
        """En group_by='spec', los untagged caen en su grupo natural."""
        groups, _, _, _, _ = agg
        assert _NO_LINE_LABEL not in groups

    def test_total_consistent(self, agg):
        _, _, _, totals, _ = agg
        assert totals["item_count"] == 12

    def test_untagged_still_tracked(self, agg):
        """untagged siempre se calcula, independiente del group_by."""
        _, _, _, _, untagged = agg
        assert untagged["item_count"] == 4


class TestBuildBoltGasketAggregatesGroupBySize:
    @pytest.fixture
    def rows(self):
        return [
            # 2"
            _bolt(line="L-001", spec="CS150", dia=2.0, dia_unit="in", num_in_set=4.0),
            _bolt(line="L-001", spec="CS150", dia=2.0, dia_unit="in", num_in_set=8.0),
            _bolt(line="L-003", spec="CS150", dia=2.0, dia_unit="in", num_in_set=4.0),
            _bolt(line=None,    spec="CS150", dia=2.0, dia_unit="in", num_in_set=4.0),
            _gasket(line="L-001", spec="CS150", dia=2.0, dia_unit="in"),
            _gasket(line="L-003", spec="CS150", dia=2.0, dia_unit="in"),
            # 4"
            _bolt(line="L-002", spec="SS150", dia=4.0, dia_unit="in", num_in_set=12.0),
            _bolt(line="L-002", spec="SS150", dia=4.0, dia_unit="in", num_in_set=16.0),
            _bolt(line="",     spec="CS150", dia=4.0, dia_unit="in", num_in_set=8.0),
            _bolt(line="?",    spec="SS150", dia=4.0, dia_unit="in", num_in_set=16.0),
            _gasket(line="L-002", spec="SS150", dia=4.0, dia_unit="in"),
            _gasket(line="?",    spec="SS150", dia=4.0, dia_unit="in"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "size")

    def test_two_size_groups(self, agg):
        groups, _, _, _, _ = agg
        assert len(groups) == 2

    def test_2in_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups.get('2"', {}).get("item_count") == 6

    def test_4in_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups.get('4"', {}).get("item_count") == 6

    def test_no_line_label_absent(self, agg):
        """En group_by='size', los untagged caen en su grupo de size."""
        groups, _, _, _, _ = agg
        assert _NO_LINE_LABEL not in groups


class TestBuildBoltGasketAggregatesGroupByItemType:
    @pytest.fixture
    def rows(self):
        return [
            _bolt(line="L-001", num_in_set=4.0),
            _bolt(line="L-001", num_in_set=8.0),
            _bolt(line=None,   num_in_set=4.0),
            _gasket(line="L-001"),
            _gasket(line=None),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "item_type")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert set(groups.keys()) == {"bolt", "gasket"}

    def test_bolt_group_item_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["bolt"]["item_count"] == 3

    def test_gasket_group_item_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["gasket"]["item_count"] == 2

    def test_bolt_group_individual_bolts(self, agg):
        # 4+8+4 = 16
        groups, _, _, _, _ = agg
        assert groups["bolt"]["individual_bolts"] == 16.0


class TestBuildBoltGasketAggregatesGroupByShopField:
    @pytest.fixture
    def rows(self):
        return [
            _bolt(line="L-001", shop_field="shop",  num_in_set=4.0),
            _bolt(line="L-001", shop_field="field", num_in_set=8.0),
            _gasket(line="L-001", shop_field="shop"),
            _gasket(line=None,   shop_field=_UNKNOWN_SHOP_FIELD),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "shop_field")

    def test_group_keys(self, agg):
        groups, _, _, _, _ = agg
        assert "shop" in groups
        assert "field" in groups
        assert _UNKNOWN_SHOP_FIELD in groups

    def test_shop_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["shop"]["item_count"] == 2

    def test_field_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["field"]["item_count"] == 1

    def test_unknown_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups[_UNKNOWN_SHOP_FIELD]["item_count"] == 1


class TestBuildBoltGasketAggregatesGroupByMaterial:
    @pytest.fixture
    def rows(self):
        return [
            _bolt(line="L-001", material="A193-B7", num_in_set=4.0),
            _bolt(line="L-001", material="A193-B7", num_in_set=8.0),
            _bolt(line="L-002", material="A193-B8", num_in_set=12.0),
            _bolt(line=None,   material=None,       num_in_set=4.0),  # None → "(sin)"
            _bolt(line=None,   material="",         num_in_set=8.0),  # "" → "(sin)"
            _gasket(line="L-001", material="GRAPHITE"),
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "material")

    def test_material_keys(self, agg):
        groups, _, _, _, _ = agg
        assert "A193-B7" in groups
        assert "A193-B8" in groups
        assert "(sin)" in groups
        assert "GRAPHITE" in groups

    def test_none_material_in_sin(self, agg):
        groups, _, _, _, _ = agg
        assert groups["(sin)"]["item_count"] == 2

    def test_empty_material_in_sin(self, agg):
        """Material vacío '' debe ir al grupo '(sin)', igual que None."""
        groups, _, _, _, _ = agg
        assert groups["(sin)"]["item_count"] == 2

    def test_a193b7_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["A193-B7"]["item_count"] == 2

    def test_a193b8_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["A193-B8"]["item_count"] == 1

    def test_graphite_count(self, agg):
        groups, _, _, _, _ = agg
        assert groups["GRAPHITE"]["item_count"] == 1


class TestBuildBoltGasketAggregatesGroupByBoltSize:
    @pytest.fixture
    def rows(self):
        return [
            _bolt(line="L-001", bolt_size="M16",   num_in_set=4.0),
            _bolt(line="L-001", bolt_size="M16",   num_in_set=8.0),
            _bolt(line="L-002", bolt_size='5/8"',  num_in_set=12.0),
            _bolt(line=None,   bolt_size=None,     num_in_set=4.0),  # None → "(sin)"
            _gasket(line="L-001"),  # bolt_size=None → "(sin)"
            _gasket(line="L-002"),  # bolt_size=None → "(sin)"
        ]

    @pytest.fixture
    def agg(self, rows):
        return _build_bolt_gasket_aggregates(rows, "bolt_size")

    def test_m16_group(self, agg):
        groups, _, _, _, _ = agg
        assert groups.get("M16", {}).get("item_count") == 2

    def test_58_group(self, agg):
        groups, _, _, _, _ = agg
        assert groups.get('5/8"', {}).get("item_count") == 1

    def test_sin_group_includes_gaskets_and_none_bolt_size(self, agg):
        """Juntas y pernos con bolt_size=None caen en '(sin)'."""
        groups, _, _, _, _ = agg
        assert groups.get("(sin)", {}).get("item_count") == 3

    def test_total_item_count(self, agg):
        _, _, _, totals, _ = agg
        assert totals["item_count"] == 6


class TestBuildBoltGasketAggregatesEmpty:
    def test_empty_rows(self):
        groups, by_it, by_sf, totals, untagged = _build_bolt_gasket_aggregates([], "line")
        assert groups == {}
        assert by_it == {}
        assert by_sf == {}
        assert totals == _bg_empty_metrics()
        assert untagged == _bg_empty_metrics()


# ===========================================================================
# Parte 3 — NumberInSet no numérico
# ===========================================================================


class TestNumberInSetNonNumeric:
    """NumberInSet 'N/A', '', None → no lanza; contribuye 0; bolt_sets aumenta."""

    def _make_proj_with_bad_num(self, tmp_path: Path, num_value: str | None) -> Path:
        """Proyecto con un bolt cuyo NumberInSet es no numérico."""
        return _make_project(
            tmp_path, f"BAD_NUM_{id(num_value)}",
            bolt_rows=[(1, "SHOP", num_value, "M16")],
            gasket_rows=[],
            ei_rows=[(1, "CS150", 2.0, "in", "A193-B7")],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100)],
        )

    def test_na_no_raise(self, tmp_path):
        proj = self._make_proj_with_bad_num(tmp_path, "N/A")
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["ok"] is True

    def test_na_bolt_sets_counted(self, tmp_path):
        proj = self._make_proj_with_bad_num(tmp_path, "N/A")
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["totals"]["bolt_sets"] == 1

    def test_na_individual_bolts_zero(self, tmp_path):
        proj = self._make_proj_with_bad_num(tmp_path, "N/A")
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["totals"]["individual_bolts"] == 0

    def test_na_item_count_counted(self, tmp_path):
        proj = self._make_proj_with_bad_num(tmp_path, "N/A")
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["totals"]["item_count"] == 1

    def test_na_note_present(self, tmp_path):
        proj = self._make_proj_with_bad_num(tmp_path, "N/A")
        r = bolt_gasket_list(str(proj), {"limit": 0})
        note_text = " ".join(r["notes"]).lower()
        assert "numberin" in note_text or "no numéric" in note_text or "numéric" in note_text

    def test_empty_string_no_raise(self, tmp_path):
        proj = self._make_proj_with_bad_num(tmp_path, "")
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["totals"]["individual_bolts"] == 0

    def test_none_num_in_set_no_raise(self, tmp_path):
        """None en NumberInSet (columna NULL): no lanza, contribuye 0."""
        proj = self._make_proj_with_bad_num(tmp_path, None)
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["ok"] is True
        assert r["totals"]["individual_bolts"] == 0

    def test_float_string_summed_as_int(self, tmp_path):
        """NumberInSet='4.0' se parsea como 4; expuesto como int 4."""
        proj = _make_project(
            tmp_path, "FLOAT_STRING",
            bolt_rows=[(1, "SHOP", "4.0", "M16")],
            gasket_rows=[],
            ei_rows=[(1, "CS150", 2.0, "in", "A193-B7")],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100)],
        )
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["totals"]["individual_bolts"] == 4


# ===========================================================================
# Parte 4 — Estructura de salida
# ===========================================================================


class TestOutputStructure:
    def test_ok_true(self, result_line):
        assert result_line["ok"] is True

    def test_required_top_level_keys(self, result_line):
        required = (
            "ok", "project", "path", "limit", "group_by", "filters",
            "totals", "by_item_type", "by_shop_field", "untagged",
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

    def test_by_item_type_is_list(self, result_line):
        assert isinstance(result_line["by_item_type"], list)

    def test_by_shop_field_is_list(self, result_line):
        assert isinstance(result_line["by_shop_field"], list)

    def test_notes_is_list(self, result_line):
        assert isinstance(result_line["notes"], list)

    def test_totals_has_all_metric_keys(self, result_line):
        t = result_line["totals"]
        assert "item_count" in t
        assert "bolt_sets" in t
        assert "individual_bolts" in t
        assert "gaskets" in t

    def test_untagged_has_all_metric_keys(self, result_line):
        u = result_line["untagged"]
        assert "item_count" in u
        assert "bolt_sets" in u
        assert "individual_bolts" in u
        assert "gaskets" in u

    def test_individual_bolts_is_int(self, result_line):
        """individual_bolts debe ser int, no float."""
        assert isinstance(result_line["totals"]["individual_bolts"], int)

    def test_groups_entries_have_group_and_metrics(self, result_line):
        for g in result_line["groups"]:
            assert "group" in g
            assert "item_count" in g
            assert "bolt_sets" in g
            assert "individual_bolts" in g
            assert "gaskets" in g

    def test_by_item_type_entries_have_required_keys(self, result_line):
        for entry in result_line["by_item_type"]:
            assert "item_type" in entry
            assert "item_count" in entry
            assert "individual_bolts" in entry

    def test_by_shop_field_entries_have_required_keys(self, result_line):
        for entry in result_line["by_shop_field"]:
            assert "shop_field" in entry
            assert "item_count" in entry

    def test_group_by_echoed(self, result_line):
        assert result_line["group_by"] == "line"

    def test_default_limit_is_50(self, proj):
        r = bolt_gasket_list(str(proj))
        assert r["limit"] == 50

    def test_limit_zero_reflected(self, result_line):
        assert result_line["limit"] == 0

    def test_filters_empty_without_filters(self, result_line):
        assert result_line["filters"] == {}


# ===========================================================================
# Parte 5 — Totales y desgloses globales
# ===========================================================================


class TestTotalsAndBreakdowns:
    def test_total_item_count(self, result_line):
        assert result_line["totals"]["item_count"] == 12

    def test_total_bolt_sets(self, result_line):
        assert result_line["totals"]["bolt_sets"] == 8

    def test_total_individual_bolts(self, result_line):
        # 4+8+12+16+4+4+8+16 = 72
        assert result_line["totals"]["individual_bolts"] == 72

    def test_total_gaskets(self, result_line):
        assert result_line["totals"]["gaskets"] == 4

    def test_untagged_item_count(self, result_line):
        # 6,7,8 (bolts) + 12 (gasket) = 4 untagged
        assert result_line["untagged"]["item_count"] == 4

    def test_untagged_bolt_sets(self, result_line):
        assert result_line["untagged"]["bolt_sets"] == 3

    def test_untagged_individual_bolts(self, result_line):
        # PnPIDs 6(4)+7(8)+8(16) = 28
        assert result_line["untagged"]["individual_bolts"] == 28

    def test_untagged_gaskets(self, result_line):
        assert result_line["untagged"]["gaskets"] == 1

    def test_by_item_type_bolt(self, result_line):
        bolt = next(
            (e for e in result_line["by_item_type"] if e["item_type"] == "bolt"), None
        )
        assert bolt is not None
        assert bolt["item_count"] == 8
        assert bolt["individual_bolts"] == 72

    def test_by_item_type_gasket(self, result_line):
        gasket = next(
            (e for e in result_line["by_item_type"] if e["item_type"] == "gasket"), None
        )
        assert gasket is not None
        assert gasket["item_count"] == 4

    def test_by_shop_field_shop(self, result_line):
        # shop: bolts 1,3,5,6 + gaskets 9,11 = 7 items
        shop = next(
            (e for e in result_line["by_shop_field"] if e["shop_field"] == "shop"), None
        )
        assert shop is not None
        assert shop["item_count"] == 7

    def test_by_shop_field_field(self, result_line):
        # field: bolts 2,4,8 + gaskets 10,12 = 5 items
        field = next(
            (e for e in result_line["by_shop_field"] if e["shop_field"] == "field"), None
        )
        assert field is not None
        assert field["item_count"] == 5

    def test_by_item_type_ranked_desc(self, result_line):
        counts = [e["item_count"] for e in result_line["by_item_type"]]
        assert counts == sorted(counts, reverse=True)

    def test_by_shop_field_ranked_desc(self, result_line):
        counts = [e["item_count"] for e in result_line["by_shop_field"]]
        assert counts == sorted(counts, reverse=True)


# ===========================================================================
# Parte 6 — group_by: agrupaciones correctas
# ===========================================================================


class TestGroupByLine:
    def test_group_count(self, result_line):
        # L-001, L-002, L-003, (SIN LÍNEA)
        assert result_line["group_count"] == 4

    def test_l001_item_count(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == "L-001"), None)
        assert g is not None
        assert g["item_count"] == 3

    def test_l002_item_count(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == "L-002"), None)
        assert g is not None
        assert g["item_count"] == 3

    def test_l003_item_count(self, result_line):
        g = next((g for g in result_line["groups"] if g["group"] == "L-003"), None)
        assert g is not None
        assert g["item_count"] == 2

    def test_no_line_group(self, result_line):
        g = next(
            (g for g in result_line["groups"] if g["group"] == _NO_LINE_LABEL), None
        )
        assert g is not None
        assert g["item_count"] == 4

    def test_groups_ordered_desc_then_asc(self, result_line):
        counts = [g["item_count"] for g in result_line["groups"]]
        assert counts == sorted(counts, reverse=True)

    def test_total_equals_sum_of_groups_item_count(self, result_line):
        total = sum(g["item_count"] for g in result_line["groups"])
        assert total == result_line["totals"]["item_count"]


class TestGroupBySpec:
    def test_group_count(self, result_spec):
        assert result_spec["group_count"] == 2

    def test_cs150_item_count(self, result_spec):
        # bolts 1,2,5,6,7 + gaskets 9,11 = 7
        g = next((g for g in result_spec["groups"] if g["group"] == "CS150"), None)
        assert g is not None
        assert g["item_count"] == 7

    def test_ss150_item_count(self, result_spec):
        # bolts 3,4,8 + gaskets 10,12 = 5
        g = next((g for g in result_spec["groups"] if g["group"] == "SS150"), None)
        assert g is not None
        assert g["item_count"] == 5

    def test_no_sin_linea_group(self, result_spec):
        groups = [g["group"] for g in result_spec["groups"]]
        assert _NO_LINE_LABEL not in groups


class TestGroupBySize:
    def test_group_count(self, result_size):
        assert result_size["group_count"] == 2

    def test_2in_item_count(self, result_size):
        # bolts 1,2,5,6 + gaskets 9,11 = 6
        g = next((g for g in result_size["groups"] if g["group"] == '2"'), None)
        assert g is not None, f"Grupos: {[g['group'] for g in result_size['groups']]}"
        assert g["item_count"] == 6

    def test_4in_item_count(self, result_size):
        # bolts 3,4,7,8 + gaskets 10,12 = 6
        g = next((g for g in result_size["groups"] if g["group"] == '4"'), None)
        assert g is not None, f"Grupos: {[g['group'] for g in result_size['groups']]}"
        assert g["item_count"] == 6


class TestGroupByItemType:
    def test_group_count(self, result_item_type):
        assert result_item_type["group_count"] == 2

    def test_bolt_item_count(self, result_item_type):
        g = next(
            (g for g in result_item_type["groups"] if g["group"] == "bolt"), None
        )
        assert g is not None
        assert g["item_count"] == 8

    def test_gasket_item_count(self, result_item_type):
        g = next(
            (g for g in result_item_type["groups"] if g["group"] == "gasket"), None
        )
        assert g is not None
        assert g["item_count"] == 4

    def test_bolt_individual_bolts(self, result_item_type):
        g = next(
            (g for g in result_item_type["groups"] if g["group"] == "bolt"), None
        )
        assert g is not None
        assert g["individual_bolts"] == 72


class TestGroupByShopField:
    def test_group_count(self, result_shop_field):
        assert result_shop_field["group_count"] == 2

    def test_shop_item_count(self, result_shop_field):
        g = next(
            (g for g in result_shop_field["groups"] if g["group"] == "shop"), None
        )
        assert g is not None
        assert g["item_count"] == 7

    def test_field_item_count(self, result_shop_field):
        g = next(
            (g for g in result_shop_field["groups"] if g["group"] == "field"), None
        )
        assert g is not None
        assert g["item_count"] == 5


class TestGroupByMaterial:
    def test_none_material_in_sin_group(self, result_material):
        """Material None en PnPID 7 → grupo '(sin)'."""
        g = next(
            (g for g in result_material["groups"] if g["group"] == "(sin)"), None
        )
        assert g is not None
        assert g["item_count"] >= 1

    def test_empty_string_material_also_in_sin_group(self, tmp_path):
        """Material '' → grupo '(sin)', igual que None."""
        proj = _make_project(
            tmp_path, "EMPTY_MAT",
            bolt_rows=[(1, "SHOP", "4", "M16")],
            gasket_rows=[],
            ei_rows=[(1, "CS150", 2.0, "in", "")],  # material vacío
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100)],
        )
        r = bolt_gasket_list(str(proj), {"limit": 0, "group_by": "material"})
        groups = {g["group"]: g for g in r["groups"]}
        assert "(sin)" in groups
        assert groups["(sin)"]["item_count"] == 1

    def test_real_materials_grouped(self, result_material):
        groups = {g["group"]: g for g in result_material["groups"]}
        assert "A193-B7" in groups
        assert "A193-B8" in groups
        assert "GRAPHITE" in groups
        assert "SPIRAL" in groups


class TestGroupByBoltSize:
    def test_m16_group(self, result_bolt_size):
        # bolts PnPIDs 1,2,5,6 → M16 = 4 sets
        g = next(
            (g for g in result_bolt_size["groups"] if g["group"] == "M16"), None
        )
        assert g is not None
        assert g["item_count"] == 4

    def test_58_group(self, result_bolt_size):
        # bolts PnPIDs 3,4,7,8 → 5/8" = 4 sets
        g = next(
            (g for g in result_bolt_size["groups"] if g["group"] == '5/8"'), None
        )
        assert g is not None
        assert g["item_count"] == 4

    def test_sin_group_has_gaskets(self, result_bolt_size):
        """Juntas (sin bolt_size) van al grupo '(sin)'."""
        g = next(
            (g for g in result_bolt_size["groups"] if g["group"] == "(sin)"), None
        )
        assert g is not None
        # 4 gaskets (9,10,11,12) → group "(sin)"
        assert g["gaskets"] == 4


# ===========================================================================
# Parte 7 — Filtros
# ===========================================================================


class TestFilterItemType:
    def test_filter_bolt_only_boltset(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "item_type": "bolt"})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 8
        assert r["totals"]["bolt_sets"] == 8
        assert r["totals"]["gaskets"] == 0
        assert r["filters"]["item_type"] == "bolt"

    def test_filter_gasket_only_gasket(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "item_type": "gasket"})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 4
        assert r["totals"]["gaskets"] == 4
        assert r["totals"]["bolt_sets"] == 0

    def test_filter_bolt_by_item_type_scoped(self, proj):
        """Con item_type=bolt, by_item_type solo tiene 'bolt'."""
        r = bolt_gasket_list(str(proj), {"limit": 0, "item_type": "bolt"})
        types = {e["item_type"] for e in r["by_item_type"]}
        assert types == {"bolt"}

    def test_filter_invalid_item_type_ignored(self, proj):
        """item_type no reconocido se ignora; se consultan ambas tablas."""
        r = bolt_gasket_list(str(proj), {"limit": 0, "item_type": "washer"})
        assert r["totals"]["item_count"] == 12  # filtro ignorado
        assert "item_type" not in r["filters"]
        assert any("item_type" in n for n in r["notes"])


class TestFilterLine:
    def test_filter_line_l001(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "line": "L-001"})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 3
        assert r["filters"]["line"] == "L-001"

    def test_filter_line_case_insensitive(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "line": "l-001"})
        assert r["totals"]["item_count"] == 3

    def test_filter_line_l002(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "line": "L-002"})
        assert r["totals"]["item_count"] == 3

    def test_filter_line_nonexistent(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "line": "L-999"})
        assert r["totals"]["item_count"] == 0
        assert r["groups"] == []

    def test_filter_line_by_item_type_scoped(self, proj):
        """by_item_type se acota al alcance del filtro de línea."""
        r = bolt_gasket_list(str(proj), {"limit": 0, "line": "L-001"})
        # L-001: 2 bolts + 1 gasket
        by_it = {e["item_type"]: e["item_count"] for e in r["by_item_type"]}
        assert by_it.get("bolt") == 2
        assert by_it.get("gasket") == 1


class TestFilterSpec:
    def test_filter_spec_cs150(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "spec": "CS150"})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 7
        assert r["filters"]["spec"] == "CS150"

    def test_filter_spec_ss150(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "spec": "SS150"})
        assert r["totals"]["item_count"] == 5

    def test_filter_spec_case_insensitive(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "spec": "cs150"})
        assert r["totals"]["item_count"] == 7

    def test_filter_spec_nonexistent(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "spec": "X999"})
        assert r["totals"]["item_count"] == 0


class TestFilterSize:
    def test_filter_size_2in(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "size": {"value": 2.0, "unit": "in"}})
        assert r["ok"] is True
        # bolts 1,2,5,6 + gaskets 9,11 = 6
        assert r["totals"]["item_count"] == 6
        assert r["filters"]["size"] == {"value": 2.0, "unit": "IN"}

    def test_filter_size_4in(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "size": {"value": 4.0, "unit": "in"}})
        assert r["totals"]["item_count"] == 6

    def test_filter_size_without_unit_ignored(self, proj):
        """size sin unidad se ignora y se añade nota."""
        r = bolt_gasket_list(str(proj), {"limit": 0, "size": 2.0})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 12  # filtro ignorado
        assert "size" not in r["filters"]
        assert any(
            "size" in n.lower() or "unidad" in n.lower() for n in r["notes"]
        )

    def test_filter_size_dict_without_unit_ignored(self, proj):
        """size como dict sin clave 'unit' se ignora."""
        r = bolt_gasket_list(str(proj), {"limit": 0, "size": {"value": 2.0}})
        assert r["totals"]["item_count"] == 12
        assert "size" not in r["filters"]


class TestFilterShopField:
    def test_filter_shop(self, proj):
        # shop: bolts 1,3,5,6 + gaskets 9,11 = 7
        r = bolt_gasket_list(str(proj), {"limit": 0, "shop_field": "shop"})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 7
        assert r["filters"]["shop_field"] == "shop"

    def test_filter_field(self, proj):
        # field: bolts 2,4,8 + gaskets 10,12 = 5
        r = bolt_gasket_list(str(proj), {"limit": 0, "shop_field": "field"})
        assert r["totals"]["item_count"] == 5

    def test_filter_shop_uppercase(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "shop_field": "SHOP"})
        assert r["totals"]["item_count"] == 7

    def test_filter_shop_field_invalid_ignored(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "shop_field": "factory"})
        assert r["totals"]["item_count"] == 12  # filtro ignorado
        assert "shop_field" not in r["filters"]
        assert any("shop_field" in n for n in r["notes"])


class TestFilterCombined:
    def test_line_and_spec(self, proj):
        # L-001 + CS150: bolts 1,2 + gasket 9 = 3
        r = bolt_gasket_list(str(proj), {"limit": 0, "line": "L-001", "spec": "CS150"})
        assert r["totals"]["item_count"] == 3

    def test_line_and_item_type_bolt(self, proj):
        # L-001 + bolt: PnPIDs 1,2
        r = bolt_gasket_list(
            str(proj), {"limit": 0, "line": "L-001", "item_type": "bolt"}
        )
        assert r["totals"]["item_count"] == 2

    def test_spec_and_shop_field(self, proj):
        # CS150 + shop: bolts 1,5,6,7 (PnPID 7 es CS150/shop aunque material=None)
        # + gaskets 9,11 = 6
        r = bolt_gasket_list(
            str(proj), {"limit": 0, "spec": "CS150", "shop_field": "shop"}
        )
        assert r["totals"]["item_count"] == 6

    def test_item_type_and_size(self, proj):
        # bolt + 2": PnPIDs 1,2,5,6 = 4
        r = bolt_gasket_list(
            str(proj),
            {"limit": 0, "item_type": "bolt", "size": {"value": 2.0, "unit": "in"}},
        )
        assert r["totals"]["item_count"] == 4

    def test_combined_totals_and_by_scoped(self, proj):
        """Los by_item_type y by_shop_field quedan acotados al filtro."""
        r = bolt_gasket_list(
            str(proj), {"limit": 0, "line": "L-001", "item_type": "bolt"}
        )
        types = {e["item_type"] for e in r["by_item_type"]}
        assert types == {"bolt"}


# ===========================================================================
# Parte 8 — limit / omitted
# ===========================================================================


class TestLimitOmitted:
    def test_limit_1_returns_1_group(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 1, "group_by": "line"})
        assert len(r["groups"]) == 1

    def test_limit_1_omitted_is_3(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 1, "group_by": "line"})
        assert r["omitted"] == 3

    def test_limit_0_no_cap(self, result_line):
        assert result_line["omitted"] == 0
        assert len(result_line["groups"]) == result_line["group_count"]

    def test_total_item_count_not_affected_by_limit(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 1, "group_by": "line"})
        assert r["totals"]["item_count"] == 12

    def test_by_item_type_not_affected_by_limit(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 1, "group_by": "line"})
        bolt = next(
            (e for e in r["by_item_type"] if e["item_type"] == "bolt"), None
        )
        assert bolt is not None
        assert bolt["item_count"] == 8

    def test_untagged_not_affected_by_limit(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 1, "group_by": "line"})
        assert r["untagged"]["item_count"] == 4

    def test_group_count_reflects_total_before_cap(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 2, "group_by": "line"})
        assert r["group_count"] == 4
        assert len(r["groups"]) == 2
        assert r["omitted"] == 2

    def test_default_limit_50(self, proj):
        r = bolt_gasket_list(str(proj))
        assert r["limit"] == 50
        assert r["omitted"] == 0


# ===========================================================================
# Parte 9 — group_by inválido cae a "line"
# ===========================================================================


class TestGroupByInvalid:
    def test_invalid_fallback_to_line(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "group_by": "xyz"})
        assert r["ok"] is True
        assert r["group_by"] == "line"

    def test_invalid_group_by_note(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "group_by": "xyz"})
        assert any("group_by" in n for n in r["notes"])

    def test_invalid_group_by_data_correct(self, proj):
        r = bolt_gasket_list(str(proj), {"limit": 0, "group_by": "xyz"})
        # Comportamiento de group_by=line
        assert r["totals"]["item_count"] == 12
        groups_keys = {g["group"] for g in r["groups"]}
        assert "L-001" in groups_keys


# ===========================================================================
# Parte 10 — Degradación de esquema
# ===========================================================================


class TestDegradationNoTables:
    """Sin ninguna tabla BoltSet/Gasket → ok:True, totales 0, listas vacías."""

    @pytest.fixture
    def proj_no_tables(self, tmp_path: Path) -> Path:
        proj = tmp_path / "NO_BG_TABLES"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        con = sqlite3.connect(str(proj / "Piping.dcf"))
        con.execute("CREATE TABLE EngineeringItems (PnPID INTEGER, Spec TEXT)")
        con.commit()
        con.close()
        return proj

    def test_ok_true(self, proj_no_tables):
        r = bolt_gasket_list(str(proj_no_tables), {"limit": 0})
        assert r["ok"] is True

    def test_item_count_zero(self, proj_no_tables):
        r = bolt_gasket_list(str(proj_no_tables), {"limit": 0})
        assert r["totals"]["item_count"] == 0

    def test_groups_empty(self, proj_no_tables):
        r = bolt_gasket_list(str(proj_no_tables), {"limit": 0})
        assert r["groups"] == []

    def test_by_item_type_empty(self, proj_no_tables):
        r = bolt_gasket_list(str(proj_no_tables), {"limit": 0})
        assert r["by_item_type"] == []

    def test_by_shop_field_empty(self, proj_no_tables):
        r = bolt_gasket_list(str(proj_no_tables), {"limit": 0})
        assert r["by_shop_field"] == []

    def test_note_present(self, proj_no_tables):
        r = bolt_gasket_list(str(proj_no_tables), {"limit": 0})
        note_text = " ".join(r["notes"]).lower()
        assert "boltset" in note_text or "gasket" in note_text or "pernos" in note_text


class TestDegradationOnlyBoltSet:
    """Solo BoltSet presente → usa BoltSet; nota de Gasket ausente."""

    @pytest.fixture
    def proj_only_bolt(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "ONLY_BOLT",
            bolt_rows=[(1, "SHOP", "4", "M16"), (2, "FIELD", "8", "M16")],
            gasket_rows=[],
            ei_rows=[
                (1, "CS150", 2.0, "in", "A193-B7"),
                (2, "CS150", 2.0, "in", "A193-B7"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100)],
            create_gasket=False,  # sin tabla Gasket
        )

    def test_ok_true(self, proj_only_bolt):
        r = bolt_gasket_list(str(proj_only_bolt), {"limit": 0})
        assert r["ok"] is True

    def test_item_count_2(self, proj_only_bolt):
        r = bolt_gasket_list(str(proj_only_bolt), {"limit": 0})
        assert r["totals"]["item_count"] == 2
        assert r["totals"]["bolt_sets"] == 2
        assert r["totals"]["gaskets"] == 0

    def test_note_about_absent_gasket(self, proj_only_bolt):
        r = bolt_gasket_list(str(proj_only_bolt), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "Gasket" in note_text or "gasket" in note_text.lower()


class TestDegradationOnlyGasket:
    """Solo Gasket presente → usa Gasket; nota de BoltSet ausente."""

    @pytest.fixture
    def proj_only_gasket(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "ONLY_GASKET",
            bolt_rows=[],
            gasket_rows=[(1, "SHOP"), (2, "FIELD")],
            ei_rows=[
                (1, "CS150", 2.0, "in", "GRAPHITE"),
                (2, "SS150", 4.0, "in", "SPIRAL"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100)],
            create_boltset=False,  # sin tabla BoltSet
        )

    def test_ok_true(self, proj_only_gasket):
        r = bolt_gasket_list(str(proj_only_gasket), {"limit": 0})
        assert r["ok"] is True

    def test_item_count_2(self, proj_only_gasket):
        r = bolt_gasket_list(str(proj_only_gasket), {"limit": 0})
        assert r["totals"]["item_count"] == 2
        assert r["totals"]["gaskets"] == 2
        assert r["totals"]["bolt_sets"] == 0
        assert r["totals"]["individual_bolts"] == 0

    def test_note_about_absent_boltset(self, proj_only_gasket):
        r = bolt_gasket_list(str(proj_only_gasket), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "BoltSet" in note_text or "bolt" in note_text.lower()


class TestDegradationNoNumberInSet:
    """BoltSet sin columna NumberInSet → individual_bolts=0; nota."""

    @pytest.fixture
    def proj_no_num(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_NUM_IN_SET",
            bolt_rows=[(1, "SHOP", "4", "M16"), (2, "FIELD", "8", "M16")],
            gasket_rows=[],
            ei_rows=[
                (1, "CS150", 2.0, "in", "A193-B7"),
                (2, "CS150", 2.0, "in", "A193-B7"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100)],
            include_num_in_set=False,
        )

    def test_ok_true(self, proj_no_num):
        r = bolt_gasket_list(str(proj_no_num), {"limit": 0})
        assert r["ok"] is True

    def test_individual_bolts_zero(self, proj_no_num):
        r = bolt_gasket_list(str(proj_no_num), {"limit": 0})
        assert r["totals"]["individual_bolts"] == 0

    def test_bolt_sets_still_counted(self, proj_no_num):
        r = bolt_gasket_list(str(proj_no_num), {"limit": 0})
        assert r["totals"]["bolt_sets"] == 2

    def test_note_about_missing_num_in_set(self, proj_no_num):
        r = bolt_gasket_list(str(proj_no_num), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "NumberInSet" in note_text


class TestDegradationNoBoltSize:
    """BoltSet sin columna BoltSize → bolt_size=None; group_by=bolt_size agrupa en '(sin)'."""

    @pytest.fixture
    def proj_no_bolt_size(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_BOLT_SIZE",
            bolt_rows=[(1, "SHOP", "4", None), (2, "FIELD", "8", None)],
            gasket_rows=[],
            ei_rows=[
                (1, "CS150", 2.0, "in", "A193-B7"),
                (2, "CS150", 2.0, "in", "A193-B7"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100)],
            include_bolt_size=False,
        )

    def test_ok_true(self, proj_no_bolt_size):
        r = bolt_gasket_list(str(proj_no_bolt_size), {"limit": 0})
        assert r["ok"] is True

    def test_bolt_size_group_by_all_sin(self, proj_no_bolt_size):
        r = bolt_gasket_list(
            str(proj_no_bolt_size), {"limit": 0, "group_by": "bolt_size"}
        )
        groups = {g["group"]: g for g in r["groups"]}
        assert "(sin)" in groups
        assert groups["(sin)"]["item_count"] == 2


class TestDegradationNoShopField:
    """BoltSet sin columna Shop_Field → "(desconocido)"; nota."""

    @pytest.fixture
    def proj_no_sf(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_SF",
            bolt_rows=[(1, None, "4", "M16"), (2, None, "8", "M16")],
            gasket_rows=[(3, "SHOP")],
            ei_rows=[
                (1, "CS150", 2.0, "in", "A193-B7"),
                (2, "CS150", 2.0, "in", "A193-B7"),
                (3, "CS150", 2.0, "in", "GRAPHITE"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100), (2, 100), (3, 100)],
            include_shop_field_bolt=False,
        )

    def test_ok_true(self, proj_no_sf):
        r = bolt_gasket_list(str(proj_no_sf), {"limit": 0})
        assert r["ok"] is True

    def test_bolt_unknown_shop_field(self, proj_no_sf):
        r = bolt_gasket_list(str(proj_no_sf), {"limit": 0})
        by_sf = {e["shop_field"]: e["item_count"] for e in r["by_shop_field"]}
        # Bolts (2) → (desconocido), Gasket (1) → shop
        assert by_sf.get(_UNKNOWN_SHOP_FIELD, 0) == 2
        assert by_sf.get("shop", 0) == 1

    def test_note_about_missing_shop_field(self, proj_no_sf):
        r = bolt_gasket_list(str(proj_no_sf), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "Shop_Field" in note_text


class TestDegradationNoLineRelationship:
    """Sin tablas de relación de línea → todos los items en untagged."""

    @pytest.fixture
    def proj_no_rel(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_REL",
            bolt_rows=[(1, "SHOP", "4", "M16"), (2, "FIELD", "8", "M16")],
            gasket_rows=[(3, "SHOP")],
            ei_rows=[
                (1, "CS150", 2.0, "in", "A193-B7"),
                (2, "CS150", 2.0, "in", "A193-B7"),
                (3, "CS150", 2.0, "in", "GRAPHITE"),
            ],
            line_group_rows=None,
            rel_rows=None,
            create_line_group=False,
            create_rel=False,
        )

    def test_all_untagged(self, proj_no_rel):
        r = bolt_gasket_list(str(proj_no_rel), {"limit": 0})
        assert r["untagged"]["item_count"] == 3

    def test_all_in_no_line_group(self, proj_no_rel):
        r = bolt_gasket_list(str(proj_no_rel), {"limit": 0, "group_by": "line"})
        groups = {g["group"]: g for g in r["groups"]}
        assert groups.get(_NO_LINE_LABEL, {}).get("item_count") == 3
        assert len(groups) == 1

    def test_note_about_missing_tables(self, proj_no_rel):
        r = bolt_gasket_list(str(proj_no_rel), {"limit": 0})
        note_text = " ".join(r["notes"])
        assert "P3dLineGroup" in note_text or "línea" in note_text.lower()

    def test_total_item_count_not_affected(self, proj_no_rel):
        r = bolt_gasket_list(str(proj_no_rel), {"limit": 0})
        assert r["totals"]["item_count"] == 3

    def test_spec_groupby_still_works_without_rel(self, proj_no_rel):
        r = bolt_gasket_list(str(proj_no_rel), {"limit": 0, "group_by": "spec"})
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 3


class TestDegradationFilterItemTypeWithMissingTable:
    """item_type=bolt con tabla BoltSet ausente → ok:True, totales 0, nota."""

    @pytest.fixture
    def proj_no_bolt_table(self, tmp_path: Path) -> Path:
        return _make_project(
            tmp_path, "NO_BOLT_TABLE",
            bolt_rows=[],
            gasket_rows=[(1, "SHOP")],
            ei_rows=[(1, "CS150", 2.0, "in", "GRAPHITE")],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100)],
            create_boltset=False,
        )

    def test_filter_bolt_with_no_bolt_table_ok(self, proj_no_bolt_table):
        r = bolt_gasket_list(
            str(proj_no_bolt_table), {"limit": 0, "item_type": "bolt"}
        )
        assert r["ok"] is True
        assert r["totals"]["item_count"] == 0

    def test_note_about_missing_table(self, proj_no_bolt_table):
        r = bolt_gasket_list(
            str(proj_no_bolt_table), {"limit": 0, "item_type": "bolt"}
        )
        note_text = " ".join(r["notes"]).lower()
        assert "boltset" in note_text or "bolt" in note_text


# ===========================================================================
# Parte 11 — Untagged: items sin Tag válido
# ===========================================================================


class TestUntagged:
    @pytest.fixture
    def proj_mixed_tags(self, tmp_path: Path) -> Path:
        """Proyecto con tags NULL, '', '?' y uno válido."""
        return _make_project(
            tmp_path, "MIXED_TAGS",
            bolt_rows=[
                (1, "SHOP",  "4", "M16"),  # untagged: sin relación
                (2, "FIELD", "8", "M16"),  # untagged: sin relación
                (3, "SHOP",  "4", "M16"),  # tagged: L-001
            ],
            gasket_rows=[],
            ei_rows=[
                (1, "CS150", 2.0, "in", "A193-B7"),
                (2, "CS150", 2.0, "in", "A193-B7"),
                (3, "CS150", 2.0, "in", "A193-B7"),
            ],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(3, 100)],  # solo PnPID 3 tiene relación
        )

    def test_untagged_count(self, proj_mixed_tags):
        r = bolt_gasket_list(str(proj_mixed_tags), {"limit": 0})
        assert r["untagged"]["item_count"] == 2

    def test_untagged_group_in_group_by_line(self, proj_mixed_tags):
        r = bolt_gasket_list(str(proj_mixed_tags), {"limit": 0, "group_by": "line"})
        groups = {g["group"]: g for g in r["groups"]}
        assert groups.get(_NO_LINE_LABEL, {}).get("item_count") == 2
        assert groups.get("L-001", {}).get("item_count") == 1

    def test_untagged_in_spec_group_by(self, proj_mixed_tags):
        """En group_by=spec, los untagged caen en su grupo natural."""
        r = bolt_gasket_list(str(proj_mixed_tags), {"limit": 0, "group_by": "spec"})
        groups = {g["group"]: g for g in r["groups"]}
        # Todas CS150
        assert groups.get("CS150", {}).get("item_count") == 3
        assert _NO_LINE_LABEL not in groups

    def test_total_includes_untagged(self, proj_mixed_tags):
        r = bolt_gasket_list(str(proj_mixed_tags), {"limit": 0})
        assert r["totals"]["item_count"] == 3

    def test_untagged_individual_bolts(self, proj_mixed_tags):
        # PnPIDs 1(4) + 2(8) = 12
        r = bolt_gasket_list(str(proj_mixed_tags), {"limit": 0})
        assert r["untagged"]["individual_bolts"] == 12


# ===========================================================================
# Parte 12 — individual_bolts expuesto como int
# ===========================================================================


class TestIndividualBoltsAsInt:
    def test_individual_bolts_int_type_totals(self, result_line):
        assert isinstance(result_line["totals"]["individual_bolts"], int)

    def test_individual_bolts_int_type_untagged(self, result_line):
        assert isinstance(result_line["untagged"]["individual_bolts"], int)

    def test_individual_bolts_int_type_groups(self, result_line):
        for g in result_line["groups"]:
            assert isinstance(g["individual_bolts"], int)

    def test_individual_bolts_int_type_by_item_type(self, result_line):
        for e in result_line["by_item_type"]:
            if "individual_bolts" in e:
                assert isinstance(e["individual_bolts"], int)

    def test_float_string_40_exposed_as_4(self, tmp_path):
        """NumberInSet='4.0' → individual_bolts=4 (int, no float)."""
        proj = _make_project(
            tmp_path, "FLOAT_STR",
            bolt_rows=[(1, "SHOP", "4.0", "M16")],
            gasket_rows=[],
            ei_rows=[(1, "CS150", 2.0, "in", "A193-B7")],
            line_group_rows=[(100, "L-001")],
            rel_rows=[(1, 100)],
        )
        r = bolt_gasket_list(str(proj), {"limit": 0})
        assert r["totals"]["individual_bolts"] == 4
        assert isinstance(r["totals"]["individual_bolts"], int)


# ===========================================================================
# Parte 13 — data no se muta
# ===========================================================================


class TestDataNotMutated:
    def test_data_not_mutated(self, proj):
        original = {"limit": 0, "group_by": "line", "spec": "CS150"}
        data_copy = dict(original)
        bolt_gasket_list(str(proj), data_copy)
        assert data_copy == original

    def test_none_data_ok(self, proj):
        r = bolt_gasket_list(str(proj), None)
        assert r["ok"] is True

    def test_empty_data_ok(self, proj):
        r = bolt_gasket_list(str(proj), {})
        assert r["ok"] is True


# ===========================================================================
# Parte 14 — Solo lectura: bytes del .dcf sin cambios
# ===========================================================================


class TestReadOnly:
    def test_dcf_bytes_unchanged(self, proj):
        dcf = proj / "Piping.dcf"
        before = dcf.read_bytes()
        bolt_gasket_list(str(proj), {"limit": 0})
        after = dcf.read_bytes()
        assert before == after


# ===========================================================================
# Parte 15 — Test de integración con proyecto real (skip si no accesible)
# ===========================================================================

_REAL_PROJECT = (
    r"\\172.16.0.220\Comun\06-INFORMÁTICA\3_UTILIDADES\MCP-Plant3D\Proyectos"
    r"\23099 - AIR LIQUIDE HUELVA"
)
_REAL_DCF = Path(_REAL_PROJECT) / "Piping.dcf"


def _real_dcf_accessible() -> bool:
    try:
        return _REAL_DCF.is_file()
    except (OSError, PermissionError):
        return False


@pytest.mark.skipif(
    not _real_dcf_accessible(),
    reason="Proyecto real AIR LIQUIDE HUELVA no accesible",
)
class TestIntegrationAirLiquideHuelva:
    """Integración contra el proyecto real; solo se ejecuta si la ruta es accesible."""

    @pytest.fixture(scope="class")
    def result_real(self) -> dict:
        return bolt_gasket_list(_REAL_PROJECT, {"limit": 0, "group_by": "line"})

    def test_ok_true(self, result_real):
        assert result_real["ok"] is True

    def test_bolt_sets_exact(self, result_real):
        """Se esperan exactamente 248 bolt sets."""
        assert result_real["totals"]["bolt_sets"] == 248

    def test_individual_bolts_exact(self, result_real):
        """Se esperan exactamente 1952 pernos individuales."""
        assert result_real["totals"]["individual_bolts"] == 1952

    def test_gaskets_exact(self, result_real):
        """Se esperan exactamente 262 juntas."""
        assert result_real["totals"]["gaskets"] == 262

    def test_item_count_exact(self, result_real):
        """Total de items = bolt_sets + gaskets = 510."""
        assert result_real["totals"]["item_count"] == 510

    def test_individual_bolts_is_int(self, result_real):
        assert isinstance(result_real["totals"]["individual_bolts"], int)

    def test_untagged_approx_20pct(self, result_real):
        """~20% de los items son untagged."""
        total = result_real["totals"]["item_count"]
        untagged = result_real["untagged"]["item_count"]
        if total > 0:
            pct = untagged / total * 100
            assert pct < 30, f"Porcentaje untagged alto: {pct:.1f}%"
            assert pct >= 5, f"Porcentaje untagged anormalmente bajo: {pct:.1f}%"

    def test_individual_bolts_sum_of_groups_matches_totals(self, result_real):
        """La suma de individual_bolts de los grupos cuadra con totals.individual_bolts.

        El dev redondea una vez por bucket, no fila a fila: debería ser exactamente 1952.
        """
        sum_groups = sum(g["individual_bolts"] for g in result_real["groups"])
        total_bolts = result_real["totals"]["individual_bolts"]
        assert sum_groups == total_bolts, (
            f"Suma de grupos ({sum_groups}) != totals.individual_bolts ({total_bolts})"
        )

    def test_dcf_unchanged(self):
        before = _REAL_DCF.read_bytes()
        bolt_gasket_list(_REAL_PROJECT, {"limit": 0})
        after = _REAL_DCF.read_bytes()
        assert before == after
