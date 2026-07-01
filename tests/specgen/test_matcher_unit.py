"""Unit tests for the confidence model using a tiny in-memory fake catalog (no files)."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from autocad_mcp.specgen.matcher import CatalogMatcher
from autocad_mcp.specgen.piping_class import PipingClassEntry


class _FakeIndex:
    """Minimal stand-in for CatalogIndex: just the attributes the matcher reads."""

    def __init__(self, con: sqlite3.Connection, logical: str = "CAT") -> None:
        self.handles = {logical: con}
        self.tables = {logical: {"EngineeringItems", "PnPBase", "PartPort", "Port"}}


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE PnPBase (PnPID INTEGER PRIMARY KEY, PnPClassName TEXT)")
    con.execute(
        "CREATE TABLE EngineeringItems (PnPID INTEGER, SizeRecordId BLOB, PartFamilyId BLOB, "
        "PartFamilyLongDesc TEXT, NominalDiameter REAL, Schedule TEXT, PressureClass TEXT, "
        "EndType TEXT)"
    )
    con.execute("CREATE TABLE PartPort (PnPID INTEGER, Part INTEGER, Port INTEGER)")
    con.execute("CREATE TABLE Port (PnPID INTEGER, NominalDiameter REAL)")
    return con


def _add(con, pnpid, cls, desc, dia, sch, pc, end):
    con.execute("INSERT INTO PnPBase VALUES (?,?)", (pnpid, cls))
    con.execute(
        "INSERT INTO EngineeringItems VALUES (?,?,?,?,?,?,?,?)",
        (pnpid, uuid.uuid4().bytes_le, uuid.uuid4().bytes_le, desc, dia, sch, pc, end),
    )


def _entry(**kw) -> PipingClassEntry:
    base = dict(
        sheet="S", family="F", type_="T", unicode_code="U", description="d",
        lcode=None, lcode_base=None, is_hydrogen=False, main_diameter=None,
        branch_diameter=None, schedule=None, rating=None, end_type=None,
    )
    base.update(kw)
    return PipingClassEntry(**base)


# --------------------------------------------------------------------------- tests
def test_no_lcode_is_baja():
    con = _con()
    m = CatalogMatcher(_FakeIndex(con))
    e = _entry(lcode=None)
    m.match(e)
    assert e.confidence == "BAJA"
    assert e.size_record_id is None


def test_lcode_absent_is_baja():
    con = _con()
    _add(con, 1, "Pipe", "PIPE L-999 SCH80", 2.0, "80", None, "BV")
    m = CatalogMatcher(_FakeIndex(con))
    e = _entry(lcode="L-111", lcode_base="L-111", main_diameter=2.0)
    m.match(e)
    assert e.confidence == "BAJA"


def test_single_dominant_candidate_is_alta():
    con = _con()
    _add(con, 1, "Pipe", "PIPE L-100 BV", 2.0, "80", None, "BV")
    m = CatalogMatcher(_FakeIndex(con))
    e = _entry(lcode="L-100", lcode_base="L-100", main_diameter=2.0, schedule="80", end_type="BV")
    m.match(e)
    assert e.confidence == "ALTA"
    assert e.size_record_id is not None


def test_h1_degrades_to_media_when_endtype_unknown_and_family_mixes_ends():
    # One family, several rows at the diameter with DIFFERENT EndTypes, and the entry has no
    # deduced end type -> H1 must keep it out of ALTA.
    con = _con()
    _add(con, 1, "Flange", "FLANGE L-200 MIXED", 2.0, None, None, "FL")
    _add(con, 2, "Flange", "FLANGE L-200 MIXED", 2.0, None, None, "SW")
    m = CatalogMatcher(_FakeIndex(con))
    e = _entry(lcode="L-200", lcode_base="L-200", main_diameter=2.0, end_type=None)
    m.match(e)
    assert e.confidence == "MEDIA"
    assert "H1" in e.match_note


def test_hydrogen_falling_back_to_base_is_substitution():
    con = _con()
    _add(con, 1, "Pipe", "PIPE L-300 BV", 2.0, "80", None, "BV")   # base only, no -H2 family
    m = CatalogMatcher(_FakeIndex(con))
    e = _entry(lcode="L-300-H2", lcode_base="L-300", is_hydrogen=True,
               main_diameter=2.0, schedule="80", end_type="BV")
    m.match(e)
    assert e.confidence == "SUSTITUCION"


def test_relaxed_diameter_retry_resolves_bolts_as_media():
    # Bolt-style family: catalog NominalDiameter is the flange size, not the bolt dia the Excel
    # lists -> strict filter empty -> relaxed retry resolves, degraded to MEDIA.
    con = _con()
    _add(con, 1, "BoltSet", "STUD H-291", 24.0, None, None, None)  # only a 24" flange-size row
    m = CatalogMatcher(_FakeIndex(con))
    e = _entry(lcode="H-291", lcode_base="H-291", main_diameter=0.625)  # bolt dia not in catalog
    m.match(e)
    assert e.size_record_id is not None
    assert e.confidence == "MEDIA"
    assert "diametro" in e.match_note
