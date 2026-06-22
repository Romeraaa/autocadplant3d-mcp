"""Tests for plant3d_query.find_untagged — headless, no AutoCAD, no network.

Builds a synthetic Plant 3D project folder in tmp_path with a real SQLite
``Piping.dcf`` (valid header, created with the ``sqlite3`` module) and exercises
``find_untagged`` against it. The real network databases are never touched.
"""

import sqlite3
from pathlib import Path

import pytest

from autocad_mcp.plant3d_query import find_untagged


# ---------------------------------------------------------------------------
# Fixture: synthetic Plant 3D project
# ---------------------------------------------------------------------------

# (PnPID, LineNumberTag, PartCategory, ShortDescription, Spec, NominalDiameter, NominalUnit)
#
# Rows 1-4: untagged by predicate (NULL / '' / '   ' / '?').
# Rows 5-7: tagged -> must NOT count (incl. a valid tag containing '?').
_SEED_ROWS = [
    # --- untagged ---
    (1, None, "Pipe", "Tubo recto", "CS150", 2.0, "in"),          # NULL tag
    (2, "", "Elbow", "Codo 90", "CS150", 2.0, "in"),              # empty
    (3, "   ", "Pipe", "Tubo recto", None, 4.0, "in"),           # blank spaces -> spec NULL
    (4, "?", "Flange", "Brida", "", None, "in"),                 # literal '?' -> spec '' , dia NULL
    # --- tagged (must NOT count) ---
    (5, '3"-P-001-ET?', "Pipe", "Tubo con interrogante", "CS300", 3.0, "in"),  # contains '?', valid
    (6, "AIRE", "Pipe", "Linea de aire", "CS300", 6.0, "in"),
    (7, '2"-NL01', "Valve", "Valvula", "SS150", 2.0, "in"),
]


def _build_piping_dcf(path: Path) -> None:
    """Create a real SQLite Piping.dcf with the minimal Plant 3D schema."""
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE PipeRunComponent (PnPID INTEGER, LineNumberTag TEXT)"
        )
        con.execute(
            "CREATE TABLE EngineeringItems ("
            "PnPID INTEGER, PartCategory TEXT, ShortDescription TEXT, "
            "Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
        )
        for pnpid, tag, cat, desc, spec, dia, unit in _SEED_ROWS:
            con.execute(
                "INSERT INTO PipeRunComponent (PnPID, LineNumberTag) VALUES (?, ?)",
                (pnpid, tag),
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


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A synthetic Plant 3D project folder with Project.xml + Piping.dcf."""
    proj = tmp_path / "PROYECTO_TEST"
    proj.mkdir()
    (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
    _build_piping_dcf(proj / "Piping.dcf")
    return proj


@pytest.fixture
def result(project_dir: Path) -> dict:
    return find_untagged(str(project_dir))


# ---------------------------------------------------------------------------
# Sanity: fixture really produced a valid SQLite file
# ---------------------------------------------------------------------------


def test_dcf_is_real_sqlite(project_dir: Path):
    with (project_dir / "Piping.dcf").open("rb") as f:
        assert f.read(16) == b"SQLite format 3\x00"


# ---------------------------------------------------------------------------
# Case 1: untagged predicate
# ---------------------------------------------------------------------------


class TestUntaggedPredicate:
    def test_count_exact(self, result):
        # Rows 1-4 untagged; rows 5-7 (incl. '3"-P-001-ET?') tagged.
        assert result["untagged_count"] == 4

    def test_only_expected_pnpids(self, result):
        pnpids = sorted(c["pnpid"] for c in result["components"])
        assert pnpids == [1, 2, 3, 4]

    def test_valid_tag_with_question_mark_excluded(self, result):
        pnpids = {c["pnpid"] for c in result["components"]}
        assert 5 not in pnpids  # '3"-P-001-ET?' is a valid tag

    def test_normal_tags_excluded(self, result):
        pnpids = {c["pnpid"] for c in result["components"]}
        assert 6 not in pnpids  # 'AIRE'
        assert 7 not in pnpids  # '2"-NL01'

    def test_ok_flag_and_project_name(self, result):
        assert result["ok"] is True
        assert result["project"] == "PROYECTO_TEST"


# ---------------------------------------------------------------------------
# Case 2: by_class
# ---------------------------------------------------------------------------


class TestByClass:
    def test_counts(self, result):
        counts = {d["class"]: d["count"] for d in result["by_class"]}
        # Untagged rows: 1=Pipe, 2=Elbow, 3=Pipe, 4=Flange
        assert counts == {"Pipe": 2, "Elbow": 1, "Flange": 1}

    def test_descending_order_by_count(self, result):
        counts = [d["count"] for d in result["by_class"]]
        assert counts == sorted(counts, reverse=True)
        # Top entry is the most frequent class.
        assert result["by_class"][0]["class"] == "Pipe"
        assert result["by_class"][0]["count"] == 2

    def test_null_class_bucketed(self, tmp_path):
        # Separate project where an untagged row has PartCategory NULL.
        proj = tmp_path / "P_NULLCLASS"
        proj.mkdir()
        (proj / "Project.xml").write_text("<Project/>", encoding="utf-8")
        db = proj / "Piping.dcf"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE PipeRunComponent (PnPID INTEGER, LineNumberTag TEXT)")
        con.execute(
            "CREATE TABLE EngineeringItems (PnPID INTEGER, PartCategory TEXT, "
            "ShortDescription TEXT, Spec TEXT, NominalDiameter REAL, NominalUnit TEXT)"
        )
        con.execute("INSERT INTO PipeRunComponent VALUES (10, NULL)")
        con.execute(
            "INSERT INTO EngineeringItems VALUES (10, NULL, 'x', 'CS150', 2.0, 'in')"
        )
        con.commit()
        con.close()

        r = find_untagged(str(proj))
        classes = {d["class"]: d["count"] for d in r["by_class"]}
        assert classes == {"(sin clase)": 1}


# ---------------------------------------------------------------------------
# Case 3: by_spec
# ---------------------------------------------------------------------------


class TestBySpec:
    def test_counts(self, result):
        counts = {d["spec"]: d["count"] for d in result["by_spec"]}
        # Untagged specs: 1=CS150, 2=CS150, 3=NULL, 4=''
        # NULL and '' both bucket to "(sin spec)".
        assert counts == {"CS150": 2, "(sin spec)": 2}

    def test_null_and_empty_spec_merged(self, result):
        names = [d["spec"] for d in result["by_spec"]]
        assert names.count("(sin spec)") == 1  # not two separate buckets

    def test_descending_order(self, result):
        counts = [d["count"] for d in result["by_spec"]]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# Case 4: components payload (size formatting + raw passthrough)
# ---------------------------------------------------------------------------


class TestComponentsPayload:
    def test_size_formatted_inches(self, result):
        comp = next(c for c in result["components"] if c["pnpid"] == 1)
        assert comp["size"] == '2"'  # 2.0 / 'in' -> 2"

    def test_size_none_when_dia_null(self, result):
        comp4 = next(c for c in result["components"] if c["pnpid"] == 4)
        assert comp4["size"] is None  # dia NULL -> None

    def test_size_formatted_when_dia_present(self, result):
        comp3 = next(c for c in result["components"] if c["pnpid"] == 3)
        assert comp3["size"] == '4"'  # 4.0 / 'in' -> 4"

    def test_class_passthrough(self, result):
        comp = next(c for c in result["components"] if c["pnpid"] == 2)
        assert comp["class"] == "Elbow"

    def test_spec_passthrough_including_null_and_empty(self, result):
        comp3 = next(c for c in result["components"] if c["pnpid"] == 3)
        comp4 = next(c for c in result["components"] if c["pnpid"] == 4)
        assert comp3["spec"] is None  # raw NULL, not "(sin spec)"
        assert comp4["spec"] == ""    # raw empty string

    def test_description_passthrough(self, result):
        comp = next(c for c in result["components"] if c["pnpid"] == 1)
        assert comp["description"] == "Tubo recto"


# ---------------------------------------------------------------------------
# Case 5: read-only — function does not modify the database
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_db_unchanged(self, project_dir):
        db = project_dir / "Piping.dcf"
        before = db.read_bytes()
        mtime_before = db.stat().st_mtime_ns

        find_untagged(str(project_dir))

        assert db.read_bytes() == before
        assert db.stat().st_mtime_ns == mtime_before

    def test_accepts_dcf_path_too(self, project_dir):
        # resolve_project_dir accepts a .dcf path and uses its parent folder.
        r = find_untagged(str(project_dir / "Piping.dcf"))
        assert r["ok"] is True
        assert r["untagged_count"] == 4
