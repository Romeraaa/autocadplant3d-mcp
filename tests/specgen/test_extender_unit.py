"""Unit tests for the -H2 target derivation (deduced from entries, not a fixed list)."""

from __future__ import annotations

import os
import sqlite3
import uuid

from autocad_mcp.specgen.catalog_extender import deduce_h2_targets
from autocad_mcp.specgen.piping_class import PipingClassEntry


def _entry(lcode, base, is_h2) -> PipingClassEntry:
    return PipingClassEntry(
        sheet="S", family="F", type_="T", unicode_code="U", description="d",
        lcode=lcode, lcode_base=base, is_hydrogen=is_h2, main_diameter=None,
        branch_diameter=None, schedule=None, rating=None, end_type=None,
    )


def _make_catalog(path: str, descs: list[str]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE EngineeringItems (PnPID INTEGER, PartFamilyId BLOB, PartFamilyLongDesc TEXT)"
    )
    for i, d in enumerate(descs, start=1):
        con.execute("INSERT INTO EngineeringItems VALUES (?,?,?)", (i, uuid.uuid4().bytes_le, d))
    con.commit()
    con.close()


def test_deduce_targets_routes_base_lcode_to_owning_catalog(tmp_path):
    cat_a = os.path.join(tmp_path, "A.pcat")
    cat_b = os.path.join(tmp_path, "B.pcat")
    _make_catalog(cat_a, ["PIPE L-100 BV", "ELBOW L-200 BV"])
    _make_catalog(cat_b, ["FLANGE L-300 FL"])

    entries = [
        _entry("L-100-H2", "L-100", True),   # base lives in A
        _entry("L-300-H2", "L-300", True),   # base lives in B
        _entry("L-999-H2", "L-999", True),   # base absent everywhere
        _entry("L-100", "L-100", False),     # non-variant: ignored
    ]
    targets = deduce_h2_targets(entries, {cat_a: cat_a, cat_b: cat_b})
    assert targets[cat_a] == ["L-100"]
    assert targets[cat_b] == ["L-300"]
    # L-999 routed nowhere (absent); no catalog gets it
    assert all("L-999" not in v for v in targets.values())


def test_deduce_targets_empty_when_no_variants(tmp_path):
    cat = os.path.join(tmp_path, "C.pcat")
    _make_catalog(cat, ["PIPE L-100 BV"])
    entries = [_entry("L-100", "L-100", False)]
    targets = deduce_h2_targets(entries, {cat: cat})
    assert targets[cat] == []
