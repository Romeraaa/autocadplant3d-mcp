"""Tests for plant3d_query.resolve_handles — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) holding PnPDataLinks + PnPDrawings, then verifies the
PnPID → {dwg, handle} mapping that feeds the .NET plugin's handle-based locate.

Key invariants:
1.  Mapeo básico pnpid → {dwg, handle}.
2.  Combinación high/low en Int64: (high << 32) | (low & 0xFFFFFFFF).
3.  Multi-DWG: un pnpid con varias filas devuelve una entrada por (dwg, handle).
4.  Filas sin dwg o sin handle (low None) se omiten.
5.  Duplicados exactos se colapsan.
6.  Tabla ausente → [] (tolerante, no peta).
7.  Lista vacía de pnpids → [] sin tocar la BD.
8.  Solo lectura: la BD no se modifica.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autocad_mcp.plant3d_query import resolve_handles


# ---------------------------------------------------------------------------
# Helpers: construcción de un Piping.dcf mínimo con PnPDataLinks/PnPDrawings
# ---------------------------------------------------------------------------


def _make_piping_dcf(
    path: Path,
    datalinks_rows: list[tuple],
    drawings_rows: list[tuple],
    *,
    create_tables: bool = True,
) -> None:
    """Crea un Piping.dcf mínimo con PnPDataLinks y PnPDrawings.

    datalinks_rows: (RowId, DwgId, DwgHandleLow, DwgHandleHigh)
    drawings_rows:  (PnPID, "Dwg Name")
    """
    con = sqlite3.connect(str(path))
    try:
        if create_tables:
            con.execute(
                "CREATE TABLE PnPDataLinks "
                "(RowId INTEGER, DwgId INTEGER, "
                "DwgHandleLow INTEGER, DwgHandleHigh INTEGER, DwgSubIndex INTEGER)"
            )
            con.execute(
                'CREATE TABLE PnPDrawings (PnPID INTEGER, "Dwg Name" TEXT)'
            )
            for rowid, dwgid, low, high in datalinks_rows:
                con.execute(
                    "INSERT INTO PnPDataLinks "
                    "(RowId, DwgId, DwgHandleLow, DwgHandleHigh, DwgSubIndex) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (rowid, dwgid, low, high),
                )
            for pnpid, name in drawings_rows:
                con.execute(
                    'INSERT INTO PnPDrawings (PnPID, "Dwg Name") VALUES (?, ?)',
                    (pnpid, name),
                )
        else:
            # Un .dcf válido pero sin las tablas que resolve_handles espera.
            con.execute("CREATE TABLE SomethingElse (x INTEGER)")
        con.commit()
    finally:
        con.close()


def _make_project(
    base: Path,
    name: str,
    datalinks_rows: list[tuple],
    drawings_rows: list[tuple],
    **kw,
) -> Path:
    """Crea una carpeta de proyecto mínima con Project.xml + Piping.dcf."""
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", datalinks_rows, drawings_rows, **kw)
    return proj


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_basic_mapping(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(200171, 5, 9390, 0)],
        drawings_rows=[(5, "23099-PIP-MOD-0001_R9.dwg")],
    )
    out = resolve_handles(str(proj), [200171])
    assert out == [
        {"pnpid": 200171, "dwg": "23099-PIP-MOD-0001_R9.dwg", "handle": 9390}
    ]


def test_high_low_combined_int64(tmp_path):
    # high=2, low=0x10 → (2 << 32) | 0x10 = 8589934608
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 0x10, 2)],
        drawings_rows=[(5, "A.dwg")],
    )
    out = resolve_handles(str(proj), [1])
    assert out[0]["handle"] == (2 << 32) | 0x10


def test_low_full_32bits_masked(tmp_path):
    # low debe enmascararse a 32 bits sin signo.
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 0xFFFFFFFF, 0)],
        drawings_rows=[(5, "A.dwg")],
    )
    out = resolve_handles(str(proj), [1])
    assert out[0]["handle"] == 0xFFFFFFFF


def test_multi_dwg_one_entry_each(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(7, 5, 100, 0), (7, 6, 200, 0)],
        drawings_rows=[(5, "A.dwg"), (6, "B.dwg")],
    )
    out = resolve_handles(str(proj), [7])
    handles = sorted((e["dwg"], e["handle"]) for e in out)
    assert handles == [("A.dwg", 100), ("B.dwg", 200)]
    assert all(e["pnpid"] == 7 for e in out)


def test_row_without_dwg_name_skipped(tmp_path):
    # DwgId apunta a un drawing sin nombre → fila omitida.
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 100, 0)],
        drawings_rows=[(5, None)],
    )
    assert resolve_handles(str(proj), [1]) == []


def test_row_without_handle_skipped(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, None, 0)],
        drawings_rows=[(5, "A.dwg")],
    )
    assert resolve_handles(str(proj), [1]) == []


def test_exact_duplicates_collapsed(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 100, 0), (1, 5, 100, 0)],
        drawings_rows=[(5, "A.dwg")],
    )
    out = resolve_handles(str(proj), [1])
    assert out == [{"pnpid": 1, "dwg": "A.dwg", "handle": 100}]


def test_missing_tables_returns_empty(tmp_path):
    proj = _make_project(
        tmp_path, "P1", [], [], create_tables=False
    )
    assert resolve_handles(str(proj), [1, 2, 3]) == []


def test_empty_pnpids_returns_empty(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 100, 0)],
        drawings_rows=[(5, "A.dwg")],
    )
    assert resolve_handles(str(proj), []) == []


def test_only_requested_pnpids_returned(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 100, 0), (2, 5, 200, 0)],
        drawings_rows=[(5, "A.dwg")],
    )
    out = resolve_handles(str(proj), [2])
    assert out == [{"pnpid": 2, "dwg": "A.dwg", "handle": 200}]


def test_read_only_does_not_modify_db(tmp_path):
    proj = _make_project(
        tmp_path,
        "P1",
        datalinks_rows=[(1, 5, 100, 0)],
        drawings_rows=[(5, "A.dwg")],
    )
    db = proj / "Piping.dcf"
    before = db.read_bytes()
    resolve_handles(str(proj), [1])
    assert db.read_bytes() == before
