"""Tests del filtro por DWG de list_components — headless, sin AutoCAD ni red.

Construye proyectos Plant 3D sintéticos en tmp_path con SQLite real:
- PipeRunComponent + EngineeringItems (inventario de componentes).
- PnPDataLinks + PnPDrawings (en qué DWG vive físicamente cada PnPID).

Cubre:
1.  pnpids_in_dwg: mapeo por basename (case-insensitive y con ruta completa),
    multi-DWG, DWG inexistente, y tabla ausente → set vacío.
2.  list_components con filtro 'dwg': devuelve SOLO los componentes de ese DWG
    (incluidos los de LineNumberTag '?'), by_class coherente, combinable con
    el filtro de clase.
3.  Dispatch en server: active_dwg:true / dwg:"@active" traducen DWGNAME del
    backend mockeado a data["dwg"]; error claro fuera de file_ipc.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.plant3d_query import list_components, pnpids_in_dwg


# ---------------------------------------------------------------------------
# Helpers: construir un Piping.dcf con inventario + vínculos a DWG
# ---------------------------------------------------------------------------


def _make_piping_dcf(
    path: Path,
    component_rows: list[tuple],
    datalinks_rows: list[tuple],
    drawings_rows: list[tuple],
    *,
    include_link_tables: bool = True,
) -> None:
    """Crea un Piping.dcf con inventario y, opcionalmente, vínculos a DWG.

    component_rows: (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription,
                     Spec, NominalDiameter, NominalUnit)
    datalinks_rows: (RowId, DwgId)
    drawings_rows:  (PnPID, "Dwg Name")
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
        for pnpid, line_tag, comp_tag, cat, desc, spec, dia, unit in component_rows:
            con.execute(
                "INSERT INTO PipeRunComponent (PnPID, LineNumberTag, Tag) "
                "VALUES (?, ?, ?)",
                (pnpid, line_tag, comp_tag),
            )
            con.execute(
                "INSERT INTO EngineeringItems "
                "(PnPID, PartCategory, ShortDescription, Spec, NominalDiameter, "
                "NominalUnit) VALUES (?, ?, ?, ?, ?, ?)",
                (pnpid, cat, desc, spec, dia, unit),
            )
        if include_link_tables:
            con.execute(
                "CREATE TABLE PnPDataLinks (RowId INTEGER, DwgId INTEGER)"
            )
            con.execute('CREATE TABLE PnPDrawings (PnPID INTEGER, "Dwg Name" TEXT)')
            for rowid, dwgid in datalinks_rows:
                con.execute(
                    "INSERT INTO PnPDataLinks (RowId, DwgId) VALUES (?, ?)",
                    (rowid, dwgid),
                )
            for pnpid, name in drawings_rows:
                con.execute(
                    'INSERT INTO PnPDrawings (PnPID, "Dwg Name") VALUES (?, ?)',
                    (pnpid, name),
                )
        con.commit()
    finally:
        con.close()


def _make_project(
    base: Path,
    name: str,
    component_rows: list[tuple],
    datalinks_rows: list[tuple],
    drawings_rows: list[tuple],
    **kw,
) -> Path:
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(
        proj / "Piping.dcf", component_rows, datalinks_rows, drawings_rows, **kw
    )
    return proj


# Inventario base: 3 componentes en DWG-A (uno SIN etiquetar, '?'), 1 en DWG-B.
# (PnPID, LineNumberTag, Tag, PartCategory, desc, Spec, dia, unit)
_COMPONENTS = [
    (1, "L-001", "V-1", "Valves", "Válvula compuerta", "CS150", 2.0, "in"),
    (2, "L-001", "P-1", "Pipe", "Tubo recto", "CS150", 2.0, "in"),
    (3, "?", "?", "Valves", "Válvula sin etiquetar", "CS150", 2.0, "in"),
    (4, "L-002", "P-2", "Pipe", "Tubo recto B", "CS150", 4.0, "in"),
]
# Vínculos a DWG: PnPID 1,2,3 → DWG-A (dwgid 5); PnPID 4 → DWG-B (dwgid 6).
_DATALINKS = [(1, 5), (2, 5), (3, 5), (4, 6)]
_DRAWINGS = [(5, "23099-PIP-MOD-0001_R9.dwg"), (6, "23099-PIP-MOD-0002_R1.dwg")]


def _project(tmp_path, **kw) -> Path:
    return _make_project(
        tmp_path, "P1", _COMPONENTS, _DATALINKS, _DRAWINGS, **kw
    )


# ===========================================================================
# pnpids_in_dwg
# ===========================================================================


def test_pnpids_in_dwg_basename(tmp_path):
    proj = _project(tmp_path)
    assert pnpids_in_dwg(str(proj), "23099-PIP-MOD-0001_R9.dwg") == {1, 2, 3}
    assert pnpids_in_dwg(str(proj), "23099-PIP-MOD-0002_R1.dwg") == {4}


def test_pnpids_in_dwg_case_insensitive(tmp_path):
    proj = _project(tmp_path)
    assert pnpids_in_dwg(str(proj), "23099-pip-mod-0001_r9.DWG") == {1, 2, 3}


def test_pnpids_in_dwg_full_path_reduced_to_basename(tmp_path):
    proj = _project(tmp_path)
    full = r"C:\Plant3D\Models\23099-PIP-MOD-0001_R9.dwg"
    assert pnpids_in_dwg(str(proj), full) == {1, 2, 3}


def test_pnpids_in_dwg_unknown_dwg_empty(tmp_path):
    proj = _project(tmp_path)
    assert pnpids_in_dwg(str(proj), "NO-EXISTE.dwg") == set()


def test_pnpids_in_dwg_blank_name_empty(tmp_path):
    proj = _project(tmp_path)
    assert pnpids_in_dwg(str(proj), "") == set()
    assert pnpids_in_dwg(str(proj), "   ") == set()


def test_pnpids_in_dwg_missing_tables_empty(tmp_path):
    proj = _project(tmp_path, include_link_tables=False)
    assert pnpids_in_dwg(str(proj), "23099-PIP-MOD-0001_R9.dwg") == set()


def test_pnpids_in_dwg_read_only(tmp_path):
    proj = _project(tmp_path)
    db = proj / "Piping.dcf"
    before = db.read_bytes()
    pnpids_in_dwg(str(proj), "23099-PIP-MOD-0001_R9.dwg")
    assert db.read_bytes() == before


# ===========================================================================
# list_components con filtro 'dwg'
# ===========================================================================


def test_list_components_dwg_filter_only_that_drawing(tmp_path):
    proj = _project(tmp_path)
    out = list_components(str(proj), {"dwg": "23099-PIP-MOD-0001_R9.dwg"})
    pnpids = {c["pnpid"] for c in out["components"]}
    assert pnpids == {1, 2, 3}  # incluye el PnPID 3 (LineNumberTag '?')
    assert out["count"] == 3
    assert out["filters"]["dwg"] == "23099-PIP-MOD-0001_R9.dwg"


def test_list_components_dwg_filter_includes_untagged(tmp_path):
    proj = _project(tmp_path)
    out = list_components(str(proj), {"dwg": "23099-PIP-MOD-0001_R9.dwg"})
    untagged = [c for c in out["components"] if c["pnpid"] == 3]
    assert len(untagged) == 1
    # El componente sin línea aparece con line=None (saneado), pero está.
    assert untagged[0]["line"] is None


def test_list_components_dwg_by_class_coherent(tmp_path):
    proj = _project(tmp_path)
    out = list_components(str(proj), {"dwg": "23099-PIP-MOD-0001_R9.dwg"})
    by_class = {row["class"]: row["count"] for row in out["by_class"]}
    assert by_class == {"Valves": 2, "Pipe": 1}
    assert sum(row["count"] for row in out["by_class"]) == out["count"]


def test_list_components_dwg_combined_with_class(tmp_path):
    proj = _project(tmp_path)
    out = list_components(
        str(proj),
        {"dwg": "23099-PIP-MOD-0001_R9.dwg", "classes": ["valve"]},
    )
    pnpids = {c["pnpid"] for c in out["components"]}
    assert pnpids == {1, 3}  # válvulas del DWG-A (etiquetada + sin etiquetar)
    assert out["filters"]["dwg"] == "23099-PIP-MOD-0001_R9.dwg"
    assert out["filters"]["classes"] == ["valve"]


def test_list_components_dwg_full_path_accepted(tmp_path):
    proj = _project(tmp_path)
    full = r"C:\Models\23099-PIP-MOD-0001_R9.dwg"
    out = list_components(str(proj), {"dwg": full})
    assert {c["pnpid"] for c in out["components"]} == {1, 2, 3}
    # El echo se normaliza a basename.
    assert out["filters"]["dwg"] == "23099-PIP-MOD-0001_R9.dwg"


def test_list_components_dwg_unknown_empty_with_note(tmp_path):
    proj = _project(tmp_path)
    out = list_components(str(proj), {"dwg": "NO-EXISTE.dwg"})
    assert out["count"] == 0
    assert out["components"] == []
    assert any("NO-EXISTE.dwg" in n for n in out["notes"])


def test_list_components_no_dwg_filter_shows_everything(tmp_path):
    proj = _project(tmp_path)
    out = list_components(str(proj), {})
    assert out["count"] == 4
    assert "dwg" not in out["filters"]


# ===========================================================================
# Dispatch del server: active_dwg / "@active" → DWGNAME del backend
# ===========================================================================


class _FakeBackend:
    def __init__(self, name: str, dwgname: str | None = "23099-PIP-MOD-0001_R9.dwg"):
        self.name = name
        self._dwgname = dwgname

    async def drawing_get_variables(self, names):
        return CommandResult(ok=True, payload={"DWGNAME": self._dwgname})


def _patch_server(backend, captured: dict):
    """Parchea server.get_backend, _detect_open_project y list_components.

    list_components se reemplaza por un stub que captura el 'data' recibido
    para verificar que active_dwg/"@active" se tradujeron a data["dwg"].
    """
    import contextlib

    from autocad_mcp import plant3d_query, server

    async def fake_get_backend():
        return backend

    async def fake_detect():
        return "FAKE_PROJECT"

    def fake_list_components(project, data):
        captured["project"] = project
        captured["data"] = dict(data)
        return {"ok": True, "components": [], "count": 0, "filters": {}}

    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(server, "get_backend", fake_get_backend))
    stack.enter_context(patch.object(server, "_detect_open_project", fake_detect))
    stack.enter_context(
        patch.object(plant3d_query, "list_components", fake_list_components)
    )
    return stack


@pytest.mark.asyncio
async def test_dispatch_active_dwg_true_reads_dwgname():
    from autocad_mcp import server

    captured: dict = {}
    backend = _FakeBackend("file_ipc")
    with _patch_server(backend, captured):
        out = await server.plant3d(
            operation="list_components",
            data={"project": "P", "active_dwg": True},
        )
    json.loads(out)
    assert captured["data"]["dwg"] == "23099-PIP-MOD-0001_R9.dwg"


@pytest.mark.asyncio
async def test_dispatch_dwg_at_active_reads_dwgname():
    from autocad_mcp import server

    captured: dict = {}
    backend = _FakeBackend("file_ipc")
    with _patch_server(backend, captured):
        await server.plant3d(
            operation="list_components",
            data={"project": "P", "dwg": "@active"},
        )
    assert captured["data"]["dwg"] == "23099-PIP-MOD-0001_R9.dwg"


@pytest.mark.asyncio
async def test_dispatch_explicit_dwg_no_backend_call():
    """Un 'dwg' explícito no debe tocar AutoCAD (backend ezdxf vale)."""
    from autocad_mcp import server

    captured: dict = {}
    backend = _FakeBackend("ezdxf")
    with _patch_server(backend, captured):
        await server.plant3d(
            operation="list_components",
            data={"project": "P", "dwg": "X.dwg"},
        )
    assert captured["data"]["dwg"] == "X.dwg"


@pytest.mark.asyncio
async def test_dispatch_active_dwg_non_file_ipc_clear_error():
    from autocad_mcp import server

    captured: dict = {}
    backend = _FakeBackend("ezdxf")
    with _patch_server(backend, captured):
        out = await server.plant3d(
            operation="list_components",
            data={"project": "P", "active_dwg": True},
        )
    parsed = json.loads(out)
    # RuntimeError → _safe → _error: contiene 'error' en español, sin tocar SQLite.
    assert "AutoCAD" in parsed["error"]
    assert "data" not in captured  # list_components no llegó a ejecutarse
