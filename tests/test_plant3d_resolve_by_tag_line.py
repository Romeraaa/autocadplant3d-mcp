"""Tests for plant3d_query.pnpids_for_tag / pnpids_for_line — headless.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf) holding PnPTagRegistry / PipeRunComponent (and an
Equipment fallback table), then verifies the tag/line → PnPID resolution that
feeds plant3d.locate.

Invariantes clave:
- Match normalizado TRIM + UPPER en ambos lados.
- Varios objetos con el mismo tag/línea → lista de PnPIDs sin duplicados.
- Tag/línea inexistente → [].
- Tabla/columna ausente → [] (tolerante).
- Fallback a Equipment.Tag si no existe PnPTagRegistry.
- Solo lectura: la BD no se modifica.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autocad_mcp.plant3d_query import pnpids_for_line, pnpids_for_tag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_piping_dcf(
    path: Path,
    *,
    tag_rows: list[tuple] | None = None,      # (RowId, Tag)
    comp_rows: list[tuple] | None = None,     # (PnPID, LineNumberTag)
    equip_rows: list[tuple] | None = None,    # (PnPID, Tag)
    with_tag_registry: bool = True,
    with_pipe_components: bool = True,
    with_equipment: bool = False,
) -> None:
    con = sqlite3.connect(str(path))
    try:
        if with_tag_registry:
            con.execute(
                "CREATE TABLE PnPTagRegistry (RowId INTEGER, Tag TEXT)"
            )
            for rowid, tag in (tag_rows or []):
                con.execute(
                    "INSERT INTO PnPTagRegistry (RowId, Tag) VALUES (?, ?)",
                    (rowid, tag),
                )
        if with_pipe_components:
            con.execute(
                "CREATE TABLE PipeRunComponent (PnPID INTEGER, LineNumberTag TEXT)"
            )
            for pnpid, line in (comp_rows or []):
                con.execute(
                    "INSERT INTO PipeRunComponent (PnPID, LineNumberTag) "
                    "VALUES (?, ?)",
                    (pnpid, line),
                )
        if with_equipment:
            con.execute("CREATE TABLE Equipment (PnPID INTEGER, Tag TEXT)")
            for pnpid, tag in (equip_rows or []):
                con.execute(
                    "INSERT INTO Equipment (PnPID, Tag) VALUES (?, ?)",
                    (pnpid, tag),
                )
        if not (with_tag_registry or with_pipe_components or with_equipment):
            con.execute("CREATE TABLE SomethingElse (x INTEGER)")
        con.commit()
    finally:
        con.close()


def _make_project(base: Path, name: str = "P1", **kw) -> Path:
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", **kw)
    return proj


# ---------------------------------------------------------------------------
# pnpids_for_tag
# ---------------------------------------------------------------------------


def test_tag_basic_match(tmp_path):
    proj = _make_project(tmp_path, tag_rows=[(200171, "1001-PG-001")])
    assert pnpids_for_tag(str(proj), "1001-PG-001") == [200171]


def test_tag_normalized_case_and_spaces(tmp_path):
    proj = _make_project(tmp_path, tag_rows=[(42, "1001-PG-001")])
    # Distinto case y con espacios alrededor → debe casar igual.
    assert pnpids_for_tag(str(proj), "  1001-pg-001 ") == [42]


def test_tag_several_objects_same_tag(tmp_path):
    proj = _make_project(
        tmp_path,
        tag_rows=[(1, "V-01"), (2, "V-01"), (3, "OTRO")],
    )
    out = pnpids_for_tag(str(proj), "V-01")
    assert sorted(out) == [1, 2]


def test_tag_duplicates_collapsed(tmp_path):
    proj = _make_project(
        tmp_path,
        tag_rows=[(7, "T-1"), (7, "T-1")],
    )
    assert pnpids_for_tag(str(proj), "T-1") == [7]


def test_tag_not_found_returns_empty(tmp_path):
    proj = _make_project(tmp_path, tag_rows=[(1, "A")])
    assert pnpids_for_tag(str(proj), "NO-EXISTE") == []


def test_tag_blank_returns_empty_without_db(tmp_path):
    proj = _make_project(tmp_path, tag_rows=[(1, "A")])
    assert pnpids_for_tag(str(proj), "   ") == []


def test_tag_table_absent_returns_empty(tmp_path):
    # Sin PnPTagRegistry ni Equipment → degrada a [].
    proj = _make_project(
        tmp_path, with_tag_registry=False, with_pipe_components=False
    )
    assert pnpids_for_tag(str(proj), "X") == []


def test_tag_fallback_to_equipment(tmp_path):
    # Sin PnPTagRegistry pero con Equipment.Tag → usa el fallback.
    proj = _make_project(
        tmp_path,
        with_tag_registry=False,
        with_equipment=True,
        equip_rows=[(500, "P-101"), (501, "P-102")],
    )
    assert pnpids_for_tag(str(proj), "p-101") == [500]


# ---------------------------------------------------------------------------
# pnpids_for_line
# ---------------------------------------------------------------------------


def test_line_components(tmp_path):
    proj = _make_project(
        tmp_path,
        comp_rows=[
            (10, "1001-PG-001"),
            (11, "1001-PG-001"),
            (12, "2002-PG-002"),
        ],
    )
    out = pnpids_for_line(str(proj), "1001-PG-001")
    assert sorted(out) == [10, 11]


def test_line_normalized(tmp_path):
    proj = _make_project(tmp_path, comp_rows=[(10, "1001-PG-001")])
    assert pnpids_for_line(str(proj), "  1001-pg-001 ") == [10]


def test_line_duplicates_collapsed(tmp_path):
    proj = _make_project(
        tmp_path, comp_rows=[(10, "L-1"), (10, "L-1")]
    )
    assert pnpids_for_line(str(proj), "L-1") == [10]


def test_line_not_found_returns_empty(tmp_path):
    proj = _make_project(tmp_path, comp_rows=[(10, "L-1")])
    assert pnpids_for_line(str(proj), "L-9") == []


def test_line_table_absent_returns_empty(tmp_path):
    proj = _make_project(
        tmp_path, with_tag_registry=False, with_pipe_components=False
    )
    assert pnpids_for_line(str(proj), "L-1") == []


def test_line_blank_returns_empty(tmp_path):
    proj = _make_project(tmp_path, comp_rows=[(10, "L-1")])
    assert pnpids_for_line(str(proj), "") == []


# ---------------------------------------------------------------------------
# Solo lectura
# ---------------------------------------------------------------------------


def test_read_only_does_not_modify_db(tmp_path):
    proj = _make_project(
        tmp_path,
        tag_rows=[(1, "A")],
        comp_rows=[(1, "L-1")],
    )
    db = proj / "Piping.dcf"
    before = db.read_bytes()
    pnpids_for_tag(str(proj), "A")
    pnpids_for_line(str(proj), "L-1")
    assert db.read_bytes() == before
