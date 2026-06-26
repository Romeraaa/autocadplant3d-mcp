"""Tests for plant3d_query.find_missing_properties — headless, no AutoCAD.

Builds synthetic Plant 3D project folders in tmp_path with a real SQLite
Piping.dcf and exercises find_missing_properties against them. No real project
databases are ever touched.

Key invariants verified:
- Default per-class profile (pipe/valve/fitting/flange/instrument/other).
- Blank detection per field (None / "" / whitespace; tag placeholders; size "?").
- required override as a flat list (applies to every class).
- required override as a dict (replaces only the named classes).
- Unknown field name in override -> dropped with a note.
- Scope filters (line) forwarded to list_components.
- limit/omitted over the flagged components.
- Output structure (top-level keys, by_class, profile echo).
- Read-only guarantee on the .dcf.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import find_missing_properties


# ===========================================================================
# Helpers (mirror tests/test_plant3d_list_components.py)
# ===========================================================================


def _make_piping_dcf_full(path: Path, rows: list[tuple]) -> None:
    """Create a Piping.dcf with PipeRunComponent + EngineeringItems.

    rows: (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription,
           Spec, NominalDiameter, NominalUnit)
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
    _make_piping_dcf_full(proj / "Piping.dcf", rows)
    return proj


# (PnPID, LineNumberTag, Tag, PartCategory, ShortDescription, Spec, dia, unit)
_ROWS = [
    # 1 complete pipe -> no missing
    (1, "L-001", "TAG-P1", "Pipe", "Tubo", "CS150", 2.0, "in"),
    # 2 pipe missing spec
    (2, "L-001", "TAG-P2", "Pipe", "Tubo", None, 2.0, "in"),
    # 3 pipe missing line and size (no dia) and spec ("" blank)
    (3, None, "TAG-P3", "Pipe", "Tubo", "  ", None, None),
    # 4 complete valve -> no missing (valve requires tag too)
    (4, "L-001", "TAG-V1", "Valves", "Valvula", "CS150", 2.0, "in"),
    # 5 valve missing tag (placeholder '?-?') -> flagged for tag
    (5, "L-002", "?-?", "Valves", "Valvula", "SS150", 4.0, "in"),
    # 6 instrument missing line; spec/size NOT required for instrument
    (6, None, "TAG-I1", "Instruments", "Manometro", None, None, None),
    # 7 instrument complete (tag+line) even though no spec/size
    (7, "L-003", "TAG-I2", "Instruments", "Transmisor", None, None, None),
    # 8 flange complete -> no missing
    (8, "L-001", "TAG-FL1", "Flanges", "Brida", "CS150", 2.0, "in"),
    # 9 unknown class (Miscellaneous) missing spec -> uses 'other' default
    (9, "L-001", "TAG-M1", "Miscellaneous", "Misc", None, 4.0, "in"),
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    return _make_project(tmp_path, "FMP_TEST", _ROWS)


# ===========================================================================
# Default profile
# ===========================================================================


class TestDefaultProfile:
    def test_profile_echo(self, proj):
        r = find_missing_properties(str(proj), {"limit": 0})
        assert r["ok"] is True
        prof = r["profile"]
        assert prof["pipe"] == ["spec", "size", "line"]
        assert prof["valve"] == ["spec", "size", "line", "tag"]
        assert prof["instrument"] == ["tag", "line"]
        assert prof["other"] == ["spec", "size", "line"]

    def test_flags_only_components_with_missing(self, proj):
        r = find_missing_properties(str(proj), {"limit": 0})
        flagged = {c["pnpid"]: c for c in r["components"]}
        # Complete ones absent
        assert 1 not in flagged  # complete pipe
        assert 4 not in flagged  # complete valve
        assert 7 not in flagged  # complete instrument
        assert 8 not in flagged  # complete flange
        # Flagged ones present
        assert 2 in flagged
        assert 3 in flagged
        assert 5 in flagged
        assert 6 in flagged
        assert 9 in flagged
        assert r["count"] == 5

    def test_missing_fields_per_component(self, proj):
        r = find_missing_properties(str(proj), {"limit": 0})
        by_pnpid = {c["pnpid"]: c["missing"] for c in r["components"]}
        assert by_pnpid[2] == ["spec"]
        # pnpid 3: spec blank ("  "), size None -> "?", line None
        assert set(by_pnpid[3]) == {"spec", "size", "line"}
        # valve missing tag (placeholder)
        assert by_pnpid[5] == ["tag"]
        # instrument requires [tag, line]; only line missing
        assert by_pnpid[6] == ["line"]
        # unknown class -> other default [spec,size,line]; only spec missing
        assert by_pnpid[9] == ["spec"]

    def test_by_class_present(self, proj):
        r = find_missing_properties(str(proj), {"limit": 0})
        names = {b["class"] for b in r["by_class"]}
        assert "Pipe" in names
        assert "Valves" in names
        total = sum(b["count"] for b in r["by_class"])
        assert total == r["count"]


# ===========================================================================
# required override
# ===========================================================================


class TestRequiredOverrideList:
    def test_flat_list_applies_to_all_classes(self, proj):
        # Only require 'spec' for every class -> instruments 6 & 7 have no spec
        r = find_missing_properties(
            str(proj), {"required": ["spec"], "limit": 0}
        )
        # Every class profile becomes ["spec"]
        assert r["profile"]["pipe"] == ["spec"]
        assert r["profile"]["instrument"] == ["spec"]
        assert r["profile"]["other"] == ["spec"]
        by_pnpid = {c["pnpid"]: c["missing"] for c in r["components"]}
        # instruments 6 & 7 lack spec now -> flagged
        assert by_pnpid[6] == ["spec"]
        assert by_pnpid[7] == ["spec"]
        # valve 5 has spec -> NOT flagged anymore (tag no longer required)
        assert 5 not in by_pnpid


class TestRequiredOverrideDict:
    def test_dict_replaces_only_named_classes(self, proj):
        # Require only 'tag' for pipe; other classes keep their default.
        r = find_missing_properties(
            str(proj), {"required": {"pipe": ["tag"]}, "limit": 0}
        )
        assert r["profile"]["pipe"] == ["tag"]
        # valve keeps its default
        assert r["profile"]["valve"] == ["spec", "size", "line", "tag"]
        by_pnpid = {c["pnpid"]: c["missing"] for c in r["components"]}
        # pipes 1,2,3 all have tags -> NOT flagged under pipe=[tag]
        assert 1 not in by_pnpid
        assert 2 not in by_pnpid
        assert 3 not in by_pnpid
        # valve 5 still flagged for tag (default unchanged)
        assert by_pnpid[5] == ["tag"]


class TestUnknownField:
    def test_unknown_field_dropped_with_note(self, proj):
        r = find_missing_properties(
            str(proj), {"required": ["spec", "color"], "limit": 0}
        )
        assert r["profile"]["pipe"] == ["spec"]
        assert any("color" in n and "desconocido" in n for n in r["notes"])

    def test_unknown_field_in_dict_note(self, proj):
        r = find_missing_properties(
            str(proj), {"required": {"valve": ["weight"]}, "limit": 0}
        )
        assert r["profile"]["valve"] == []
        assert any("weight" in n and "valve" in n for n in r["notes"])


# ===========================================================================
# Filters + limit
# ===========================================================================


class TestFilters:
    def test_line_filter_forwarded(self, proj):
        r = find_missing_properties(str(proj), {"line": "L-001", "limit": 0})
        assert r["filters"].get("line") == "L-001"
        for c in r["components"]:
            assert c["line"] is None or c["line"] == "L-001"
        # pnpid 3 (line None) and 5/6 (other lines) excluded
        pnpids = {c["pnpid"] for c in r["components"]}
        assert pnpids == {2, 9}

    def test_classes_filter_forwarded(self, proj):
        r = find_missing_properties(
            str(proj), {"classes": ["valve"], "limit": 0}
        )
        for c in r["components"]:
            assert c["class"] == "Valves"
        assert {c["pnpid"] for c in r["components"]} == {5}


class TestLimit:
    def test_limit_caps_and_reports_omitted(self, proj):
        r = find_missing_properties(str(proj), {"limit": 2})
        assert len(r["components"]) == 2
        assert r["count"] == 5
        assert r["omitted"] == 3

    def test_limit_zero_no_cap(self, proj):
        r = find_missing_properties(str(proj), {"limit": 0})
        assert r["omitted"] == 0
        assert len(r["components"]) == r["count"]


# ===========================================================================
# Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_db_bytes_unchanged(self, proj):
        db = proj / "Piping.dcf"
        before = db.read_bytes()
        mtime = db.stat().st_mtime_ns
        find_missing_properties(str(proj), {"limit": 0})
        assert db.read_bytes() == before
        assert db.stat().st_mtime_ns == mtime
