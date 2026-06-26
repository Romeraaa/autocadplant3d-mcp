"""Tests for plant3d_query.get_component — headless, no AutoCAD, no network.

Builds a synthetic Piping.dcf with PnPBase + a class table (Valve/Equipment) +
EngineeringItems + PnPDataLinks/PnPDrawings + PnPTagRegistry, then verifies the
full property dump by pnpid, the resolved class, merged engineering props, the
dwg/handle resolution, lookup by tag and the not-found path.

Invariantes verificados con datos reales (23099 AIR LIQUIDE HUELVA):
- PnPBase.PnPClassName da la clase del objeto.
- La fila de la tabla de clase + EngineeringItems aportan las propiedades.
- PnPDataLinks.RowId = PnPID del objeto; DwgId→PnPDrawings.PnPID; handle = hex.
- PnPTagRegistry.RowId = PnPID del objeto etiquetado.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autocad_mcp.plant3d_query import get_component


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_piping_dcf(path: Path) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE PnPBase "
            "(PnPID INTEGER, PnPClassName TEXT, PnPGuid TEXT, PnPTimestamp TEXT)"
        )
        con.executemany(
            "INSERT INTO PnPBase (PnPID, PnPClassName, PnPGuid, PnPTimestamp) "
            "VALUES (?, ?, ?, ?)",
            [
                (2783, "Valve", "{guid-1}", "ts1"),
                (100, "Equipment", "{guid-2}", "ts2"),
            ],
        )

        con.execute(
            "CREATE TABLE Valve "
            "(PnPID INTEGER, Tag TEXT, PartSubType TEXT, PnPGuid TEXT)"
        )
        con.execute(
            "INSERT INTO Valve (PnPID, Tag, PartSubType, PnPGuid) "
            "VALUES (2783, 'HV-001', 'GateValve', '{guid-1}')"
        )

        con.execute(
            "CREATE TABLE Equipment "
            "(PnPID INTEGER, Tag TEXT, Number TEXT, Type TEXT, Area TEXT)"
        )
        con.execute(
            "INSERT INTO Equipment (PnPID, Tag, Number, Type, Area) "
            "VALUES (100, 'P-62A', '62A', 'P', 'Z1')"
        )

        con.execute(
            "CREATE TABLE EngineeringItems "
            "(PnPID INTEGER, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT, "
            "Material TEXT)"
        )
        con.execute(
            "INSERT INTO EngineeringItems "
            "(PnPID, Spec, NominalDiameter, NominalUnit, Material) "
            "VALUES (2783, 'CS150', 50.0, 'mm', 'A105')"
        )

        con.execute(
            "CREATE TABLE PnPDataLinks "
            "(RowId INTEGER, DwgId INTEGER, DwgHandleLow INTEGER, "
            "DwgHandleHigh INTEGER)"
        )
        con.execute(
            "INSERT INTO PnPDataLinks (RowId, DwgId, DwgHandleLow, DwgHandleHigh) "
            "VALUES (2783, 5, 1019, 0)"
        )

        con.execute('CREATE TABLE PnPDrawings (PnPID INTEGER, "Dwg Name" TEXT)')
        con.execute(
            'INSERT INTO PnPDrawings (PnPID, "Dwg Name") VALUES (5, "Aire.dwg")'
        )

        con.execute(
            "CREATE TABLE PnPTagRegistry (Tag TEXT, RowId INTEGER, PnPID INTEGER)"
        )
        con.executemany(
            "INSERT INTO PnPTagRegistry (Tag, RowId, PnPID) VALUES (?, ?, ?)",
            [
                ("HV-001", 2783, 2784),
                ("DUP", 100, 101),
                ("DUP", 2783, 2785),
            ],
        )
        con.commit()
    finally:
        con.close()


def _make_project(base: Path, name: str = "P1") -> Path:
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf")
    return proj


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dump_by_pnpid_class_and_props(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"pnpid": 2783})
    assert out["ok"] is True
    assert out["pnpid"] == 2783
    assert out["class"] == "Valve"
    props = out["properties"]
    # Propiedades de la tabla de clase + EngineeringItems fusionadas.
    assert props["Tag"] == "HV-001"
    assert props["PartSubType"] == "GateValve"
    assert props["Spec"] == "CS150"
    assert props["NominalDiameter"] == 50.0
    assert props["Material"] == "A105"


def test_internal_columns_omitted(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"pnpid": 2783})
    assert "PnPGuid" not in out["properties"]
    assert "PnPID" not in out["properties"]


def test_dwg_and_handle_resolved(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"pnpid": 2783})
    assert out["dwgs"] == [{"dwg": "Aire.dwg", "handle": 1019}]


def test_equipment_dump(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"pnpid": 100})
    assert out["ok"] is True
    assert out["class"] == "Equipment"
    assert out["properties"]["Tag"] == "P-62A"
    assert out["properties"]["Area"] == "Z1"
    # Sin datalink → dwgs vacío, con nota.
    assert out["dwgs"] == []
    assert any("PnPDataLinks" in n for n in out["notes"])


def test_lookup_by_tag(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"tag": "HV-001"})
    assert out["ok"] is True
    assert out["pnpid"] == 2783  # RowId del tag registry
    assert out["class"] == "Valve"


def test_ambiguous_tag_returns_first_with_note(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"tag": "DUP"})
    assert out["ok"] is True
    assert out["pnpid"] in (100, 2783)
    assert any("resuelve a 2" in n for n in out["notes"])


def test_tag_not_found(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"tag": "NOPE"})
    assert out["ok"] is False
    assert any("NOPE" in n for n in out["notes"])


def test_pnpid_not_found(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"pnpid": 999999})
    assert out["ok"] is False
    assert out["pnpid"] == 999999
    assert any("No existe" in n for n in out["notes"])


def test_missing_identifier(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {})
    assert out["ok"] is False
    assert any("pnpid" in n for n in out["notes"])


def test_non_integer_pnpid(tmp_path):
    proj = _make_project(tmp_path)
    out = get_component(str(proj), {"pnpid": "abc"})
    assert out["ok"] is False
    assert any("entero" in n for n in out["notes"])


def test_read_only_does_not_modify_db(tmp_path):
    proj = _make_project(tmp_path)
    db = proj / "Piping.dcf"
    before = db.read_bytes()
    get_component(str(proj), {"pnpid": 2783})
    assert db.read_bytes() == before
