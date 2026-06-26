"""Tests for plant3d_query.list_specs and spec_contents — headless.

No AutoCAD, no network. Builds synthetic Plant 3D project folders in tmp_path
with real SQLite databases (Piping.dcf and .pspc catalogues) and exercises both
read-only operations against them. No real project databases are ever touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import list_specs, spec_contents


# ===========================================================================
# Helpers: build minimal SQLite databases
# ===========================================================================


def _make_piping_dcf(path: Path, specs: list[str | None]) -> None:
    """Create a Piping.dcf SQLite with one EngineeringItems row per spec value.

    ``specs`` is a list of Spec values (may include None/'' to exercise the
    NULL/empty filter). PnPID is auto-assigned.
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE EngineeringItems ("
            "PnPID INTEGER, "
            "Spec TEXT"
            ")"
        )
        for i, spec in enumerate(specs, start=1):
            con.execute(
                "INSERT INTO EngineeringItems (PnPID, Spec) VALUES (?, ?)",
                (i, spec),
            )
        con.commit()
    finally:
        con.close()


def _make_piping_dcf_no_eng(path: Path) -> None:
    """Create a Piping.dcf marked as SQLite but WITHOUT EngineeringItems."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE Other (x INTEGER)")
        con.commit()
    finally:
        con.close()


def _make_pspc(path: Path, rows: list[dict]) -> None:
    """Create a minimal .pspc SQLite catalogue with component rows.

    Each row dict may carry: PartCategory, ShortDescription, NominalDiameter,
    NominalUnit, Schedule, Material, EndType, PressureClass, ItemCode.
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE RepositoryDescriptor (Name TEXT)")
        con.execute("INSERT INTO RepositoryDescriptor VALUES ('test-catalogue')")
        con.execute(
            "CREATE TABLE EngineeringItems ("
            "PnPID INTEGER, "
            "PartCategory TEXT, "
            "ShortDescription TEXT, "
            "NominalDiameter REAL, "
            "NominalUnit TEXT, "
            "Schedule TEXT, "
            "Material TEXT, "
            "EndType TEXT, "
            "PressureClass TEXT, "
            "ItemCode TEXT"
            ")"
        )
        for i, r in enumerate(rows, start=1):
            con.execute(
                "INSERT INTO EngineeringItems "
                "(PnPID, PartCategory, ShortDescription, NominalDiameter, "
                "NominalUnit, Schedule, Material, EndType, PressureClass, ItemCode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    i,
                    r.get("PartCategory"),
                    r.get("ShortDescription"),
                    r.get("NominalDiameter"),
                    r.get("NominalUnit"),
                    r.get("Schedule"),
                    r.get("Material"),
                    r.get("EndType"),
                    r.get("PressureClass"),
                    r.get("ItemCode"),
                ),
            )
        con.commit()
    finally:
        con.close()


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def full_project(tmp_path: Path) -> Path:
    """Project where:
    - CS150 is used (2 comps) AND has a .pspc.
    - SS300 is used (1 comp) but has NO .pspc (ghost / used-without-pspc).
    - A045 has a .pspc but is NOT used in the model (catalogue-only).
    - empty/NULL specs are ignored in the usage count.
    """
    proj = tmp_path / "FULL_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(
        proj / "Piping.dcf",
        ["CS150", "cs150 ", "SS300", "", None],
    )
    sheets = proj / "Spec Sheets"
    sheets.mkdir()
    _make_pspc(
        sheets / "CS150.pspc",
        [
            {"PartCategory": "Pipe", "ShortDescription": "TUBO", "NominalDiameter": 2.0,
             "NominalUnit": "in", "Schedule": "STD", "Material": "ASTM A106 Gr B",
             "EndType": "BW", "PressureClass": None, "ItemCode": "P-001"},
            {"PartCategory": "Flanges", "ShortDescription": "BRIDA WN", "NominalDiameter": 2.0,
             "NominalUnit": "in", "Schedule": None, "Material": "ASTM A105",
             "EndType": "RF", "PressureClass": "150", "ItemCode": "F-001"},
        ],
    )
    _make_pspc(
        sheets / "A045.pspc",
        [
            {"PartCategory": "Valves", "ShortDescription": "VALVULA BOLA", "NominalDiameter": 1.0,
             "NominalUnit": "in", "Schedule": None, "Material": "CF8M",
             "EndType": "RF", "PressureClass": "300", "ItemCode": "V-001"},
        ],
    )
    return proj


# ===========================================================================
# list_specs
# ===========================================================================


class TestListSpecs:
    def test_ok_flag(self, full_project):
        r = list_specs(str(full_project))
        assert r["ok"] is True

    def test_count_includes_used_and_catalogue(self, full_project):
        # CS150 + SS300 + A045 -> 3 distinct specs.
        r = list_specs(str(full_project))
        assert r["count"] == 3
        names = {s["spec"] for s in r["specs"]}
        assert names == {"CS150", "SS300", "A045"}

    def test_used_count_merges_case_and_space(self, full_project):
        r = list_specs(str(full_project))
        cs150 = next(s for s in r["specs"] if s["spec"] == "CS150")
        # "CS150" + "cs150 " merged -> 2 components.
        assert cs150["used"] == 2
        assert cs150["has_pspc"] is True

    def test_used_without_pspc(self, full_project):
        r = list_specs(str(full_project))
        ss300 = next(s for s in r["specs"] if s["spec"] == "SS300")
        assert ss300["used"] == 1
        assert ss300["has_pspc"] is False

    def test_pspc_without_use(self, full_project):
        r = list_specs(str(full_project))
        a045 = next(s for s in r["specs"] if s["spec"] == "A045")
        assert a045["used"] == 0
        assert a045["has_pspc"] is True

    def test_empty_and_null_specs_not_counted(self, full_project):
        # Only CS150 (2) + SS300 (1) used; empty/NULL excluded entirely.
        r = list_specs(str(full_project))
        total_used = sum(s["used"] for s in r["specs"])
        assert total_used == 3

    def test_no_spec_sheets_degrades(self, tmp_path):
        proj = tmp_path / "NO_SHEETS"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        _make_piping_dcf(proj / "Piping.dcf", ["CS150", "CS150"])
        r = list_specs(str(proj))
        assert r["ok"] is True
        # Used spec still listed, just without catalogue.
        cs150 = next(s for s in r["specs"] if s["spec"] == "CS150")
        assert cs150["used"] == 2
        assert cs150["has_pspc"] is False
        assert any("Spec Sheets" in n for n in r["notes"])

    def test_no_engineering_items_degrades(self, tmp_path):
        proj = tmp_path / "NO_ENG"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        _make_piping_dcf_no_eng(proj / "Piping.dcf")
        sheets = proj / "Spec Sheets"
        sheets.mkdir()
        _make_pspc(sheets / "CS150.pspc", [])
        r = list_specs(str(proj))
        assert r["ok"] is True
        cs150 = next(s for s in r["specs"] if s["spec"] == "CS150")
        assert cs150["used"] == 0
        assert cs150["has_pspc"] is True
        assert any("EngineeringItems" in n for n in r["notes"])


# ===========================================================================
# spec_contents
# ===========================================================================


class TestSpecContents:
    def test_lists_components(self, full_project):
        r = spec_contents(str(full_project), {"spec": "CS150"})
        assert r["ok"] is True
        assert r["spec"] == "CS150"
        assert r["count"] == 2
        assert len(r["components"]) == 2

    def test_component_fields(self, full_project):
        r = spec_contents(str(full_project), {"spec": "CS150"})
        pipe = next(c for c in r["components"] if c["class"] == "Pipe")
        assert pipe["description"] == "TUBO"
        assert pipe["size"] == '2"'
        assert pipe["schedule"] == "STD"
        assert pipe["material"] == "ASTM A106 Gr B"
        assert pipe["end_type"] == "BW"
        assert pipe["item_code"] == "P-001"

    def test_path_pspc_reported(self, full_project):
        r = spec_contents(str(full_project), {"spec": "CS150"})
        assert r["path_pspc"].endswith("CS150.pspc")

    def test_case_insensitive_match(self, full_project):
        r = spec_contents(str(full_project), {"spec": "cs150"})
        assert r["ok"] is True
        # Display name uses the catalogue stem casing.
        assert r["spec"] == "CS150"

    def test_spec_without_pspc(self, full_project):
        # SS300 is used but has no .pspc.
        r = spec_contents(str(full_project), {"spec": "SS300"})
        assert r["ok"] is False
        assert "SS300" in r["message"]

    def test_missing_spec_param(self, full_project):
        r = spec_contents(str(full_project), {})
        assert r["ok"] is False
        assert "spec" in r["message"].lower()

    def test_no_spec_sheets_folder(self, tmp_path):
        proj = tmp_path / "NO_SHEETS2"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        _make_piping_dcf(proj / "Piping.dcf", ["CS150"])
        r = spec_contents(str(proj), {"spec": "CS150"})
        assert r["ok"] is False
        assert "Spec Sheets" in r["message"]

    def test_limit_caps_and_reports_omitted(self, full_project):
        r = spec_contents(str(full_project), {"spec": "CS150", "limit": 1})
        assert r["ok"] is True
        assert r["count"] == 2          # total reported faithfully
        assert len(r["components"]) == 1  # only 1 shown
        assert any("omitidos" in n for n in r["notes"])

    def test_limit_zero_no_cap(self, full_project):
        r = spec_contents(str(full_project), {"spec": "CS150", "limit": 0})
        assert len(r["components"]) == 2
        assert r["notes"] == []
