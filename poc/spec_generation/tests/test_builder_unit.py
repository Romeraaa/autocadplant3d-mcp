"""Unit tests for spec_builder XML generation and definition assembly (no catalog files)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from specgen import spec_builder
from specgen.spec_builder import (
    BranchCell,
    BranchSymbol,
    SpecDefinition,
    generate_branch_table_xml,
    make_definition,
    parse_branch_table,
)


def _defin() -> SpecDefinition:
    return SpecDefinition(
        name="X", repository_id="{x}", description="d",
        branch_symbols=[BranchSymbol(
            name="A1", description="elbow",
            part_references=[("Elbow", "ELBOW 90 L-1", "abcd-1234")],
        )],
        branch_cells=[BranchCell(header="2", branch="1", symbol="A1")],
    )


def test_branch_table_round_trip():
    defin = _defin()
    raw = generate_branch_table_xml(defin)
    ET.fromstring(raw)   # well-formed XML
    syms, cells = parse_branch_table(raw)
    assert len(syms) == 1 and syms[0].name == "A1"
    assert syms[0].part_references == [("Elbow", "ELBOW 90 L-1", "abcd-1234")]
    assert len(cells) == 1 and cells[0] == BranchCell("2", "1", "A1")


def test_branch_table_escapes_special_chars():
    defin = SpecDefinition(
        name="X", repository_id="{x}", description="d",
        branch_symbols=[BranchSymbol(name="A&B", description="<x>",
                                     part_references=[])],
    )
    raw = generate_branch_table_xml(defin)
    root = ET.fromstring(raw)   # parses despite & and <
    assert root.find("BranchSymbols/BranchSymbol/Name").text == "A&B"


def test_make_definition_without_template_is_minimal_but_valid():
    defin = make_definition(name="NoTpl", description="d", components=[], template_pspx=None)
    assert defin.branch_symbols == [] and defin.branch_cells == []
    # all verbatim XML fragments parse
    for raw in (defin.part_use_priorities_xml, defin.spec_notes_xml,
                defin.spec_sheet_settings_xml, defin.content_types_xml):
        ET.fromstring(raw)
    # branch table generated from an empty definition is still valid
    ET.fromstring(generate_branch_table_xml(defin))


def test_catalog_references_xml_lists_every_catalog():
    refs = [("CAT A", r"C:\x\a.pcat"), ("CAT&B", r"C:\x\b.pcat")]
    raw = spec_builder._catalog_references_xml(refs)
    root = ET.fromstring(raw)
    names = [n.text for n in root.findall("EditorCatalogFileReference/Name")]
    assert names == ["CAT A", "CAT&B"]   # & correctly escaped + unescaped on parse


def test_rels_data_target_is_basename_external():
    raw = spec_builder._rels_xml(r"C:\out\MySpec.pspc")
    root = ET.fromstring(raw)
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    data = [r for r in root.findall(f"{{{ns}}}Relationship")
            if r.get("Type") == spec_builder.DATA_REL_TYPE]
    assert len(data) == 1
    assert data[0].get("Target") == "MySpec.pspc"
    assert data[0].get("TargetMode") == "External"
