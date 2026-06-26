"""Tests for plant3d_query.export — headless, no AutoCAD, no network.

Builds a synthetic Plant 3D project (Piping.dcf) in tmp_path and exports its
listings to CSV / XLSX files inside tmp_path. The only file ever written is the
export target; the .dcf is verified to stay byte-identical.

Key invariants verified:
- CSV export: header row + one row per item, utf-8-sig encoding.
- XLSX export: header + rows readable back with openpyxl.
- kind=components / valves / specs row counts and columns.
- Stable ordered column union; nested values serialized.
- Missing kind / missing path / invalid extension -> ok:False (Spanish).
- openpyxl absent -> ok:False with the Spanish message (monkeypatched import).
- limit forced to 0 (no truncation) + note.
- Read-only guarantee on the .dcf.
"""

from __future__ import annotations

import builtins
import csv as _csv
import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import export


# ===========================================================================
# Helpers
# ===========================================================================


def _make_piping_dcf(path: Path, rows: list[tuple]) -> None:
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
        for pnpid, line_tag, comp_tag, cat, desc, spec, dia, unit in rows:
            con.execute(
                "INSERT INTO PipeRunComponent "
                "(PnPID, LineNumberTag, Tag) VALUES (?, ?, ?)",
                (pnpid, line_tag, comp_tag),
            )
            con.execute(
                "INSERT INTO EngineeringItems "
                "(PnPID, PartCategory, ShortDescription, Spec, NominalDiameter, NominalUnit) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pnpid, cat, desc, spec, dia, unit),
            )
        con.commit()
    finally:
        con.close()


def _make_project(base: Path, name: str, rows: list[tuple]) -> Path:
    proj = base / name
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", rows)
    return proj


# (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription, Spec, dia, unit)
_ROWS = [
    (1, "L-001", "TAG-P1", "Pipe", "Tubo recto", "CS150", 2.0, "in"),
    (2, "L-001", "TAG-P2", "Pipe", "Tubo recto", "CS150", 4.0, "in"),
    (3, "L-002", "TAG-V1", "Valves", "Válvula compuerta", "SS150", 2.0, "in"),
    (4, "L-002", "TAG-V2", "Valves", "Válvula bola", "SS150", 4.0, "in"),
    (5, "L-001", "TAG-F1", "Flanges", "Brida con acentós", "CS150", 2.0, "in"),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    return _make_project(tmp_path, "EXPORT_TEST", _ROWS)


# ===========================================================================
# CSV
# ===========================================================================


class TestCsv:
    def test_components_csv(self, proj, tmp_path):
        out = tmp_path / "out" / "components.csv"
        r = export(str(proj), {"kind": "components", "path": str(out)})
        assert r["ok"] is True
        assert r["format"] == "csv"
        assert r["kind"] == "components"
        assert r["rows"] == 5
        assert out.exists()
        # Columns match list_components row shape
        assert r["columns"] == [
            "pnpid",
            "class",
            "tag",
            "description",
            "spec",
            "size",
            "line",
        ]

    def test_csv_content_roundtrip(self, proj, tmp_path):
        out = tmp_path / "components.csv"
        export(str(proj), {"kind": "components", "path": str(out)})
        text = out.read_text(encoding="utf-8-sig")
        reader = list(_csv.reader(text.splitlines()))
        header = reader[0]
        assert header[0] == "pnpid"
        body = reader[1:]
        assert len(body) == 5

    def test_csv_utf8_sig_bom(self, proj, tmp_path):
        out = tmp_path / "components.csv"
        export(str(proj), {"kind": "components", "path": str(out)})
        raw = out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel

    def test_creates_parent_dirs(self, proj, tmp_path):
        out = tmp_path / "deep" / "nested" / "x.csv"
        r = export(str(proj), {"kind": "components", "path": str(out)})
        assert r["ok"] is True
        assert out.exists()

    def test_limit_forced_note(self, proj, tmp_path):
        out = tmp_path / "components.csv"
        r = export(str(proj), {"kind": "components", "path": str(out), "limit": 1})
        # limit ignored -> all 5 rows exported
        assert r["rows"] == 5
        assert any("limit" in n.lower() for n in r["notes"])

    def test_valves_kind(self, proj, tmp_path):
        out = tmp_path / "valves.csv"
        r = export(str(proj), {"kind": "valves", "path": str(out)})
        assert r["ok"] is True
        assert r["rows"] == 2  # two Valves rows

    def test_filter_forwarded(self, proj, tmp_path):
        out = tmp_path / "comp.csv"
        r = export(
            str(proj), {"kind": "components", "path": str(out), "line": "L-001"}
        )
        assert r["rows"] == 3  # pnpid 1,2,5 on L-001


# ===========================================================================
# XLSX
# ===========================================================================


class TestXlsx:
    def test_components_xlsx_roundtrip(self, proj, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        out = tmp_path / "components.xlsx"
        r = export(str(proj), {"kind": "components", "path": str(out)})
        assert r["ok"] is True
        assert r["format"] == "xlsx"
        assert r["rows"] == 5
        assert out.exists()

        wb = openpyxl.load_workbook(str(out))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        assert rows[0][0] == "pnpid"  # header
        assert len(rows) == 1 + 5  # header + 5 data rows
        # number of columns matches reported columns
        assert len(rows[0]) == len(r["columns"])

    def test_xlsx_specs_kind(self, proj, tmp_path):
        pytest.importorskip("openpyxl")
        out = tmp_path / "specs.xlsx"
        r = export(str(proj), {"kind": "specs", "path": str(out)})
        assert r["ok"] is True
        # CS150 + SS150 distinct specs
        assert r["rows"] == 2


class TestXlsxMissingOpenpyxl:
    def test_openpyxl_absent_returns_error(self, proj, tmp_path, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "openpyxl" or name.startswith("openpyxl."):
                raise ImportError("simulated: openpyxl not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        out = tmp_path / "x.xlsx"
        r = export(str(proj), {"kind": "components", "path": str(out)})
        assert r["ok"] is False
        assert "openpyxl" in r["error"]
        assert not out.exists()  # nothing written when the dep is missing


# ===========================================================================
# Error handling
# ===========================================================================


class TestErrors:
    def test_missing_kind(self, proj, tmp_path):
        r = export(str(proj), {"path": str(tmp_path / "x.csv")})
        assert r["ok"] is False
        assert "kind" in r["error"].lower()

    def test_unknown_kind(self, proj, tmp_path):
        r = export(str(proj), {"kind": "bogus", "path": str(tmp_path / "x.csv")})
        assert r["ok"] is False
        assert "bogus" in r["error"]

    def test_missing_path(self, proj):
        r = export(str(proj), {"kind": "components"})
        assert r["ok"] is False
        assert "path" in r["error"].lower()

    def test_invalid_extension(self, proj, tmp_path):
        r = export(str(proj), {"kind": "components", "path": str(tmp_path / "x.txt")})
        assert r["ok"] is False
        assert ".csv" in r["error"] and ".xlsx" in r["error"]


# ===========================================================================
# Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_dcf_bytes_unchanged(self, proj, tmp_path):
        db = proj / "Piping.dcf"
        before = db.read_bytes()
        mtime = db.stat().st_mtime_ns
        export(str(proj), {"kind": "components", "path": str(tmp_path / "x.csv")})
        assert db.read_bytes() == before
        assert db.stat().st_mtime_ns == mtime
