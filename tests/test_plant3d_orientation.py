"""Tests for the Plant 3D orientation queries — headless, no AutoCAD, no network.

Cubre:
  - list_drawings: clasificación de cada tipo (3d_model/spec_sheet/folder/
    isometric/ortho/pid), coherencia by_type/count, ProcessPower ausente,
    tabla PnPDrawings ausente (degrada), nombres no-ASCII tolerados.
  - project_info enriquecido: name/description/version/parts/units/
    spec_sheets_dir parseados de Project.xml; Metric vs Imperial; campos
    antiguos intactos; Project.xml mínimo / con nodos faltantes / con BOM no
    revientan.

Construye proyectos sintéticos en tmp_path con Project.xml y .dcf SQLite
reales, igual que los tests existentes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autocad_mcp.plant3d_query import list_drawings, project_info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# rows: (PnPID, "Dwg Name", PnPType, PnPRelativePath, Title, Author)
def _make_drawings_dcf(path: Path, rows: list[tuple], *, create_table: bool = True) -> None:
    con = sqlite3.connect(str(path))
    try:
        if create_table:
            con.execute(
                'CREATE TABLE PnPDrawings ('
                'PnPID INTEGER, "Dwg Name" TEXT, PnPType VARCHAR, '
                'PnPRelativePath TEXT, Title TEXT, Author TEXT, '
                'PnPParentGuid TEXT)'
            )
            for r in rows:
                con.execute(
                    'INSERT INTO PnPDrawings '
                    '(PnPID, "Dwg Name", PnPType, PnPRelativePath, Title, Author) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    r,
                )
        else:
            con.execute("CREATE TABLE SomethingElse (x INTEGER)")
        con.commit()
    finally:
        con.close()


_DEFAULT_XML = "<Project><ProjectName>P</ProjectName></Project>"


def _make_project(
    base: Path,
    name: str,
    *,
    piping_rows: list[tuple] | None = None,
    pid_rows: list[tuple] | None = None,
    project_xml: str | bytes = _DEFAULT_XML,
    piping_table: bool = True,
) -> Path:
    proj = base / name
    proj.mkdir()
    if isinstance(project_xml, bytes):
        (proj / "Project.xml").write_bytes(project_xml)
    else:
        (proj / "Project.xml").write_text(project_xml, encoding="utf-8")
    if piping_rows is not None or piping_table is False:
        _make_drawings_dcf(
            proj / "Piping.dcf", piping_rows or [], create_table=piping_table
        )
    if pid_rows is not None:
        _make_drawings_dcf(proj / "ProcessPower.dcf", pid_rows)
    return proj


# ---------------------------------------------------------------------------
# list_drawings
# ---------------------------------------------------------------------------


def test_list_drawings_classifies_every_type(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        piping_rows=[
            (1, "MOD-0001.dwg", "DWG", r"Plant 3D Models\MOD-0001.dwg", "Modelo", "ana"),
            (2, "SPEC-PUMP.pspx", "SPEC", r"SpecSheets\SPEC-PUMP.pspx", "Spec", "ana"),
            (3, "Pumps", "FOLDER", None, None, None),
            (4, "ISO-001.dwg", "DWG", r"Isometric\ISO-001.dwg", "Iso", "ana"),
            (5, "ORT-001.dwg", "DWG", r"Orthos\ORT-001.dwg", "Ortho", "ana"),
        ],
        pid_rows=[
            (10, "PID-001.dwg", "DWG", r"PID DWG\PID-001.dwg", "P&ID", "luis"),
        ],
    )
    res = list_drawings(str(proj))
    assert res["ok"] is True
    assert res["count"] == 6
    assert res["by_type"] == {
        "3d_model": 1,
        "spec_sheet": 1,
        "folder": 1,
        "isometric": 1,
        "ortho": 1,
        "pid": 1,
    }
    # by_type coherente con count
    assert sum(res["by_type"].values()) == res["count"]
    # Ordenado por type y luego name
    types = [d["type"] for d in res["drawings"]]
    assert types == sorted(types)


def test_list_drawings_spec_by_extension(tmp_path):
    # PnPType no es SPEC pero el nombre termina en .pspx → spec_sheet.
    proj = _make_project(
        tmp_path,
        "P1",
        piping_rows=[(1, "X.pspx", "DWG", r"SpecSheets\X.pspx", None, None)],
    )
    res = list_drawings(str(proj))
    assert res["by_type"] == {"spec_sheet": 1}


def test_list_drawings_pid_db_plain_dwg_is_pid(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        piping_rows=[],
        pid_rows=[(1, "PID.dwg", "DWG", r"PID DWG\PID.dwg", None, None)],
    )
    res = list_drawings(str(proj))
    assert res["by_type"] == {"pid": 1}


def test_list_drawings_no_processpower_only_piping(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        piping_rows=[(1, "MOD.dwg", "DWG", r"Plant 3D Models\MOD.dwg", None, None)],
    )
    assert not (proj / "ProcessPower.dcf").exists()
    res = list_drawings(str(proj))
    assert res["count"] == 1
    assert res["by_type"] == {"3d_model": 1}


def test_list_drawings_missing_table_degrades(tmp_path):
    proj = _make_project(tmp_path, "P1", piping_table=False)
    res = list_drawings(str(proj))
    assert res["ok"] is True
    assert res["count"] == 0
    assert "notes" in res
    assert any("PnPDrawings" in n for n in res["notes"])


def test_list_drawings_non_ascii_name_tolerated(tmp_path):
    # Inyecta un nombre con bytes windows-1252 (no UTF-8 válido) directamente.
    proj = _make_project(tmp_path, "P1", piping_rows=[])
    db = proj / "Piping.dcf"
    con = sqlite3.connect(str(db))
    try:
        # 0xF1 = 'ñ' en windows-1252, byte inválido como UTF-8 aislado.
        con.execute(
            'INSERT INTO PnPDrawings (PnPID, "Dwg Name", PnPType, PnPRelativePath) '
            "VALUES (1, CAST(? AS BLOB), 'DWG', 'Plant 3D Models\\x.dwg')",
            (b"Espa\xf1a.dwg",),
        )
        con.commit()
    finally:
        con.close()
    res = list_drawings(str(proj))  # no debe petar
    assert res["count"] == 1
    assert res["drawings"][0]["type"] == "3d_model"
    assert isinstance(res["drawings"][0]["name"], str)


def test_list_drawings_missing_optional_columns_still_lists(tmp_path):
    # Proyecto cuya tabla PnPDrawings carece de las columnas opcionales Title y
    # Author: solo PnPID, "Dwg Name", PnPType, PnPRelativePath. El listado debe
    # devolverse igualmente (no vacío), bien clasificado, con title/author None.
    proj = tmp_path / "P1"
    proj.mkdir()
    (proj / "Project.xml").write_text(_DEFAULT_XML, encoding="utf-8")
    db = proj / "Piping.dcf"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            'CREATE TABLE PnPDrawings ('
            'PnPID INTEGER, "Dwg Name" TEXT, PnPType VARCHAR, '
            'PnPRelativePath TEXT)'
        )
        con.execute(
            'INSERT INTO PnPDrawings (PnPID, "Dwg Name", PnPType, PnPRelativePath) '
            "VALUES (1, 'MOD.dwg', 'DWG', 'Plant 3D Models\\MOD.dwg')",
        )
        con.execute(
            'INSERT INTO PnPDrawings (PnPID, "Dwg Name", PnPType, PnPRelativePath) '
            "VALUES (2, 'ISO-001.dwg', 'DWG', 'Isometric\\ISO-001.dwg')",
        )
        con.commit()
    finally:
        con.close()

    res = list_drawings(str(proj))
    assert res["ok"] is True
    assert res["count"] == 2  # NO vacío pese a faltar Title/Author
    assert res["by_type"] == {"3d_model": 1, "isometric": 1}
    for d in res["drawings"]:
        assert d["title"] is None
        assert d["author"] is None
    # No degrada: sin notas de columnas inesperadas.
    assert not any(
        "columnas de PnPDrawings inesperadas" in n for n in res.get("notes", [])
    )


def test_list_drawings_read_only(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        piping_rows=[(1, "MOD.dwg", "DWG", r"Plant 3D Models\MOD.dwg", None, None)],
    )
    db = proj / "Piping.dcf"
    before = db.read_bytes()
    list_drawings(str(proj))
    assert db.read_bytes() == before


# ---------------------------------------------------------------------------
# project_info enriquecido
# ---------------------------------------------------------------------------


_FULL_XML = """<?xml version="1.0" encoding="utf-8"?>
<Project>
  <ProjectName>AIR LIQUIDE HUELVA</ProjectName>
  <ProjectDescription>Planta de hidrógeno</ProjectDescription>
  <ProjectVersion>2024.1</ProjectVersion>
  <ProjectParts>
    <ProjectPart name="PnId" relativeFileName="ProcessPower.dcf" />
    <ProjectPart name="Piping" relativeFileName="Metric_PipingPart.xml" />
    <ProjectPart name="Iso" relativeFileName="Metric_IsoPart.xml" />
  </ProjectParts>
  <ProjectPartDirectories>
    <ProjectPartDirectory name="SpecSheets" relativeDirectoryName="Spec Sheets" />
    <ProjectPartDirectory name="Ortho" relativeDirectoryName="Orthos" />
  </ProjectPartDirectories>
</Project>
"""


def test_project_info_enriched_full(tmp_path):
    proj = _make_project(
        tmp_path,
        "ProjFolder",
        piping_rows=[(1, "MOD.dwg", "DWG", r"Plant 3D Models\MOD.dwg", None, None)],
        pid_rows=[(2, "PID.dwg", "DWG", r"PID DWG\PID.dwg", None, None)],
        project_xml=_FULL_XML,
    )
    res = project_info(str(proj))
    # Campos antiguos intactos
    assert res["ok"] is True
    assert res["path"] == str(proj)
    assert res["has_piping"] is True
    assert res["has_pid"] is True
    # Nombre tomado de Project.xml
    assert res["name"] == "AIR LIQUIDE HUELVA"
    # Campos nuevos
    assert res["description"] == "Planta de hidrógeno"
    assert res["version"] == "2024.1"
    assert res["parts"] == ["PnId", "Piping", "Iso"]
    assert res["units"] == "Metric"
    assert res["spec_sheets_dir"] == "Spec Sheets"
    # drawing_counts = by_type de list_drawings
    assert res["drawing_counts"] == {"3d_model": 1, "pid": 1}


def test_project_info_units_imperial(tmp_path):
    xml = (
        "<Project><ProjectName>P</ProjectName><ProjectParts>"
        '<ProjectPart name="Piping" relativeFileName="Imperial_PipingPart.xml" />'
        "</ProjectParts></Project>"
    )
    proj = _make_project(tmp_path, "P", project_xml=xml)
    res = project_info(str(proj))
    assert res["units"] == "Imperial"


def test_project_info_minimal_xml_no_crash(tmp_path):
    proj = _make_project(tmp_path, "MyProj", project_xml="<Project/>")
    res = project_info(str(proj))
    assert res["ok"] is True
    # Sin ProjectName → cae al nombre de carpeta
    assert res["name"] == "MyProj"
    assert res["description"] is None
    assert res["version"] is None
    assert res["parts"] == []
    assert res["units"] is None
    assert res["spec_sheets_dir"] is None
    # Sin .dcf → drawing_counts vacío, sin reventar
    assert res["drawing_counts"] == {}


def test_project_info_version_minor_attribute(tmp_path):
    xml = (
        "<Project><ProjectName>P</ProjectName>"
        '<ProjectVersion Major="2025" Minor="3" /></Project>'
    )
    proj = _make_project(tmp_path, "P", project_xml=xml)
    res = project_info(str(proj))
    assert res["version"] == "2025.3"


def test_project_info_xml_with_bom(tmp_path):
    raw = ("﻿" + _FULL_XML).encode("utf-8")
    proj = _make_project(tmp_path, "P", project_xml=raw)
    res = project_info(str(proj))
    assert res["name"] == "AIR LIQUIDE HUELVA"
    assert res["units"] == "Metric"


def test_project_info_malformed_xml_degrades(tmp_path):
    proj = _make_project(tmp_path, "BrokenProj", project_xml="<Project><oops")
    res = project_info(str(proj))
    assert res["ok"] is True
    # Cae al nombre de carpeta y deja metadatos vacíos sin reventar.
    assert res["name"] == "BrokenProj"
    assert res["description"] is None
    assert res["parts"] == []
