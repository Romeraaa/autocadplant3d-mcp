"""Tests for plant3d_query.list_equipment — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with a real SQLite
Piping.dcf holding Equipment + PnPBase + Nozzle + AssetOwnership, then checks
the equipment listing, real class resolution, nozzle counts and graceful
degradation when a table is missing.

Datos reales de referencia (23099 AIR LIQUIDE HUELVA): Equipment tiene 15 filas
cuya clase real (PnPBase.PnPClassName) es Pump/MiscEquipment/HeatExchanger; la
relación equipo↔nozzle es vía AssetOwnership (Owner=equipo, Owned=nozzle).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autocad_mcp.plant3d_query import list_equipment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_piping_dcf(
    path: Path,
    *,
    equipment_rows: list[tuple] | None = None,
    base_rows: list[tuple] | None = None,
    nozzle_rows: list[int] | None = None,
    ownership_rows: list[tuple] | None = None,
    skip_equipment: bool = False,
    skip_assetownership: bool = False,
) -> None:
    """Crea un Piping.dcf mínimo para list_equipment.

    equipment_rows: (PnPID, Tag, Number, Type, Area)
    base_rows:      (PnPID, PnPClassName)
    nozzle_rows:    [PnPID, ...]
    ownership_rows: (Owner, Owned)
    """
    con = sqlite3.connect(str(path))
    try:
        if not skip_equipment:
            con.execute(
                "CREATE TABLE Equipment "
                "(PnPID INTEGER, Tag TEXT, Number TEXT, Type TEXT, Area TEXT)"
            )
            for row in equipment_rows or []:
                con.execute(
                    "INSERT INTO Equipment (PnPID, Tag, Number, Type, Area) "
                    "VALUES (?, ?, ?, ?, ?)",
                    row,
                )

        con.execute("CREATE TABLE PnPBase (PnPID INTEGER, PnPClassName TEXT)")
        for row in base_rows or []:
            con.execute(
                "INSERT INTO PnPBase (PnPID, PnPClassName) VALUES (?, ?)", row
            )

        con.execute("CREATE TABLE Nozzle (PnPID INTEGER, Type TEXT)")
        for pid in nozzle_rows or []:
            con.execute("INSERT INTO Nozzle (PnPID, Type) VALUES (?, 'N')", (pid,))

        if not skip_assetownership:
            con.execute(
                "CREATE TABLE AssetOwnership "
                "(PnPID INTEGER, Owner INTEGER, Owned INTEGER)"
            )
            for owner, owned in ownership_rows or []:
                con.execute(
                    "INSERT INTO AssetOwnership (Owner, Owned) VALUES (?, ?)",
                    (owner, owned),
                )
        con.commit()
    finally:
        con.close()


def _make_project(base: Path, name: str = "P1", **kw) -> Path:
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", **kw)
    return proj


def _typical_project(base: Path) -> Path:
    """Proyecto representativo: 3 equipos (Pump, MiscEquipment, HeatExchanger)."""
    return _make_project(
        base,
        equipment_rows=[
            (100, "P-62A", "62A", "P", "Z1"),
            (200, "UP-62A", "62A", "UP", ""),
            (300, "FO-7956", "7956", "FO", None),
        ],
        base_rows=[
            (100, "Pump"),
            (200, "MiscEquipment"),
            (300, "HeatExchanger"),
        ],
        nozzle_rows=[111, 112, 211],
        ownership_rows=[(100, 111), (100, 112), (200, 211)],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lists_all_equipment(tmp_path):
    proj = _typical_project(tmp_path)
    out = list_equipment(str(proj))
    assert out["ok"] is True
    assert out["count"] == 3
    tags = {e["tag"] for e in out["equipment"]}
    assert tags == {"P-62A", "UP-62A", "FO-7956"}


def test_real_class_from_pnpbase(tmp_path):
    proj = _typical_project(tmp_path)
    out = list_equipment(str(proj))
    by_pid = {e["pnpid"]: e for e in out["equipment"]}
    assert by_pid[100]["class"] == "Pump"
    assert by_pid[200]["class"] == "MiscEquipment"
    assert by_pid[300]["class"] == "HeatExchanger"


def test_nozzle_count_and_list(tmp_path):
    proj = _typical_project(tmp_path)
    out = list_equipment(str(proj))
    by_pid = {e["pnpid"]: e for e in out["equipment"]}
    assert by_pid[100]["nozzle_count"] == 2
    assert by_pid[100]["nozzles"] == [111, 112]
    assert by_pid[200]["nozzle_count"] == 1
    assert by_pid[200]["nozzles"] == [211]
    assert by_pid[300]["nozzle_count"] == 0
    assert by_pid[300]["nozzles"] == []


def test_by_class_breakdown(tmp_path):
    proj = _make_project(
        tmp_path,
        equipment_rows=[
            (1, "P-1", "1", "P", ""),
            (2, "P-2", "2", "P", ""),
            (3, "E-1", "1", "E", ""),
        ],
        base_rows=[(1, "Pump"), (2, "Pump"), (3, "HeatExchanger")],
        nozzle_rows=[],
        ownership_rows=[],
    )
    out = list_equipment(str(proj))
    assert out["by_class"] == {"Pump": 2, "HeatExchanger": 1}


def test_carries_type_number_area(tmp_path):
    proj = _typical_project(tmp_path)
    out = list_equipment(str(proj))
    e = next(e for e in out["equipment"] if e["pnpid"] == 100)
    assert e["type"] == "P"
    assert e["number"] == "62A"
    assert e["area"] == "Z1"


def test_missing_assetownership_degrades(tmp_path):
    proj = _make_project(
        tmp_path,
        equipment_rows=[(1, "P-1", "1", "P", "")],
        base_rows=[(1, "Pump")],
        nozzle_rows=[111],
        skip_assetownership=True,
    )
    out = list_equipment(str(proj))
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["equipment"][0]["nozzle_count"] == 0
    assert any("AssetOwnership" in n for n in out["notes"])


def test_missing_equipment_table(tmp_path):
    proj = _make_project(tmp_path, skip_equipment=True)
    out = list_equipment(str(proj))
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["equipment"] == []
    assert any("Equipment" in n for n in out["notes"])


def test_class_none_when_no_pnpbase_row(tmp_path):
    # Equipo sin fila en PnPBase → class None, agrupado como '(sin clase)'.
    proj = _make_project(
        tmp_path,
        equipment_rows=[(1, "P-1", "1", "P", "")],
        base_rows=[],
        nozzle_rows=[],
        ownership_rows=[],
    )
    out = list_equipment(str(proj))
    assert out["equipment"][0]["class"] is None
    assert out["by_class"] == {"(sin clase)": 1}


def test_read_only_does_not_modify_db(tmp_path):
    proj = _typical_project(tmp_path)
    db = proj / "Piping.dcf"
    before = db.read_bytes()
    list_equipment(str(proj))
    assert db.read_bytes() == before
