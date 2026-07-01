"""Sample-backed integration tests (skipped when the scratchpad data is absent).

These exercise the real REPSOL piping class + catalogs: parse counts, confidence levels (STUD-BOLT
regression), the H2 extension, spec integrity + graph, a valid .pspx, and the full build smoke test.
"""

from __future__ import annotations

import os
import zipfile

from _sample import needs_sample

from autocad_mcp.specgen.catalog_extender import deduce_h2_targets, extend_catalogs, verify as verify_h2
from autocad_mcp.specgen.catalog_index import CatalogIndex, discover_catalogs
from autocad_mcp.specgen.matcher import CatalogMatcher
from autocad_mcp.specgen.piping_class import parse_workbook


def _matched_entries(xlsx: str, catalogs_dir: str):
    entries = parse_workbook(xlsx)
    index = CatalogIndex(catalogs_dir)
    try:
        m = CatalogMatcher(index)
        for e in entries:
            m.match(e)
    finally:
        index.close()
    return entries


@needs_sample
def test_parse_counts(sample_xlsx):
    entries = parse_workbook(sample_xlsx)
    assert len(entries) > 500          # the real class has ~774 rows
    sheets = {e.sheet for e in entries}
    assert len(sheets) >= 8
    assert any("STUD" in s.upper() or "BOLT" in s.upper() for s in sheets)


@needs_sample
def test_stud_bolt_regression(sample_xlsx, scratch_dir):
    # The espárragos (H-291..H-298) must ALL resolve (the historic STUD-BOLT not-matched bug).
    entries = _matched_entries(sample_xlsx, scratch_dir)
    bolts = [e for e in entries if e.lcode and e.lcode.startswith("H-")]
    assert bolts, "no se encontraron entradas de esparrago H-xxx"
    assert all(e.size_record_id is not None for e in bolts)


@needs_sample
def test_confidence_levels_present(sample_xlsx, scratch_dir):
    entries = _matched_entries(sample_xlsx, scratch_dir)
    levels = {e.confidence for e in entries}
    assert "ALTA" in levels and "MEDIA" in levels
    matched = [e for e in entries if e.size_record_id is not None]
    assert len(matched) / len(entries) > 0.7   # broad coverage


@needs_sample
def test_h2_extension_closes_codes(sample_xlsx, scratch_dir, tmp_path):
    entries = parse_workbook(sample_xlsx)
    src_paths = [p for _logical, p in discover_catalogs(scratch_dir)]
    targets = deduce_h2_targets(entries, {p: p for p in src_paths})
    out_dir = os.path.join(tmp_path, "catalogs")
    extenders = extend_catalogs(targets, out_dir)
    by_fname = {os.path.basename(p): lc for p, lc in targets.items()}
    rep = verify_h2(extenders, out_dir, by_fname)

    # every extended catalog stays consistent and the deduced codes are present
    for fname, r in rep.items():
        assert r["integrity_check"] == "ok"
        assert r["graph_consistent"], (fname, r["graph_orphans"])
        assert not r["h2_absent"], (fname, r["h2_absent"])
    assert sum(e.families_created for e in extenders.values()) > 0

    # H2 entries now resolve to a DEDICATED -H2 family in the extended set
    matched_ext = _matched_entries(sample_xlsx, out_dir)
    h2 = [e for e in matched_ext if e.is_hydrogen]
    dedicated = [e for e in h2 if e.family_desc and "-H2" in e.family_desc]
    assert len(dedicated) > 0.8 * len(h2)   # vast majority resolved to a dedicated family


@needs_sample
def test_full_build_smoke(sample_xlsx, scratch_dir, sample_template_pspc, tmp_path):
    from autocad_mcp.specgen.cli import main
    out = os.path.join(tmp_path, "out")
    argv = ["build", "--piping-class", sample_xlsx, "--catalogs", scratch_dir,
            "--out", out, "--extend-h2"]
    if sample_template_pspc:
        argv += ["--template-pspc", sample_template_pspc]
    rc = main(argv)
    assert rc == 0

    # the artefacts exist
    base = os.path.splitext(os.path.basename(sample_xlsx))[0]
    pspc = os.path.join(out, f"{base}.pspc")
    pspx = os.path.join(out, f"{base}.pspx")
    review = os.path.join(out, "REVISION_MATCHING.xlsx")
    assert os.path.exists(pspc) and os.path.exists(pspx) and os.path.exists(review)
    assert os.path.isdir(os.path.join(out, "catalogs"))   # extended catalogs

    # the .pspx is a valid ZIP with parseable XML parts
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(pspx) as z:
        for name in z.namelist():
            if name.lower().endswith((".xml", ".rels")):
                ET.fromstring(z.read(name))
