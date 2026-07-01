"""Build a COMPLETE AutoCAD Plant 3D specification by SELECTING parts from catalogs.

Standalone proof of concept (NOT part of the MCP server). Standard library only:
``sqlite3, zipfile, uuid, xml.etree.ElementTree, datetime, struct, shutil``.

This extends ``generate_spec_poc.py`` (which proved a single-family pipe spec opens in the
Spec Editor) to a *full* spec equivalent to the template ``NXD-2`` -- every component family
plus a regenerated branch table -- assembled component-by-component from the six REPSOL
catalogs (and, where a catalog has been re-versioned away from the spec, from the template
itself).

Design intent (forward-looking): the build is split into two clearly separated phases so the
next phase can swap the "definition" source for a piping-class Excel without touching the
"materialisation" code:

* SpecDefinition -- WHAT goes in the spec: the list of components (one ComponentRef per part,
  carrying class + SizeRecordId + PartFamilyId), the branch table (symbols + cell matrix) and
  the part-use priorities. Here it is *derived* from the original ``NXD-2.pspc`` / ``NXD-2.pspx``
  as the source of truth.
* Materialisation -- HOW each component is realised: locate it in the right source database and
  copy its full PnP graph (PnPBase + type tables + Port + PartPort + PnPRowRelations + lookups)
  into the fresh ``.pspc`` with new, internally-consistent PnPIDs / PnPGuids, copying geometry
  and GUID BLOBs verbatim.

GUID text encoding (verified empirically against the NXD-2 package, do not assume):
  * ``EngineeringItems.PartFamilyId``, ``content/branchtable.xml`` *and*
    ``content/PartUsePriorities.xml`` all use the SAME text form == ``str(uuid.UUID(bytes_le=blob))``
    (lower-case, dashed). The "hex / big-endian" form (``blob.hex()``) is NOT used in any of these
    XML locations -- it only happens to equal the *reordered* bytes of ``CatalogPartFamilyId``.
  * New GUID blobs are ``uuid.uuid4().bytes_le`` so the round-trip stays consistent.

Inputs are opened strictly read-only (``sqlite3`` URI ``mode=ro``). Output goes to ``./out/``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Reuse the proven primitives from the first PoC.
from generate_spec_poc import (
    blob_to_guid_text,
    columns,
    new_guid_blob,
    now_ticks,
    ro_connect,
)

# --------------------------------------------------------------------------- paths
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")

SCRATCH = (
    r"C:\Users\aromera\AppData\Local\Temp\claude"
    r"\C--Users-aromera-OneDrive---INGENIERIA-Y-DISE-O-ESTRUCTURAL-AVANZADO--S-L-AutocadMCP"
    r"\fd24ee29-daa9-49c0-884e-da26a0be4203\scratchpad"
)
TEMPLATE_PSPC = os.path.join(SCRATCH, "NXD-2.pspc")
TEMPLATE_PSPX = os.path.join(SCRATCH, "NXD-2.pspx")

OUT_NAME = "NXD-2-GEN"

# Logical name -> catalog filename in the scratchpad (accents stripped).
CATALOGS = {
    "REPSOL - TUBERÍA": "REPSOL_TUBERIA.pcat",
    "REPSOL - ACCESORIOS BW": "REPSOL_ACCESORIOS_BW.pcat",
    "REPSOL - ACCESORIOS SW": "REPSOL_ACCESORIOS_SW.pcat",
    "REPSOL - BRIDAS,JUNTAS Y PERNOS": "REPSOL_BRIDAS_JUNTAS_PERNOS.pcat",
    "REPSOL - VALVULAS": "REPSOL_VALVULAS.pcat",
    "REPSOL - VICTAULIC": "REPSOL_VICTAULIC.pcat",
}

DATA_REL_TYPE = "Plant/Specification/Data"

# Component classes whose PnPID also gets a row in PipeRunComponent (run components).
# Gasket and BoltSet are NOT run components; auxiliary tables (lookups) are not components.
NON_RUN_COMPONENT_CLASSES = {"Gasket", "BoltSet"}

# Auxiliary "lookup" tables: rows keyed by a PnPBase PnPID but NOT EngineeringItems components.
# They are copied verbatim from the template (their content is reference data, not parts).
AUX_LOOKUP_TABLES = ["ValveActuatorMap", "StandardBoltLength"]

# Every component type table that may hold a row keyed by a component PnPID. A component lives
# in exactly one of these, except Tee which lives in BOTH SingleBranchFitting and Tee.
COMPONENT_TYPE_TABLES = [
    "BlindDisk", "BlindFlange", "BoltSet", "Cap", "Coupling", "Elbow", "Flange", "Gasket",
    "Nipple", "Olet", "Pipe", "Reducer", "SingleBranchFitting", "SpacerDisk",
    "SpectacleBlind", "Swage", "Tee", "Valve", "ValveActuator",
]


# =========================================================================== DEFINITION
@dataclass
class ComponentRef:
    """One part to place in the spec: class + identity keys read from the template."""

    pnpid_template: int          # PnPID in NXD-2 (used for template-sourced materialisation)
    class_name: str              # PnPClassName, e.g. 'Pipe', 'Valve', 'Tee'
    size_record_id: bytes | None  # primary catalog lookup key (16-byte GUID blob)
    part_family_id: bytes | None  # family GUID blob


@dataclass
class BranchSymbol:
    """A branch-table symbol: short name + the part(s) it resolves to."""

    name: str
    description: str
    # list of (part_type, part_family_name, part_family_id_text) -- id text may be "" (generic).
    part_references: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class BranchCell:
    """One run/branch combination mapped to a symbol name."""

    header: str   # run size (in)
    branch: str   # branch size (in)
    symbol: str   # BranchSymbol.name


@dataclass
class SpecDefinition:
    """The complete *definition* of a spec, independent of how parts are materialised."""

    name: str
    repository_id: str
    description: str
    repo_type: str
    content_type: str
    version: str
    original_version: str
    components: list[ComponentRef] = field(default_factory=list)
    branch_symbols: list[BranchSymbol] = field(default_factory=list)
    branch_cells: list[BranchCell] = field(default_factory=list)
    part_use_priorities_xml: bytes = b""   # copied verbatim (already a valid definition fragment)
    spec_notes_xml: bytes = b""
    spec_sheet_settings_xml: bytes = b""
    content_types_xml: bytes = b""


def derive_definition_from_template() -> SpecDefinition:
    """Read NXD-2.pspc + NXD-2.pspx and produce the SpecDefinition (the source of truth)."""
    con = ro_connect(TEMPLATE_PSPC)
    try:
        rd = con.execute(
            "SELECT Name, RepositoryID, Description, Type, ContentType, Version, OriginalVersion "
            "FROM RepositoryDescriptor LIMIT 1"
        ).fetchone()
        components = [
            ComponentRef(pnpid_template=pid, class_name=cls,
                         size_record_id=srid, part_family_id=fam)
            for pid, cls, srid, fam in con.execute(
                "SELECT b.PnPID, b.PnPClassName, e.SizeRecordId, e.PartFamilyId "
                "FROM EngineeringItems e JOIN PnPBase b ON e.PnPID = b.PnPID "
                "ORDER BY b.PnPClassName, b.PnPID"
            ).fetchall()
        ]
    finally:
        con.close()

    z = zipfile.ZipFile(TEMPLATE_PSPX, "r")
    try:
        branch_raw = z.read("content/branchtable.xml")
        defin = SpecDefinition(
            name=OUT_NAME,
            repository_id="{" + str(uuid.uuid4()) + "}",
            description=(rd[2] or "") + " (GEN: regenerada por spec_builder)",
            repo_type=rd[3],
            content_type=rd[4],
            version=rd[5],
            original_version=rd[6],
            components=components,
            part_use_priorities_xml=z.read("content/PartUsePriorities.xml"),
            spec_notes_xml=z.read("content/SpecNotes.xml"),
            spec_sheet_settings_xml=z.read("content/SpecSheetSettings.xml"),
            content_types_xml=z.read("[Content_Types].xml"),
        )
    finally:
        z.close()

    symbols, cells = _parse_branch_table(branch_raw)
    defin.branch_symbols = symbols
    defin.branch_cells = cells
    return defin


def _parse_branch_table(raw: bytes) -> tuple[list[BranchSymbol], list[BranchCell]]:
    """Parse branchtable.xml into a structured (symbols, cells) representation."""
    root = ET.fromstring(raw.decode("utf-8"))
    symbols: list[BranchSymbol] = []
    syms = root.find("BranchSymbols")
    if syms is not None:
        for s in syms.findall("BranchSymbol"):
            sym = BranchSymbol(
                name=(s.findtext("Name") or "").strip(),
                description=(s.findtext("Description") or "").strip(),
            )
            refs = s.find("BranchPartReferences")
            if refs is not None:
                for bpr in refs.findall("BranchPartReference"):
                    pr = bpr.find("PartReference")
                    if pr is not None:
                        sym.part_references.append((
                            pr.get("PartType", ""),
                            pr.get("PartFamilyName", ""),
                            pr.get("PartFamilyId", ""),
                        ))
            symbols.append(sym)

    cells: list[BranchCell] = []
    branches = root.find("Branches")
    if branches is not None:
        for item in branches.findall("BranchTableItem"):
            header = item.find("Header")
            branch = item.find("Branch")
            opts = item.find("BranchOptions")
            sym_name = ""
            if opts is not None:
                bs = opts.find("BranchSymbol")
                sym_name = (bs.text or "").strip() if bs is not None else ""
            cells.append(BranchCell(
                header=header.get("Value", "") if header is not None else "",
                branch=branch.get("Value", "") if branch is not None else "",
                symbol=sym_name,
            ))
    return symbols, cells


# =========================================================================== CATALOG INDEX
class CatalogIndex:
    """Read-only handles to the six catalogs plus a SizeRecordId -> catalog lookup."""

    def __init__(self) -> None:
        self.handles: dict[str, sqlite3.Connection] = {}
        self.tables: dict[str, set[str]] = {}
        self._srid_to_cat: dict[bytes, str] = {}
        for logical, fname in CATALOGS.items():
            path = os.path.join(SCRATCH, fname)
            if not os.path.exists(path):
                continue
            con = ro_connect(path)
            self.handles[logical] = con
            self.tables[logical] = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        # Build SizeRecordId index (first catalog that has each id wins; order is CATALOGS order).
        for logical, con in self.handles.items():
            for (srid,) in con.execute(
                "SELECT SizeRecordId FROM EngineeringItems WHERE SizeRecordId IS NOT NULL"
            ).fetchall():
                if srid not in self._srid_to_cat:
                    self._srid_to_cat[srid] = logical

    def find(self, size_record_id: bytes | None) -> str | None:
        """Return the logical catalog name holding this SizeRecordId, or None."""
        if size_record_id is None:
            return None
        return self._srid_to_cat.get(size_record_id)

    def close(self) -> None:
        for con in self.handles.values():
            con.close()


# =========================================================================== MATERIALISER
class Materialiser:
    """Writes the fresh .pspc and copies each component's PnP graph into it.

    Maintains the two ID spaces: PnPBase PnPID (components, ports, lookups) and the relationship
    PnPID (PartPort). New ids start above whatever the template ever allocated so they never
    collide with values embedded elsewhere.
    """

    def __init__(self, out_pspc: str, defin: SpecDefinition, catalogs: CatalogIndex) -> None:
        self.out_pspc = out_pspc
        self.defin = defin
        self.catalogs = catalogs
        self.con: sqlite3.Connection | None = None
        self.next_base = 0
        self.next_rel = 0
        self.ts = now_ticks()
        # column lists of the destination tables, cached
        self._cols: dict[str, list[str]] = {}
        # per-class materialisation outcome, for reporting
        self.report: dict[str, dict] = {}

    # ------------------------------------------------------------------ infra
    def _dst_cols(self, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = columns(self.con, table)
        return self._cols[table]

    def _insert(self, table: str, row: dict) -> None:
        """Insert a row (dict keyed by column name) into a destination table."""
        cols = [c for c in self._dst_cols(table) if c in row]
        ph = ",".join("?" * len(cols))
        col_sql = ",".join(f'"{c}"' for c in cols)
        self.con.execute(
            f'INSERT INTO "{table}" ({col_sql}) VALUES ({ph})',
            tuple(row[c] for c in cols),
        )

    def _alloc_base(self) -> int:
        self.next_base += 1
        return self.next_base

    def _alloc_rel(self) -> int:
        self.next_rel += 1
        return self.next_rel

    # ------------------------------------------------------------------ build
    def build(self) -> None:
        shutil.copyfile(TEMPLATE_PSPC, self.out_pspc)
        self.con = sqlite3.connect(self.out_pspc)
        try:
            self._empty_component_graph()
            self._reset_counters()
            self._materialise_aux_lookups()
            for comp in self.defin.components:
                self._materialise_component(comp)
            self._stamp_identity()
            self.con.commit()
            self.con.execute("VACUUM")
            self.con.commit()
        finally:
            self.con.close()
            self.con = None

    def _empty_component_graph(self) -> None:
        """Strip the template down to schema + metadata, keeping only RepositoryDescriptor."""
        cur = self.con.cursor()
        cur.execute("DELETE FROM EngineeringItems")
        cur.execute("DELETE FROM PipeRunComponent")
        for t in COMPONENT_TYPE_TABLES:
            cur.execute(f'DELETE FROM "{t}"')
        for t in AUX_LOOKUP_TABLES:
            cur.execute(f'DELETE FROM "{t}"')
        cur.execute("DELETE FROM Port")
        cur.execute("DELETE FROM PartPort")
        cur.execute("DELETE FROM PnPRowRelations")
        cur.execute("DELETE FROM LookUps")
        cur.execute("DELETE FROM PnPBase WHERE PnPClassName <> 'RepositoryDescriptor'")
        self.con.commit()

    def _reset_counters(self) -> None:
        seqs = dict(self.con.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        self.next_base = max(int(seqs.get("PnPSys_PnPBase_PnPID", 0) or 0), 1000)
        self.next_rel = max(int(seqs.get("PnPSys_RelationshipSystem_PnPID", 0) or 0), 1000)

    def _add_base_row(self, pnpid: int, class_name: str) -> None:
        self.con.execute(
            "INSERT INTO PnPBase (PnPID, PnPClassName, PnPStatus, PnPRevision, PnPGuid, "
            "PnPTimestamp) VALUES (?,?,?,?,?,?)",
            (pnpid, class_name, 0, 1, new_guid_blob(), self.ts),
        )

    # ------------------------------------------------------------------ lookups
    def _materialise_aux_lookups(self) -> None:
        """Copy ValveActuatorMap / StandardBoltLength (+ their LookUps/PnPBase rows) verbatim.

        These are reference tables, not parts; their GUID/dimension content is copied unchanged
        from the template, only the PnPID is remapped into the fresh PnPBase id space.
        """
        src = ro_connect(TEMPLATE_PSPC)
        try:
            n = 0
            for table in AUX_LOOKUP_TABLES:
                cols = columns(src, table)
                for row in src.execute(f'SELECT * FROM "{table}"').fetchall():
                    d = dict(zip(cols, row))
                    new_id = self._alloc_base()
                    d["PnPID"] = new_id
                    self._add_base_row(new_id, table)
                    self._insert(table, d)
                    # LookUps registry row (PortName mirrors the template: NULL for these).
                    self.con.execute(
                        "INSERT INTO LookUps (PnPID, PortName) VALUES (?, ?)", (new_id, None)
                    )
                    n += 1
            self.report["_aux_lookups"] = {"rows": n}
        finally:
            src.close()

    # ------------------------------------------------------------------ component
    def _materialise_component(self, comp: ComponentRef) -> None:
        """Locate the component and copy its full PnP graph with fresh ids."""
        logical = self.catalogs.find(comp.size_record_id)
        if logical is not None:
            src = self.catalogs.handles[logical]
            src_pnpid = self._catalog_pnpid_for(src, comp)
            origin = logical
        else:
            # Catalog re-versioned away from the spec (Valve/Flange/Gasket/BlindFlange/
            # SpectacleBlind): source the part from the template, which is itself catalog data.
            src = ro_connect(TEMPLATE_PSPC)
            src_pnpid = comp.pnpid_template
            origin = "TEMPLATE"

        try:
            if src_pnpid is None:
                self._bump(comp.class_name, "unresolved", origin)
                return
            self._copy_graph(src, src_pnpid, comp.class_name)
            self._bump(comp.class_name, "ok", origin)
        finally:
            if origin == "TEMPLATE":
                src.close()

    def _catalog_pnpid_for(self, src: sqlite3.Connection, comp: ComponentRef) -> int | None:
        """Return the catalog PnPID of the EngineeringItems row for this SizeRecordId."""
        row = src.execute(
            "SELECT e.PnPID FROM EngineeringItems e JOIN PnPBase b ON e.PnPID = b.PnPID "
            "WHERE e.SizeRecordId = ? AND b.PnPClassName = ? LIMIT 1",
            (comp.size_record_id, comp.class_name),
        ).fetchone()
        if row:
            return row[0]
        row = src.execute(
            "SELECT PnPID FROM EngineeringItems WHERE SizeRecordId = ? LIMIT 1",
            (comp.size_record_id,),
        ).fetchone()
        return row[0] if row else None

    def _copy_graph(self, src: sqlite3.Connection, src_pnpid: int, class_name: str) -> None:
        """Copy one component's full graph from ``src`` into the destination with new ids."""
        new_comp = self._alloc_base()
        self._add_base_row(new_comp, class_name)

        # EngineeringItems (copy every shared column verbatim, incl. GUID/geometry blobs).
        ei_src_cols = columns(src, "EngineeringItems")
        ei_row = src.execute(
            "SELECT * FROM EngineeringItems WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()
        ei = dict(zip(ei_src_cols, ei_row))
        ei["PnPID"] = new_comp
        self._insert("EngineeringItems", ei)

        # PipeRunComponent (run components only).
        if class_name not in NON_RUN_COMPONENT_CLASSES:
            prc_src_cols = columns(src, "PipeRunComponent")
            prc_row = src.execute(
                "SELECT * FROM PipeRunComponent WHERE PnPID = ?", (src_pnpid,)
            ).fetchone()
            if prc_row is not None:
                prc = dict(zip(prc_src_cols, prc_row))
                prc["PnPID"] = new_comp
                self._insert("PipeRunComponent", prc)

        # Every component type table that has a row for this PnPID (Tee -> 2 tables).
        for table in COMPONENT_TYPE_TABLES:
            if table not in self._dst_set():
                continue
            row = self._maybe_row(src, table, src_pnpid)
            if row is not None:
                row["PnPID"] = new_comp
                self._insert(table, row)

        # Ports + PartPort + PnPRowRelations.
        pp_src_cols = columns(src, "PartPort")
        port_src_cols = columns(src, "Port")
        for pp_row in src.execute(
            "SELECT * FROM PartPort WHERE Part = ?", (src_pnpid,)
        ).fetchall():
            pp = dict(zip(pp_src_cols, pp_row))
            src_port = pp["Port"]
            new_port = self._alloc_base()
            self._add_base_row(new_port, "Port")
            port_row = src.execute(
                "SELECT * FROM Port WHERE PnPID = ?", (src_port,)
            ).fetchone()
            if port_row is not None:
                port = dict(zip(port_src_cols, port_row))
                port["PnPID"] = new_port
                self._insert("Port", port)

            new_pp = self._alloc_rel()
            self._insert("PartPort", {
                "PnPID": new_pp,
                "PnPGuid": new_guid_blob(),
                "PnPTimestamp": self.ts,
                "Part": new_comp,
                "Port": new_port,
                "Name": pp.get("Name"),
            })
            self.con.execute(
                "INSERT INTO PnPRowRelations (ROWID, RELID, RelationshipTypeName) "
                "VALUES (?,?,?)",
                (new_comp, new_pp, "PartPort"),
            )

    def _dst_set(self) -> set[str]:
        if not hasattr(self, "_dstset_cache"):
            self._dstset_cache = {
                r[0] for r in self.con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        return self._dstset_cache

    @staticmethod
    def _maybe_row(src: sqlite3.Connection, table: str, pnpid: int) -> dict | None:
        if table not in {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            return None
        cols = columns(src, table)
        row = src.execute(f'SELECT * FROM "{table}" WHERE PnPID = ?', (pnpid,)).fetchone()
        return dict(zip(cols, row)) if row is not None else None

    def _bump(self, class_name: str, status: str, origin: str) -> None:
        d = self.report.setdefault(class_name, {"ok": 0, "unresolved": 0, "origins": {}})
        d[status] = d.get(status, 0) + 1
        d["origins"][origin] = d["origins"].get(origin, 0) + 1

    def _stamp_identity(self) -> None:
        self.con.execute(
            "UPDATE RepositoryDescriptor SET Name=?, RepositoryID=?, Description=?",
            (self.defin.name, self.defin.repository_id, self.defin.description),
        )
        self.con.execute("UPDATE PnPDatabase SET DBID=?", (new_guid_blob(),))


# =========================================================================== .pspx (package)
def build_pspx(out_pspx: str, defin: SpecDefinition, data_target: str) -> None:
    """Write the .pspx package with a GENERATED branchtable.xml and all six catalog references."""
    with zipfile.ZipFile(out_pspx, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", defin.content_types_xml)
        z.writestr("_rels/.rels", _rels_xml(data_target))
        z.writestr("editor/CatalogReferences.xml", _catalog_references_xml())
        z.writestr("content/branchtable.xml", generate_branch_table_xml(defin))
        z.writestr("content/PartUsePriorities.xml", defin.part_use_priorities_xml)
        z.writestr("content/SpecNotes.xml", defin.spec_notes_xml)
        z.writestr("content/SpecSheetSettings.xml", defin.spec_sheet_settings_xml)


def _rels_xml(data_target: str) -> bytes:
    rels = [
        ("Plant/SpecificationEditor/CatalogReferences", "/editor/CatalogReferences.xml",
         "CatalogReference", None),
        ("Plant/Specification/BranchTable", "/content/branchtable.xml", "PlantBranchTable", None),
        ("Plant/Specification/PartPriorities", "/content/PartUsePriorities.xml",
         "PlantPartPriorities", None),
        ("Plant/Specification/SpecNotes", "/content/SpecNotes.xml", "PlantSpecNotes", None),
        ("Plant/Specification/SpecSheetSettings", "/content/SpecSheetSettings.xml",
         "PlantSpecSheetSettings", None),
        (DATA_REL_TYPE, os.path.basename(data_target), "PlantSpecContent", "External"),
    ]
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    parts = [f'<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="{ns}">']
    for rtype, target, rid, mode in rels:
        mode_attr = f' TargetMode="{mode}"' if mode else ""
        parts.append(
            f'<Relationship Type="{rtype}" Target="{target}"{mode_attr} Id="{rid}" />'
        )
    parts.append("</Relationships>")
    return "".join(parts).encode("utf-8")


def _catalog_references_xml() -> bytes:
    """Reference all six REPSOL catalogs by the scratchpad path used in this PoC."""
    ns_xsd = "http://www.w3.org/2001/XMLSchema"
    ns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<EditorCatalogFileReferences xmlns:xsd="{ns_xsd}" xmlns:xsi="{ns_xsi}">',
    ]
    for logical, fname in CATALOGS.items():
        path = os.path.join(SCRATCH, fname)
        lines.append("  <EditorCatalogFileReference>")
        lines.append(f"    <Name>{_xml_escape(logical)}</Name>")
        lines.append(f"    <Reference>{_xml_escape(path)}</Reference>")
        lines.append("  </EditorCatalogFileReference>")
    lines.append("</EditorCatalogFileReferences>")
    return "\n".join(lines).encode("utf-8")


def generate_branch_table_xml(defin: SpecDefinition) -> bytes:
    """Build branchtable.xml from the structured SpecDefinition (NOT copied literally)."""
    ns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    ns_xsd = "http://www.w3.org/2001/XMLSchema"
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<SpecificationBranchTable xmlns:xsi="{ns_xsi}" xmlns:xsd="{ns_xsd}">',
        "  <BranchSymbols>",
    ]
    for sym in defin.branch_symbols:
        out.append("    <BranchSymbol>")
        out.append(f"      <Name>{_xml_escape(sym.name)}</Name>")
        out.append(f"      <Description>{_xml_escape(sym.description)}</Description>")
        out.append("      <BranchPartReferences>")
        for part_type, fam_name, fam_id in sym.part_references:
            out.append("        <BranchPartReference>")
            out.append(
                f'          <PartReference PartType="{_xml_escape(part_type)}" '
                f'PartFamilyName="{_xml_escape(fam_name)}" '
                f'PartFamilyId="{_xml_escape(fam_id)}" />'
            )
            out.append("          <Notes />")
            out.append("        </BranchPartReference>")
        out.append("      </BranchPartReferences>")
        out.append("    </BranchSymbol>")
    out.append("  </BranchSymbols>")
    out.append("  <Branches>")
    for cell in defin.branch_cells:
        out.append("    <BranchTableItem>")
        out.append(f'      <Header Units="in" Value="{_xml_escape(cell.header)}" />')
        out.append(f'      <Branch Units="in" Value="{_xml_escape(cell.branch)}" />')
        out.append("      <BranchOptions>")
        out.append(f"        <BranchSymbol>{_xml_escape(cell.symbol)}</BranchSymbol>")
        out.append("      </BranchOptions>")
        out.append("    </BranchTableItem>")
    out.append("  </Branches>")
    out.append("</SpecificationBranchTable>")
    return "\n".join(out).encode("utf-8")


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# =========================================================================== VERIFY
def verify(out_pspc: str, out_pspx: str, defin: SpecDefinition, mat: Materialiser) -> dict:
    """Run programmatic checks and return a structured report (also printed)."""
    result: dict = {"pspc": out_pspc, "pspx": out_pspx}
    con = ro_connect(out_pspc)
    try:
        result["integrity_check"] = con.execute("PRAGMA integrity_check").fetchone()[0]

        # Component counts per class, generated vs template.
        gen_counts = dict(con.execute(
            "SELECT b.PnPClassName, COUNT(*) FROM EngineeringItems e "
            "JOIN PnPBase b ON e.PnPID=b.PnPID GROUP BY b.PnPClassName"
        ).fetchall())
        tpl = ro_connect(TEMPLATE_PSPC)
        try:
            tpl_counts = dict(tpl.execute(
                "SELECT b.PnPClassName, COUNT(*) FROM EngineeringItems e "
                "JOIN PnPBase b ON e.PnPID=b.PnPID GROUP BY b.PnPClassName"
            ).fetchall())
        finally:
            tpl.close()
        classes = sorted(set(gen_counts) | set(tpl_counts))
        result["counts"] = {
            c: {"gen": gen_counts.get(c, 0), "tpl": tpl_counts.get(c, 0),
                "match": gen_counts.get(c, 0) == tpl_counts.get(c, 0)}
            for c in classes
        }
        result["counts_all_match"] = all(v["match"] for v in result["counts"].values())

        # GUID blobs are 16 bytes.
        guid_ok = guid_bad = 0
        for table, col in [("PnPBase", "PnPGuid"), ("PartPort", "PnPGuid"),
                           ("EngineeringItems", "SizeRecordId"),
                           ("EngineeringItems", "PartFamilyId"),
                           ("EngineeringItems", "CatalogPartFamilyId"),
                           ("Port", "SizeRecordId"), ("PnPDatabase", "DBID")]:
            for (val,) in con.execute(f'SELECT "{col}" FROM "{table}"').fetchall():
                if val is None:
                    continue
                if isinstance(val, (bytes, bytearray)) and len(val) == 16:
                    guid_ok += 1
                else:
                    guid_bad += 1
        result["guid_16byte_ok"] = guid_ok
        result["guid_16byte_bad"] = guid_bad

        # Graph: no orphans.
        orphans = {
            "partport_part_missing_ei": con.execute(
                "SELECT COUNT(*) FROM PartPort WHERE Part NOT IN "
                "(SELECT PnPID FROM EngineeringItems)").fetchone()[0],
            "partport_port_missing_port": con.execute(
                "SELECT COUNT(*) FROM PartPort WHERE Port NOT IN "
                "(SELECT PnPID FROM Port)").fetchone()[0],
            "port_missing_base": con.execute(
                "SELECT COUNT(*) FROM Port WHERE PnPID NOT IN "
                "(SELECT PnPID FROM PnPBase WHERE PnPClassName='Port')").fetchone()[0],
            "relation_rowid_missing_ei": con.execute(
                "SELECT COUNT(*) FROM PnPRowRelations WHERE ROWID NOT IN "
                "(SELECT PnPID FROM EngineeringItems)").fetchone()[0],
            "relation_relid_missing_pp": con.execute(
                "SELECT COUNT(*) FROM PnPRowRelations WHERE RELID NOT IN "
                "(SELECT PnPID FROM PartPort)").fetchone()[0],
            "ei_missing_base": con.execute(
                "SELECT COUNT(*) FROM EngineeringItems WHERE PnPID NOT IN "
                "(SELECT PnPID FROM PnPBase)").fetchone()[0],
            "tee_not_in_singlebranch": con.execute(
                "SELECT COUNT(*) FROM PnPBase b WHERE b.PnPClassName='Tee' AND b.PnPID NOT IN "
                "(SELECT PnPID FROM SingleBranchFitting)").fetchone()[0],
        }
        result["graph_orphans"] = orphans
        result["graph_consistent"] = all(v == 0 for v in orphans.values())

        # Auxiliary lookups copied.
        result["aux_lookups"] = {
            "ValveActuatorMap": con.execute("SELECT COUNT(*) FROM ValveActuatorMap").fetchone()[0],
            "StandardBoltLength": con.execute(
                "SELECT COUNT(*) FROM StandardBoltLength").fetchone()[0],
            "LookUps": con.execute("SELECT COUNT(*) FROM LookUps").fetchone()[0],
        }
    finally:
        con.close()

    # Branch table: regenerated vs original.
    result["branch_table"] = _verify_branch_table(defin)

    # .pspx structural checks.
    result["pspx"] = _verify_pspx(out_pspx, out_pspc)

    result["materialisation_report"] = mat.report
    _print_report(result)
    return result


def _verify_branch_table(defin: SpecDefinition) -> dict:
    """Regenerate the branch table, re-parse it and diff against the original definition."""
    regen = generate_branch_table_xml(defin)
    re_syms, re_cells = _parse_branch_table(regen)

    orig_z = zipfile.ZipFile(TEMPLATE_PSPX, "r")
    try:
        orig_syms, orig_cells = _parse_branch_table(orig_z.read("content/branchtable.xml"))
    finally:
        orig_z.close()

    def sym_key(s: BranchSymbol):
        return (s.name, s.description, tuple(s.part_references))

    def cell_key(c: BranchCell):
        return (c.header, c.branch, c.symbol)

    orig_sym_set = {sym_key(s) for s in orig_syms}
    re_sym_set = {sym_key(s) for s in re_syms}
    orig_cell_set = {cell_key(c) for c in orig_cells}
    re_cell_set = {cell_key(c) for c in re_cells}

    # Each symbol's PartFamilyId should resolve to a real family. We check, in bytes_le text form
    # (the encoding branchtable.xml uses -- verified, same as EngineeringItems / PartUsePriorities),
    # against both the referenced catalogs AND the spec's own EngineeringItems. A family present in
    # the spec but absent from every catalog signals catalog version drift, not a generation bug.
    cat_family_texts = _collect_catalog_family_texts()
    spec_family_texts = _collect_template_family_texts()
    missing_family = []        # absent from BOTH catalogs and the spec
    only_in_spec_family = []   # catalog drift: in the spec but not in the local catalogs
    for s in re_syms:
        for part_type, _fam_name, fam_id in s.part_references:
            if not fam_id:
                continue
            in_cat = fam_id.lower() in cat_family_texts
            in_spec = fam_id.lower() in spec_family_texts
            if not in_cat and not in_spec:
                missing_family.append((s.name, part_type, fam_id))
            elif not in_cat and in_spec:
                only_in_spec_family.append((s.name, part_type, fam_id))

    return {
        "symbols_original": len(orig_syms),
        "symbols_generated": len(re_syms),
        "symbols_match": orig_sym_set == re_sym_set,
        "symbols_only_in_original": sorted(s[0] for s in (orig_sym_set - re_sym_set)),
        "symbols_only_in_generated": sorted(s[0] for s in (re_sym_set - orig_sym_set)),
        "cells_original": len(orig_cells),
        "cells_generated": len(re_cells),
        "cells_match": orig_cell_set == re_cell_set,
        "cells_diff_count": len(orig_cell_set ^ re_cell_set),
        "symbol_families_missing_everywhere": missing_family,
        "symbol_families_only_in_spec_catalog_drift": only_in_spec_family,
    }


def _collect_template_family_texts() -> set[str]:
    """Set of every PartFamilyId in the template spec's EngineeringItems (bytes_le text)."""
    texts: set[str] = set()
    con = ro_connect(TEMPLATE_PSPC)
    try:
        for (fb,) in con.execute(
            "SELECT DISTINCT PartFamilyId FROM EngineeringItems WHERE PartFamilyId IS NOT NULL"
        ).fetchall():
            if isinstance(fb, (bytes, bytearray)) and len(fb) == 16:
                texts.add(blob_to_guid_text(fb).lower())
    finally:
        con.close()
    return texts


def _collect_catalog_family_texts() -> set[str]:
    """Set of every PartFamilyId of every catalog, as lower-case bytes_le GUID text."""
    texts: set[str] = set()
    for fname in CATALOGS.values():
        path = os.path.join(SCRATCH, fname)
        if not os.path.exists(path):
            continue
        con = ro_connect(path)
        try:
            for (fb,) in con.execute(
                "SELECT DISTINCT PartFamilyId FROM EngineeringItems WHERE PartFamilyId IS NOT NULL"
            ).fetchall():
                if isinstance(fb, (bytes, bytearray)) and len(fb) == 16:
                    texts.add(blob_to_guid_text(fb).lower())
        finally:
            con.close()
    return texts


def _verify_pspx(out_pspx: str, out_pspc: str) -> dict:
    info = {"opens_as_zip": False, "parts": [], "parse_errors": [], "catalog_count": None,
            "data_target": None, "data_target_ok": None}
    try:
        z = zipfile.ZipFile(out_pspx, "r")
        info["opens_as_zip"] = True
        for name in z.namelist():
            if name.lower().endswith((".xml", ".rels")):
                try:
                    ET.fromstring(z.read(name))
                    info["parts"].append(name)
                except ET.ParseError as exc:
                    info["parse_errors"].append(f"{name}: {exc}")
        cat_root = ET.fromstring(z.read("editor/CatalogReferences.xml"))
        info["catalog_count"] = len(cat_root.findall("EditorCatalogFileReference"))
        rns = "http://schemas.openxmlformats.org/package/2006/relationships"
        rels = ET.fromstring(z.read("_rels/.rels"))
        for rel in rels.findall(f"{{{rns}}}Relationship"):
            if rel.get("Type") == DATA_REL_TYPE:
                info["data_target"] = rel.get("Target")
        info["data_target_ok"] = info["data_target"] == os.path.basename(out_pspc)
        z.close()
    except Exception as exc:  # noqa: BLE001 - PoC reporting
        info["error"] = repr(exc)
    return info


def _print_report(r: dict) -> None:
    print("\n===== VERIFICACION: NXD-2-GEN (spec completa) =====")
    print(f"  pspc: {r['pspc']}")
    print(f"  integrity_check: {r['integrity_check']}")
    print(f"  GUID 16-byte OK={r['guid_16byte_ok']}  fallos={r['guid_16byte_bad']}")
    print(f"  grafo consistente: {r['graph_consistent']}  ({r['graph_orphans']})")
    print(f"  aux lookups: {r['aux_lookups']}")
    print("\n  Recuentos por familia (generado vs NXD-2 original):")
    print(f"    {'Clase':<18} {'gen':>5} {'tpl':>5}  ok")
    for cls, v in r["counts"].items():
        flag = "OK" if v["match"] else "DIFERENCIA"
        print(f"    {cls:<18} {v['gen']:>5} {v['tpl']:>5}  {flag}")
    print(f"  TODOS los recuentos cuadran: {r['counts_all_match']}")

    print("\n  Origen de materializacion por clase:")
    for cls, d in sorted(r["materialisation_report"].items()):
        if cls.startswith("_"):
            continue
        print(f"    {cls:<18} ok={d.get('ok',0):>3} unresolved={d.get('unresolved',0):>3} "
              f"origenes={d.get('origins',{})}")

    bt = r["branch_table"]
    print("\n  Branch table (generada vs original):")
    print(f"    simbolos: original={bt['symbols_original']} generado={bt['symbols_generated']} "
          f"identicos={bt['symbols_match']}")
    print(f"    celdas:   original={bt['cells_original']} generado={bt['cells_generated']} "
          f"identicas={bt['cells_match']} (diffs={bt['cells_diff_count']})")
    if bt["symbols_only_in_original"] or bt["symbols_only_in_generated"]:
        print(f"    simbolos solo en original: {bt['symbols_only_in_original']}")
        print(f"    simbolos solo en generado: {bt['symbols_only_in_generated']}")
    mf = bt["symbol_families_missing_everywhere"]
    drift = bt["symbol_families_only_in_spec_catalog_drift"]
    if mf:
        print(f"    ERROR: familias de simbolo no encontradas NI en catalogos NI en la spec "
              f"({len(mf)}):")
        for name, ptype, fid in mf:
            print(f"      - simbolo {name} ({ptype}) PartFamilyId={fid}")
    if drift:
        print(f"    AVISO (deriva de version de catalogo): {len(drift)} familias de simbolo "
              f"existen en NXD-2 pero NO en los .pcat locales (catalogos re-versionados):")
        for name, ptype, fid in drift:
            print(f"      - simbolo {name} ({ptype}) PartFamilyId={fid}")
    if not mf and not drift:
        print("    todas las familias de simbolo existen en algun catalogo referenciado: OK")

    p = r["pspx"]
    print(f"\n  pspx abre ZIP: {p['opens_as_zip']}  catalogos referenciados: {p['catalog_count']}")
    print(f"  pspx Data target: {p['data_target']} (ok={p['data_target_ok']})")
    print(f"  pspx XML parseadas: {len(p['parts'])}  errores: {p['parse_errors'] or 'ninguno'}")


# =========================================================================== main
def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    out_pspc = os.path.join(OUT_DIR, f"{OUT_NAME}.pspc")
    out_pspx = os.path.join(OUT_DIR, f"{OUT_NAME}.pspx")

    print("== Fase 1: derivar la definicion de spec desde NXD-2 (fuente de verdad) ==")
    defin = derive_definition_from_template()
    print(f"  componentes en definicion: {len(defin.components)}")
    print(f"  simbolos de branch table: {len(defin.branch_symbols)}  "
          f"celdas: {len(defin.branch_cells)}")

    print("\n== Fase 2: materializar cada componente desde los 6 catalogos ==")
    catalogs = CatalogIndex()
    print(f"  catalogos abiertos: {list(catalogs.handles)}")
    try:
        mat = Materialiser(out_pspc, defin, catalogs)
        mat.build()
        build_pspx(out_pspx, defin, out_pspc)
    finally:
        catalogs.close()

    verify(out_pspc, out_pspx, defin, mat)
    print("\nListo. Ficheros en:", OUT_DIR)


if __name__ == "__main__":
    main()
