"""Tests for the MCP ``specgen`` tool dispatch (server.specgen).

Two layers:
- No sample data needed: unknown operation and parameter validation return a JSON ``error``
  (never raise), in Spanish.
- Sample-backed (skipif when the scratchpad data is absent): ``analyze`` returns coverage +
  gaps without writing a spec, and ``build`` generates a valid .pspc/.pspx + review workbook.
"""

from __future__ import annotations

import json
import os

import pytest

from _sample import needs_sample

from autocad_mcp import server


async def _call(operation, data):
    return json.loads(await server.specgen(operation=operation, data=data))


# ---------------------------------------------------------------- validation (no data)
async def test_unknown_operation_returns_error():
    # Valid-looking (existing) paths so we reach the dispatch, then an unknown op.
    here = os.path.dirname(os.path.abspath(__file__))
    out = await _call("frobnicate", {"piping_class": os.path.abspath(__file__),
                                      "catalogs": here})
    assert "error" in out
    assert "frobnicate" in out["error"]


async def test_missing_piping_class_returns_error():
    out = await _call("analyze", {"catalogs": "."})
    assert "error" in out
    assert "piping_class" in out["error"]


async def test_missing_catalogs_returns_error():
    out = await _call("analyze", {"piping_class": os.path.abspath(__file__)})
    assert "error" in out
    assert "catalogs" in out["error"]


async def test_bad_piping_class_path_returns_error():
    out = await _call("analyze", {"piping_class": "no_such_file.xlsx", "catalogs": "."})
    assert "error" in out
    assert "no_such_file.xlsx" in out["error"]


async def test_bad_catalogs_dir_returns_error():
    out = await _call("analyze", {"piping_class": os.path.abspath(__file__),
                                  "catalogs": "no_such_dir_xyz"})
    assert "error" in out
    assert "no_such_dir_xyz" in out["error"]


async def test_build_without_out_returns_error():
    out = await _call("build", {"piping_class": os.path.abspath(__file__), "catalogs": "."})
    assert "error" in out
    assert "out" in out["error"]


async def test_extend_catalog_without_out_returns_error():
    out = await _call("extend_catalog",
                      {"piping_class": os.path.abspath(__file__), "catalogs": "."})
    assert "error" in out
    assert "out" in out["error"]


# ---------------------------------------------------------------- sample-backed
@needs_sample
async def test_analyze_returns_coverage_and_gaps(sample_xlsx, scratch_dir):
    out = await _call("analyze", {"piping_class": sample_xlsx, "catalogs": scratch_dir})
    assert out["ok"] is True
    cov = out["coverage"]
    assert cov["total"] > 500
    assert 0.0 <= cov["match_pct"] <= 100.0
    assert set(cov["by_level"]) == {"ALTA", "MEDIA", "SUSTITUCION", "BAJA"}
    assert isinstance(out["gaps"], list)
    assert len(out["by_family"]) >= 8
    # analyze without 'out' must NOT write the review workbook
    assert out["review_xlsx"] is None


@needs_sample
async def test_build_generates_valid_spec(sample_xlsx, scratch_dir, sample_template_pspc, tmp_path):
    data = {
        "piping_class": sample_xlsx,
        "catalogs": scratch_dir,
        "out": str(tmp_path / "out"),
        "extend_h2": True,
    }
    if sample_template_pspc:
        data["template_pspc"] = sample_template_pspc
    out = await _call("build", data)

    assert out["ok"] is True
    files = out["files"]
    assert os.path.exists(files["pspc"])
    assert os.path.exists(files["pspx"])
    assert os.path.exists(files["review_xlsx"])
    assert out["components_built"] > 0
    assert out["verify"]["integrity_check"] == "ok"
    assert out["verify"]["graph_consistent"] is True
    assert out["extend_h2"]["families_created"] > 0

    # the .pspx is a valid ZIP with parseable XML parts
    import xml.etree.ElementTree as ET
    import zipfile
    with zipfile.ZipFile(files["pspx"]) as z:
        for name in z.namelist():
            if name.lower().endswith((".xml", ".rels")):
                ET.fromstring(z.read(name))
