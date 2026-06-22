"""Tests for plant3d_query.validate_specs — headless, no AutoCAD, no network.

Builds synthetic Plant 3D project folders in tmp_path with real SQLite
databases (Piping.dcf and .pspc catalogues) and exercises validate_specs
and its helpers against them. No real project databases are ever touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import (
    _capped,
    _norm,
    _norm_tight,
    _read_spec_catalogue,
    _spec_sheet_stems,
    validate_specs,
)


# ===========================================================================
# Part 1 – Pure helpers
# ===========================================================================


class TestNorm:
    def test_strips_leading_trailing_spaces(self):
        assert _norm("  CS150  ") == "CS150"

    def test_uppercases(self):
        assert _norm("cs150") == "CS150"

    def test_strips_and_uppercases(self):
        assert _norm("  cs150  ") == "CS150"

    def test_none_returns_empty_string(self):
        assert _norm(None) == ""

    def test_empty_returns_empty_string(self):
        assert _norm("") == ""

    def test_internal_spaces_preserved(self):
        # _norm only strips leading/trailing; internal spaces stay.
        assert _norm("CS 150 A") == "CS 150 A"


class TestNormTight:
    def test_removes_all_whitespace(self):
        assert _norm_tight("ASTM A403 GrWP304") == "ASTMA403GRWP304"

    def test_uppercases(self):
        assert _norm_tight("astm a403") == "ASTMA403"

    def test_none_returns_empty_string(self):
        assert _norm_tight(None) == ""

    def test_internal_tabs_removed(self):
        assert _norm_tight("A\t403") == "A403"

    def test_empty_returns_empty_string(self):
        assert _norm_tight("") == ""


class TestCapped:
    def test_no_cap_when_limit_zero(self):
        items = list(range(10))
        result, omitted = _capped(items, 0)
        assert result == items
        assert omitted == 0

    def test_no_cap_when_limit_negative(self):
        items = list(range(10))
        result, omitted = _capped(items, -1)
        assert result == items
        assert omitted == 0

    def test_caps_to_limit(self):
        items = list(range(10))
        result, omitted = _capped(items, 3)
        assert result == [0, 1, 2]
        assert omitted == 7

    def test_no_omission_when_items_fit(self):
        items = [1, 2, 3]
        result, omitted = _capped(items, 5)
        assert result == items
        assert omitted == 0

    def test_omitted_equals_excess(self):
        items = list(range(20))
        _, omitted = _capped(items, 15)
        assert omitted == 5

    def test_empty_list(self):
        result, omitted = _capped([], 5)
        assert result == []
        assert omitted == 0


# ===========================================================================
# Helpers: build minimal SQLite databases
# ===========================================================================


def _make_piping_dcf(path: Path, rows: list[tuple]) -> None:
    """Create a Piping.dcf SQLite with the minimum Plant 3D schema.

    Each row: (PnPID, LineNumberTag, required_spec, Spec, PartCategory,
               ShortDescription, Schedule, Material, NominalDiameter, NominalUnit)
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE PipeRunComponent ("
            "PnPID INTEGER, "
            "LineNumberTag TEXT, "
            '"Required Spec" TEXT'
            ")"
        )
        con.execute(
            "CREATE TABLE EngineeringItems ("
            "PnPID INTEGER, "
            "Spec TEXT, "
            "PartCategory TEXT, "
            "ShortDescription TEXT, "
            "Schedule TEXT, "
            "Material TEXT, "
            "NominalDiameter REAL, "
            "NominalUnit TEXT"
            ")"
        )
        for row in rows:
            (
                pnpid, line_tag, req_spec, spec, category,
                desc, schedule, material, dia, unit,
            ) = row
            con.execute(
                "INSERT INTO PipeRunComponent (PnPID, LineNumberTag, \"Required Spec\") "
                "VALUES (?, ?, ?)",
                (pnpid, line_tag, req_spec),
            )
            con.execute(
                "INSERT INTO EngineeringItems "
                "(PnPID, Spec, PartCategory, ShortDescription, Schedule, Material, "
                "NominalDiameter, NominalUnit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pnpid, spec, category, desc, schedule, material, dia, unit),
            )
        con.commit()
    finally:
        con.close()


def _make_pspc(path: Path, rows: list[tuple]) -> None:
    """Create a minimal .pspc SQLite catalogue.

    Each row: (Schedule, Material).
    Also creates RepositoryDescriptor table (Plant 3D marker).
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE RepositoryDescriptor (Name TEXT)")
        con.execute("INSERT INTO RepositoryDescriptor VALUES ('test-catalogue')")
        con.execute(
            "CREATE TABLE EngineeringItems ("
            "Schedule TEXT, "
            "Material TEXT"
            ")"
        )
        for schedule, material in rows:
            con.execute(
                "INSERT INTO EngineeringItems (Schedule, Material) VALUES (?, ?)",
                (schedule, material),
            )
        con.commit()
    finally:
        con.close()


# ===========================================================================
# Part 2 – Check 1: mismatched_spec
# ===========================================================================

# (PnPID, LineTag, RequiredSpec, ActualSpec, Category, Desc, Schedule, Material, Dia, Unit)
_MISMATCH_ROWS = [
    # Row 1: actual spec matches required (exactly) -> no violation
    (1, "L-001", "CS150", "CS150", "Pipe", "Tubo", "STD", "ASTM A106", 2.0, "in"),
    # Row 2: actual spec matches required (differs only in case/spaces) -> no violation
    (2, "L-001", "CS150", "cs150 ", "Pipe", "Tubo", "STD", "ASTM A106", 2.0, "in"),
    # Row 3: actual spec differs from required -> violation
    (3, "L-002", "CS150", "SS150", "Pipe", "Tubo", "STD", "ASTM A312", 2.0, "in"),
    # Row 4: required spec empty -> skip (no required spec declared)
    (4, "L-003", "", "CS150", "Pipe", "Tubo", "STD", "ASTM A106", 2.0, "in"),
    # Row 5: actual spec empty -> skip (nothing to compare)
    (5, "L-003", "CS150", "", "Pipe", "Tubo", "STD", "ASTM A106", 2.0, "in"),
]


@pytest.fixture
def mismatch_project(tmp_path: Path) -> Path:
    proj = tmp_path / "MISMATCH_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", _MISMATCH_ROWS)
    return proj


class TestMismatchedSpec:
    def test_count_exact(self, mismatch_project):
        r = validate_specs(str(mismatch_project))
        assert r["mismatched_spec"]["count"] == 1

    def test_correct_pnpid_flagged(self, mismatch_project):
        r = validate_specs(str(mismatch_project))
        examples = r["mismatched_spec"]["examples"]
        assert len(examples) == 1
        assert examples[0]["pnpid"] == 3

    def test_case_and_space_match_not_flagged(self, mismatch_project):
        # Row 2 ("cs150 " vs "CS150") must NOT appear.
        r = validate_specs(str(mismatch_project))
        pnpids = [e["pnpid"] for e in r["mismatched_spec"]["examples"]]
        assert 2 not in pnpids

    def test_payload_contains_required_and_actual(self, mismatch_project):
        r = validate_specs(str(mismatch_project))
        ex = r["mismatched_spec"]["examples"][0]
        assert ex["required_spec"] == "CS150"
        assert ex["actual_spec"] == "SS150"

    def test_ok_flag(self, mismatch_project):
        r = validate_specs(str(mismatch_project))
        assert r["ok"] is True


# ===========================================================================
# Part 3 – Check 2: mixed_specs
# ===========================================================================

# Line L-001: has CS150 + "PlaceHolder Metric" (auxiliary) -> NOT mixed (aux excluded)
# Line L-002: has CS150 + SS150 (both real specs) -> IS mixed
# Line L-003: has only CS150 -> clean
_MIXED_ROWS = [
    (10, "L-001", "CS150", "CS150", "Pipe", "T1", "STD", "ASTM A106", 2.0, "in"),
    (11, "L-001", "CS150", "PlaceHolder Metric", "Pipe", "T2", "STD", "ASTM A106", 2.0, "in"),
    (12, "L-002", "CS150", "CS150", "Pipe", "T3", "STD", "ASTM A106", 2.0, "in"),
    (13, "L-002", "SS150", "SS150", "Pipe", "T4", "STD", "ASTM A312", 2.0, "in"),
    (14, "L-003", "CS150", "CS150", "Pipe", "T5", "STD", "ASTM A106", 2.0, "in"),
]


@pytest.fixture
def mixed_project(tmp_path: Path) -> Path:
    proj = tmp_path / "MIXED_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", _MIXED_ROWS)
    return proj


class TestMixedSpecs:
    def test_line_with_only_auxiliary_not_flagged(self, mixed_project):
        # L-001 has CS150 + PlaceHolder Metric; after excluding auxiliary, only CS150 remains.
        r = validate_specs(str(mixed_project))
        flagged_lines = [e["line"] for e in r["mixed_specs"]["examples"]]
        assert "L-001" not in flagged_lines

    def test_line_with_two_real_specs_flagged(self, mixed_project):
        # L-002 has CS150 + SS150 -> must appear.
        r = validate_specs(str(mixed_project))
        flagged_lines = [e["line"] for e in r["mixed_specs"]["examples"]]
        assert "L-002" in flagged_lines

    def test_clean_line_not_flagged(self, mixed_project):
        r = validate_specs(str(mixed_project))
        flagged_lines = [e["line"] for e in r["mixed_specs"]["examples"]]
        assert "L-003" not in flagged_lines

    def test_count_exact(self, mixed_project):
        r = validate_specs(str(mixed_project))
        assert r["mixed_specs"]["count"] == 1

    def test_mixed_line_payload(self, mixed_project):
        r = validate_specs(str(mixed_project))
        ex = r["mixed_specs"]["examples"][0]
        assert ex["line"] == "L-002"
        assert ex["n_specs"] == 2
        # specs ya sale ordenada alfabéticamente y normalizada (TRIM+UPPER)
        assert ex["specs"] == ["CS150", "SS150"]
        # spec_components (renombrada desde "components") cuenta los componentes
        # de la línea con spec no auxiliar y no vacía
        assert ex["spec_components"] == 2


# ===========================================================================
# Part 3b – Check 2: normalización de specs (colapso casing/espacios)
# ===========================================================================

# Datos: L-001 tiene "cs150" y " CS150 " (misma spec normalizada) -> NO es mezcla
# L-002 tiene "CS150" y "SS150" (specs distintas) -> SÍ es mezcla
_NORM_ROWS = [
    (70, "L-001", "CS150", "cs150",   "Pipe", "T1", "STD", "ASTM A106", 2.0, "in"),
    (71, "L-001", "CS150", " CS150 ", "Pipe", "T2", "STD", "ASTM A106", 2.0, "in"),
    (72, "L-002", "CS150", "CS150",   "Pipe", "T3", "STD", "ASTM A106", 2.0, "in"),
    (73, "L-002", "SS150", "SS150",   "Pipe", "T4", "STD", "ASTM A312", 2.0, "in"),
]


@pytest.fixture
def norm_project(tmp_path: Path) -> Path:
    proj = tmp_path / "NORM_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", _NORM_ROWS)
    return proj


class TestMixedSpecsNormalisation:
    def test_case_variants_same_spec_not_mixed(self, norm_project):
        # "cs150" y " CS150 " son la misma spec normalizada: L-001 NO debe aparecer.
        r = validate_specs(str(norm_project))
        flagged = [e["line"] for e in r["mixed_specs"]["examples"]]
        assert "L-001" not in flagged

    def test_count_only_real_mix(self, norm_project):
        # Solo L-002 (CS150 + SS150) es mezcla real.
        r = validate_specs(str(norm_project))
        assert r["mixed_specs"]["count"] == 1

    def test_specs_list_is_uppercase(self, norm_project):
        # La lista de specs en el payload debe estar en mayúsculas (normalizada).
        r = validate_specs(str(norm_project))
        ex = r["mixed_specs"]["examples"][0]
        for s in ex["specs"]:
            assert s == s.upper(), f"spec '{s}' no está en mayúsculas"

    def test_specs_list_is_sorted_alphabetically(self, norm_project):
        # La lista de specs debe estar ordenada alfabéticamente.
        r = validate_specs(str(norm_project))
        ex = r["mixed_specs"]["examples"][0]
        assert ex["specs"] == sorted(ex["specs"])

    def test_specs_normalized_values(self, norm_project):
        # L-002: specs normalizada son exactamente ["CS150", "SS150"].
        r = validate_specs(str(norm_project))
        ex = r["mixed_specs"]["examples"][0]
        assert ex["line"] == "L-002"
        assert ex["specs"] == ["CS150", "SS150"]

    def test_n_specs_counts_normalized_distinct(self, norm_project):
        # L-001 queda con 1 spec normalizada -> n_specs=1 (línea no aparece en mixed).
        # L-002 tiene 2 specs normalizadas distintas -> n_specs=2.
        r = validate_specs(str(norm_project))
        ex = r["mixed_specs"]["examples"][0]
        assert ex["n_specs"] == 2

    def test_spec_components_key_present(self, norm_project):
        # La clave "spec_components" (renombrada desde "components") debe existir.
        r = validate_specs(str(norm_project))
        ex = r["mixed_specs"]["examples"][0]
        assert "spec_components" in ex
        assert "components" not in ex  # la clave antigua NO debe existir


# ===========================================================================
# Part 4 – Check 3a: empty_spec
# ===========================================================================

_EMPTY_SPEC_ROWS = [
    (20, "L-001", "CS150", "CS150", "Pipe", "T1", "STD", "ASTM A106", 2.0, "in"),  # ok
    (21, "L-002", None, None,  "Pipe", "T2", None, None, 2.0, "in"),               # NULL spec
    (22, "L-003", None, "",    "Pipe", "T3", None, None, 2.0, "in"),               # empty string
    (23, "L-004", None, "  ", "Pipe", "T4", None, None, 2.0, "in"),               # whitespace only
]


@pytest.fixture
def empty_spec_project(tmp_path: Path) -> Path:
    proj = tmp_path / "EMPTYSPEC_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", _EMPTY_SPEC_ROWS)
    return proj


class TestEmptySpec:
    def test_count_exact(self, empty_spec_project):
        r = validate_specs(str(empty_spec_project))
        assert r["empty_spec"]["count"] == 3

    def test_pnpids_flagged(self, empty_spec_project):
        r = validate_specs(str(empty_spec_project))
        pnpids = {e["pnpid"] for e in r["empty_spec"]["examples"]}
        assert pnpids == {21, 22, 23}

    def test_valid_spec_not_flagged(self, empty_spec_project):
        r = validate_specs(str(empty_spec_project))
        pnpids = {e["pnpid"] for e in r["empty_spec"]["examples"]}
        assert 20 not in pnpids


# ===========================================================================
# Part 5 – Check 3b: ghost_specs
# ===========================================================================


@pytest.fixture
def ghost_project(tmp_path: Path) -> Path:
    """Project with Spec Sheets folder; CS150 has .pspc, SS150 does not."""
    proj = tmp_path / "GHOST_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")

    rows = [
        # CS150 -> has .pspc (not a ghost)
        (30, "L-001", "CS150", "CS150", "Pipe", "T1", "STD", "ASTM A106", 2.0, "in"),
        # SS150 -> no .pspc (ghost)
        (31, "L-002", "SS150", "SS150", "Pipe", "T2", "STD", "ASTM A312", 2.0, "in"),
        # PlaceHolder Metric -> auxiliary, excluded from ghost check
        (32, "L-001", "CS150", "PlaceHolder Metric", "Pipe", "T3", None, None, 2.0, "in"),
    ]
    _make_piping_dcf(proj / "Piping.dcf", rows)

    sheets = proj / "Spec Sheets"
    sheets.mkdir()
    # Only CS150.pspc exists
    _make_pspc(sheets / "CS150.pspc", [("STD", "ASTM A106")])
    return proj


@pytest.fixture
def no_sheets_project(tmp_path: Path) -> Path:
    """Project WITHOUT a Spec Sheets folder."""
    proj = tmp_path / "NOSHEETS_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    rows = [
        (40, "L-001", "CS150", "CS150", "Pipe", "T1", "STD", "ASTM A106", 2.0, "in"),
    ]
    _make_piping_dcf(proj / "Piping.dcf", rows)
    return proj


class TestGhostSpecs:
    def test_ss150_is_ghost(self, ghost_project):
        r = validate_specs(str(ghost_project))
        assert r["ghost_specs"]["checkable"] is True
        assert "SS150" in r["ghost_specs"]["specs"]

    def test_cs150_not_ghost(self, ghost_project):
        r = validate_specs(str(ghost_project))
        assert "CS150" not in r["ghost_specs"]["specs"]

    def test_auxiliary_spec_excluded_from_ghost(self, ghost_project):
        r = validate_specs(str(ghost_project))
        # PlaceHolder Metric is auxiliary -> must not appear as ghost
        assert "PlaceHolder Metric" not in r["ghost_specs"]["specs"]

    def test_count(self, ghost_project):
        r = validate_specs(str(ghost_project))
        assert r["ghost_specs"]["count"] == 1

    def test_no_sheets_folder_marks_not_checkable(self, no_sheets_project):
        r = validate_specs(str(no_sheets_project))
        assert r["ghost_specs"]["checkable"] is False

    def test_no_sheets_folder_does_not_mark_everything_ghost(self, no_sheets_project):
        # When Spec Sheets is absent, ghost specs list must be empty (not all specs ghost).
        r = validate_specs(str(no_sheets_project))
        assert r["ghost_specs"]["specs"] == []

    def test_no_sheets_folder_catalogue_violations_not_checkable(self, no_sheets_project):
        r = validate_specs(str(no_sheets_project))
        assert r["catalogue_violations"]["checkable"] is False


class TestSpecSheetStems:
    def test_returns_none_when_no_folder(self, tmp_path):
        proj = tmp_path / "P"
        proj.mkdir()
        assert _spec_sheet_stems(proj) is None

    def test_returns_set_of_stems(self, tmp_path):
        proj = tmp_path / "P"
        proj.mkdir()
        sheets = proj / "Spec Sheets"
        sheets.mkdir()
        (sheets / "CS150.pspc").write_bytes(b"")
        (sheets / "SS150.pspc").write_bytes(b"")
        (sheets / "ignore.pspx").write_bytes(b"")  # ZIP archive, must be ignored
        result = _spec_sheet_stems(proj)
        assert result == {"CS150", "SS150"}

    def test_pspx_excluded(self, tmp_path):
        proj = tmp_path / "P"
        proj.mkdir()
        sheets = proj / "Spec Sheets"
        sheets.mkdir()
        (sheets / "only.pspx").write_bytes(b"")
        result = _spec_sheet_stems(proj)
        assert result == set()


# ===========================================================================
# Part 6 – Check 4: catalogue_violations (Schedule & Material)
# ===========================================================================

# Catalogue for CS150: Schedule in {STD, XH}, Material in {ASTM A106 GrB}
_CATALOGUE_CS150 = [
    ("STD", "ASTM A106 GrB"),
    ("XH", "ASTM A106 GrB"),
]

# Piping rows:
# PnPID 50: Schedule=STD (allowed), Material=ASTM A106 GrB (allowed) -> clean
# PnPID 51: Schedule=XXH (NOT in catalogue) -> schedule violation
# PnPID 52: Schedule=STD (allowed), Material=ASTM A335 GrP11 (not in catalogue) -> material violation
# PnPID 53: empty Schedule -> skipped for schedule check
_CATALOGUE_ROWS = [
    (50, "L-001", "CS150", "CS150", "Pipe", "T1", "STD",  "ASTM A106 GrB",  2.0, "in"),
    (51, "L-001", "CS150", "CS150", "Pipe", "T2", "XXH",  "ASTM A106 GrB",  2.0, "in"),
    (52, "L-001", "CS150", "CS150", "Pipe", "T3", "STD",  "ASTM A335 GrP11", 2.0, "in"),
    (53, "L-001", "CS150", "CS150", "Pipe", "T4", None,   "ASTM A106 GrB",  2.0, "in"),
]


@pytest.fixture
def catalogue_project(tmp_path: Path) -> Path:
    proj = tmp_path / "CATALOGUE_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _make_piping_dcf(proj / "Piping.dcf", _CATALOGUE_ROWS)

    sheets = proj / "Spec Sheets"
    sheets.mkdir()
    _make_pspc(sheets / "CS150.pspc", _CATALOGUE_CS150)
    return proj


class TestCatalogueViolations:
    def test_schedule_violation_detected(self, catalogue_project):
        r = validate_specs(str(catalogue_project))
        cat = r["catalogue_violations"]
        assert cat["checkable"] is True
        assert cat["schedule_count"] == 1
        sched_pnpids = [e["pnpid"] for e in cat["schedule_violations"]]
        assert 51 in sched_pnpids

    def test_clean_schedule_not_flagged(self, catalogue_project):
        r = validate_specs(str(catalogue_project))
        sched_pnpids = [e["pnpid"] for e in r["catalogue_violations"]["schedule_violations"]]
        assert 50 not in sched_pnpids

    def test_null_schedule_not_flagged(self, catalogue_project):
        r = validate_specs(str(catalogue_project))
        sched_pnpids = [e["pnpid"] for e in r["catalogue_violations"]["schedule_violations"]]
        assert 53 not in sched_pnpids

    def test_material_violation_detected(self, catalogue_project):
        r = validate_specs(str(catalogue_project))
        cat = r["catalogue_violations"]
        assert cat["material_count"] == 1
        mat_pnpids = [e["pnpid"] for e in cat["material_violations"]]
        assert 52 in mat_pnpids

    def test_material_confidence_is_low(self, catalogue_project):
        r = validate_specs(str(catalogue_project))
        assert r["catalogue_violations"]["material_confidence"] == "low"

    def test_schedule_violation_payload(self, catalogue_project):
        r = validate_specs(str(catalogue_project))
        ex = r["catalogue_violations"]["schedule_violations"][0]
        assert ex["schedule"] == "XXH"
        assert ex["spec"] == "CS150"


class TestReadSpecCatalogue:
    def test_reads_schedules(self, tmp_path):
        pspc = tmp_path / "CS150.pspc"
        _make_pspc(pspc, [("STD", "ASTM A106"), ("XH", "ASTM A106")])
        cat = _read_spec_catalogue(pspc)
        assert "STD" in cat["schedules"]
        assert "XH" in cat["schedules"]

    def test_reads_materials_normalized(self, tmp_path):
        pspc = tmp_path / "CS150.pspc"
        _make_pspc(pspc, [("STD", "ASTM A106 GrB")])
        cat = _read_spec_catalogue(pspc)
        # _norm_tight removes spaces and uppercases
        assert "ASTMA106GRB" in cat["materials"]

    def test_null_schedule_excluded(self, tmp_path):
        pspc = tmp_path / "CS150.pspc"
        _make_pspc(pspc, [(None, "ASTM A106")])
        cat = _read_spec_catalogue(pspc)
        assert len(cat["schedules"]) == 0

    def test_null_material_excluded(self, tmp_path):
        pspc = tmp_path / "CS150.pspc"
        _make_pspc(pspc, [("STD", None)])
        cat = _read_spec_catalogue(pspc)
        assert len(cat["materials"]) == 0


# ===========================================================================
# Part 7 – Read-only guarantee
# ===========================================================================


class TestReadOnly:
    def test_piping_dcf_unchanged(self, ghost_project):
        db = ghost_project / "Piping.dcf"
        before = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        validate_specs(str(ghost_project))

        assert db.read_bytes() == before
        assert db.stat().st_mtime_ns == mtime_before

    def test_pspc_unchanged(self, ghost_project):
        pspc = ghost_project / "Spec Sheets" / "CS150.pspc"
        before = pspc.read_bytes()
        mtime_before = pspc.stat().st_mtime_ns

        validate_specs(str(ghost_project))

        assert pspc.read_bytes() == before
        assert pspc.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path(self, ghost_project):
        # resolve_project_dir accepts a .dcf path and walks up to project folder.
        r = validate_specs(str(ghost_project / "Piping.dcf"))
        assert r["ok"] is True


# ===========================================================================
# Part 8 – Parametrisation: data["ignore_specs"] and data["limit"]
# ===========================================================================


@pytest.fixture
def param_project(tmp_path: Path) -> Path:
    """Project with three real specs, one of which is custom-ignored."""
    proj = tmp_path / "PARAM_PROJ"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")

    # L-001 mixes CS150 + MY_AUX (custom aux) -> should NOT flag if MY_AUX is ignored
    # L-002 mixes CS150 + SS150 (both real) -> always flags
    rows = [
        (60, "L-001", "CS150", "CS150",  "Pipe", "T1", "STD", "ASTM A106", 2.0, "in"),
        (61, "L-001", "CS150", "MY_AUX", "Pipe", "T2", None,  None,        2.0, "in"),
        (62, "L-002", "CS150", "CS150",  "Pipe", "T3", "STD", "ASTM A106", 2.0, "in"),
        (63, "L-002", "SS150", "SS150",  "Pipe", "T4", "STD", "ASTM A312", 2.0, "in"),
    ]
    _make_piping_dcf(proj / "Piping.dcf", rows)
    return proj


class TestParametrisation:
    def test_custom_ignore_specs_excludes_line(self, param_project):
        # With MY_AUX in ignore_specs, L-001 has only CS150 -> not mixed.
        r = validate_specs(
            str(param_project), data={"ignore_specs": ["MY_AUX"]}
        )
        flagged = [e["line"] for e in r["mixed_specs"]["examples"]]
        assert "L-001" not in flagged

    def test_without_custom_ignore_spec_line_mixed(self, param_project):
        # Without ignoring MY_AUX (use empty list), L-001 DOES mix CS150 + MY_AUX.
        r = validate_specs(
            str(param_project), data={"ignore_specs": []}
        )
        flagged = [e["line"] for e in r["mixed_specs"]["examples"]]
        assert "L-001" in flagged
        # Verify the payload keys for the mixed entry of L-001.
        l001 = next(e for e in r["mixed_specs"]["examples"] if e["line"] == "L-001")
        assert l001["n_specs"] == 2
        assert l001["specs"] == sorted(l001["specs"])  # ordenada
        assert "spec_components" in l001              # clave renombrada presente
        assert "components" not in l001               # clave antigua ausente

    def test_limit_caps_examples(self, tmp_path):
        # Build a project with 5 mismatches; limit=2 should cap at 2.
        proj = tmp_path / "LIMIT_PROJ"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        rows = [
            (i, f"L-{i:03}", "CS150", "SS150", "Pipe", f"T{i}", "STD", "ASTM A106", 2.0, "in")
            for i in range(1, 6)
        ]
        _make_piping_dcf(proj / "Piping.dcf", rows)

        r = validate_specs(str(proj), data={"limit": 2})
        mm = r["mismatched_spec"]
        assert mm["count"] == 5       # total never truncated
        assert len(mm["examples"]) == 2
        assert mm["omitted"] == 3

    def test_limit_zero_no_cap(self, tmp_path):
        proj = tmp_path / "NOLIMIT_PROJ"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        rows = [
            (i, f"L-{i:03}", "CS150", "SS150", "Pipe", f"T{i}", "STD", "ASTM A106", 2.0, "in")
            for i in range(1, 6)
        ]
        _make_piping_dcf(proj / "Piping.dcf", rows)

        r = validate_specs(str(proj), data={"limit": 0})
        mm = r["mismatched_spec"]
        assert mm["count"] == 5
        assert len(mm["examples"]) == 5
        assert mm["omitted"] == 0

    def test_ignore_specs_reflected_in_output(self, param_project):
        custom = ["MY_AUX", "ANOTHER"]
        r = validate_specs(str(param_project), data={"ignore_specs": custom})
        # Output must reflect the custom list (sorted).
        assert r["ignore_specs"] == sorted(custom)
