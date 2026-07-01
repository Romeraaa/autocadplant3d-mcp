"""Build a complete AutoCAD Plant 3D specification by selecting parts from catalogs.

Standalone proof of concept (NOT part of the MCP server). Standard library only
(``sqlite3, zipfile, uuid, xml.etree.ElementTree, os, shutil, tempfile``) plus :mod:`specgen.common`.

This is the generalised port of the original ``spec_builder.py`` PoC. The build is split into two
clearly separated phases:

* :class:`SpecDefinition` -- WHAT goes in the spec: the list of components (one :class:`ComponentRef`
  per part, carrying class + SizeRecordId + PartFamilyId), the branch table and the part-use
  priorities. The CLI builds this from the matched piping-class entries; the branch table comes from
  an optional template (``--template-pspc`` and its sibling ``.pspx``) or is emitted minimal/empty.
* :class:`Materialiser` -- HOW each component is realised: locate it in the right source database
  (by SizeRecordId) and copy its full PnP graph (PnPBase + type tables + Port + PartPort +
  PnPRowRelations) into the fresh ``.pspc`` with new, internally-consistent PnPIDs / PnPGuids,
  copying geometry and GUID BLOBs verbatim.

The ``.pspc`` needs a valid Plant 3D schema to open; that schema is taken from a *seed* database.
With a template we copy it and strip its component graph; without a template the seed is the
catalog that holds the first component (catalogs share the identical schema), stripped likewise.

GUID encoding (verified empirically, see :mod:`specgen.common`): blobs are ``uuid.uuid4().bytes_le``;
``PartFamilyId`` / branch-table / PartUsePriorities text forms are ``str(uuid.UUID(bytes_le=blob))``.

H4: the ``.pspc`` is written to a temporary path and atomically renamed into place; a partial file
is removed if anything raises. Inputs are opened strictly read-only.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

from . import common
from .catalog_index import CatalogIndex
from .common import blob_to_guid_text, columns, new_guid_blob, now_ticks, ro_connect, xml_escape

DATA_REL_TYPE = "Plant/Specification/Data"

# Component classes whose PnPID also gets a row in PipeRunComponent (run components).
NON_RUN_COMPONENT_CLASSES = {"Gasket", "BoltSet"}

# Auxiliary "lookup" tables: rows keyed by a PnPBase PnPID but NOT EngineeringItems components.
AUX_LOOKUP_TABLES = ["ValveActuatorMap", "StandardBoltLength"]

# Every component type table that may hold a row keyed by a component PnPID. Tables that do not
# exist in the seed schema are skipped gracefully.
COMPONENT_TYPE_TABLES = [
    "BlindDisk", "BlindFlange", "BoltSet", "Cap", "Coupling", "Elbow", "Flange", "Gasket",
    "Nipple", "Olet", "Pipe", "Reducer", "SingleBranchFitting", "SpacerDisk",
    "SpectacleBlind", "Swage", "Tee", "Valve", "ValveActuator", "ValveBody",
]

# Tables emptied to strip the seed down to schema + metadata before materialising.
_GRAPH_TABLES = ["EngineeringItems", "PipeRunComponent", "Port", "PartPort",
                 "PnPRowRelations", "LookUps"]


# =========================================================================== DEFINITION
@dataclass
class ComponentRef:
    """One part to place in the spec: class + identity keys."""

    class_name: str               # PnPClassName, e.g. 'Pipe', 'Valve', 'Tee'
    size_record_id: bytes | None  # primary catalog lookup key (16-byte GUID blob)
    part_family_id: bytes | None  # family GUID blob
    pnpid_source: int | None = None  # source PnPID hint (used only when no srid match)


@dataclass
class BranchSymbol:
    """A branch-table symbol: short name + the part(s) it resolves to."""

    name: str
    description: str
    part_references: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class BranchCell:
    """One run/branch combination mapped to a symbol name."""

    header: str
    branch: str
    symbol: str


@dataclass
class SpecDefinition:
    """The complete definition of a spec, independent of how parts are materialised."""

    name: str
    repository_id: str
    description: str
    components: list[ComponentRef] = field(default_factory=list)
    branch_symbols: list[BranchSymbol] = field(default_factory=list)
    branch_cells: list[BranchCell] = field(default_factory=list)
    # XML fragments copied verbatim from a template when available; sensible minimals otherwise.
    part_use_priorities_xml: bytes = b""
    spec_notes_xml: bytes = b""
    spec_sheet_settings_xml: bytes = b""
    content_types_xml: bytes = b""


# --------------------------------------------------------------------------- branch table
def parse_branch_table(raw: bytes) -> tuple[list[BranchSymbol], list[BranchCell]]:
    """Parse a branchtable.xml byte string into a structured (symbols, cells) representation."""
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


def load_template_xml(template_pspx: str) -> dict[str, bytes]:
    """Read the reusable XML parts from a template ``.pspx`` package (best-effort per part)."""
    parts: dict[str, bytes] = {}
    wanted = {
        "content/branchtable.xml": "branchtable",
        "content/PartUsePriorities.xml": "part_use_priorities",
        "content/SpecNotes.xml": "spec_notes",
        "content/SpecSheetSettings.xml": "spec_sheet_settings",
        "[Content_Types].xml": "content_types",
    }
    with zipfile.ZipFile(template_pspx, "r") as z:
        names = set(z.namelist())
        for path, key in wanted.items():
            if path in names:
                parts[key] = z.read(path)
    return parts


def _minimal_branch_table() -> bytes:
    ns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    ns_xsd = "http://www.w3.org/2001/XMLSchema"
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<SpecificationBranchTable xmlns:xsi="{ns_xsi}" xmlns:xsd="{ns_xsd}">\n'
        "  <BranchSymbols />\n"
        "  <Branches />\n"
        "</SpecificationBranchTable>"
    ).encode("utf-8")


def _minimal_xml(root: str) -> bytes:
    ns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    ns_xsd = "http://www.w3.org/2001/XMLSchema"
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n<{root} xmlns:xsi="{ns_xsi}" '
        f'xmlns:xsd="{ns_xsd}" />'
    ).encode("utf-8")


_MINIMAL_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    '  <Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml" />\n'
    '  <Default Extension="xml" ContentType="application/xml" />\n'
    "</Types>"
).encode("utf-8")


def make_definition(
    *,
    name: str,
    description: str,
    components: list[ComponentRef],
    template_pspx: str | None = None,
) -> SpecDefinition:
    """Assemble a :class:`SpecDefinition` for ``components``.

    The branch table and the verbatim XML fragments come from ``template_pspx`` when given; without
    a template a minimal (empty) branch table and minimal XML fragments are emitted so the spec
    still opens (no branch routing until the engineer fills the table).
    """
    defin = SpecDefinition(
        name=name,
        repository_id=common.new_repository_id(),
        description=description,
        components=components,
        part_use_priorities_xml=_minimal_xml("SpecPartUsePriorities"),
        spec_notes_xml=_minimal_xml("SpecNotes"),
        spec_sheet_settings_xml=_minimal_xml("SpecSheetSettings"),
        content_types_xml=_MINIMAL_CONTENT_TYPES,
    )
    if template_pspx and os.path.exists(template_pspx):
        parts = load_template_xml(template_pspx)
        if "branchtable" in parts:
            defin.branch_symbols, defin.branch_cells = parse_branch_table(parts["branchtable"])
        defin.part_use_priorities_xml = parts.get(
            "part_use_priorities", defin.part_use_priorities_xml)
        defin.spec_notes_xml = parts.get("spec_notes", defin.spec_notes_xml)
        defin.spec_sheet_settings_xml = parts.get(
            "spec_sheet_settings", defin.spec_sheet_settings_xml)
        defin.content_types_xml = parts.get("content_types", defin.content_types_xml)
    return defin


# =========================================================================== MATERIALISER
class Materialiser:
    """Writes the fresh .pspc and copies each component's PnP graph into it.

    ``seed_pspc`` is a database with the right Plant 3D schema: a template ``.pspc`` (its component
    graph is stripped) or a catalog ``.pcat`` (schema is identical; its graph is stripped too).
    Parts are sourced from the :class:`CatalogIndex` by SizeRecordId.
    """

    def __init__(
        self,
        out_pspc: str,
        defin: SpecDefinition,
        catalogs: CatalogIndex,
        *,
        seed_pspc: str,
        template_pspc: str | None = None,
    ) -> None:
        self.out_pspc = out_pspc
        self.defin = defin
        self.catalogs = catalogs
        self.seed_pspc = seed_pspc
        self.template_pspc = template_pspc
        self.con: sqlite3.Connection | None = None
        self.next_base = 0
        self.next_rel = 0
        self.ts = now_ticks()
        self._cols: dict[str, list[str]] = {}
        self._dstset_cache: set[str] | None = None
        self.report: dict[str, dict] = {}

    # ------------------------------------------------------------------ infra
    def _dst_cols(self, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = columns(self.con, table)
        return self._cols[table]

    def _dst_set(self) -> set[str]:
        if self._dstset_cache is None:
            self._dstset_cache = common.table_names(self.con)
        return self._dstset_cache

    def _insert(self, table: str, row: dict) -> None:
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
        """Materialise the spec atomically (H4: temp file + rename, partial removed on error)."""
        out_dir = os.path.dirname(os.path.abspath(self.out_pspc)) or "."
        os.makedirs(out_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".pspc", dir=out_dir)
        os.close(fd)
        try:
            shutil.copyfile(self.seed_pspc, tmp)
            self.con = sqlite3.connect(tmp)
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
            os.replace(tmp, self.out_pspc)   # atomic rename into place
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _empty_component_graph(self) -> None:
        """Strip the seed down to schema + metadata, keeping only RepositoryDescriptor."""
        cur = self.con.cursor()
        present = self._dst_set()
        for t in _GRAPH_TABLES + COMPONENT_TYPE_TABLES + AUX_LOOKUP_TABLES:
            if t in present:
                cur.execute(f'DELETE FROM "{t}"')
        if "PnPBase" in present:
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
        """Copy ValveActuatorMap / StandardBoltLength verbatim from the template if one exists."""
        n = 0
        if self.template_pspc and os.path.exists(self.template_pspc):
            src = ro_connect(self.template_pspc)
            try:
                src_tables = common.table_names(src)
                for table in AUX_LOOKUP_TABLES:
                    if table not in self._dst_set() or table not in src_tables:
                        continue
                    cols = columns(src, table)
                    for row in src.execute(f'SELECT * FROM "{table}"').fetchall():
                        d = dict(zip(cols, row))
                        new_id = self._alloc_base()
                        d["PnPID"] = new_id
                        self._add_base_row(new_id, table)
                        self._insert(table, d)
                        if "LookUps" in self._dst_set():
                            self.con.execute(
                                "INSERT INTO LookUps (PnPID, PortName) VALUES (?, ?)",
                                (new_id, None),
                            )
                        n += 1
            finally:
                src.close()
        self.report["_aux_lookups"] = {"rows": n}

    # ------------------------------------------------------------------ component
    def _materialise_component(self, comp: ComponentRef) -> None:
        """Locate the component and copy its full PnP graph with fresh ids."""
        logical = self.catalogs.find(comp.size_record_id)
        src: sqlite3.Connection | None = None
        own_src = False
        if logical is not None:
            src = self.catalogs.handles[logical]
            src_pnpid = self._catalog_pnpid_for(src, comp)
            origin = logical
        elif self.template_pspc and os.path.exists(self.template_pspc):
            src = ro_connect(self.template_pspc)
            own_src = True
            src_pnpid = comp.pnpid_source
            origin = "TEMPLATE"
        else:
            self._bump(comp.class_name, "unresolved", "NONE")
            return
        try:
            if src_pnpid is None:
                self._bump(comp.class_name, "unresolved", origin)
                return
            self._copy_graph(src, src_pnpid, comp.class_name)
            self._bump(comp.class_name, "ok", origin)
        finally:
            if own_src and src is not None:
                src.close()

    def _catalog_pnpid_for(self, src: sqlite3.Connection, comp: ComponentRef) -> int | None:
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
        new_comp = self._alloc_base()
        self._add_base_row(new_comp, class_name)

        ei_src_cols = columns(src, "EngineeringItems")
        ei_row = src.execute(
            "SELECT * FROM EngineeringItems WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()
        if ei_row is None:
            return
        ei = dict(zip(ei_src_cols, ei_row))
        ei["PnPID"] = new_comp
        self._insert("EngineeringItems", ei)

        if class_name not in NON_RUN_COMPONENT_CLASSES and "PipeRunComponent" in self._dst_set():
            prc_row = self._maybe_row(src, "PipeRunComponent", src_pnpid)
            if prc_row is not None:
                prc_row["PnPID"] = new_comp
                self._insert("PipeRunComponent", prc_row)

        for table in COMPONENT_TYPE_TABLES:
            if table not in self._dst_set():
                continue
            row = self._maybe_row(src, table, src_pnpid)
            if row is not None:
                row["PnPID"] = new_comp
                self._insert(table, row)

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
                "INSERT INTO PnPRowRelations (ROWID, RELID, RelationshipTypeName) VALUES (?,?,?)",
                (new_comp, new_pp, "PartPort"),
            )

    @staticmethod
    def _maybe_row(src: sqlite3.Connection, table: str, pnpid: int) -> dict | None:
        if table not in common.table_names(src):
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
        if "PnPDatabase" in self._dst_set():
            self.con.execute("UPDATE PnPDatabase SET DBID=?", (new_guid_blob(),))


# =========================================================================== .pspx (package)
def build_pspx(out_pspx: str, defin: SpecDefinition, data_target: str,
               references: list[tuple[str, str]]) -> None:
    """Write the .pspx package with a generated branchtable.xml and the catalog references."""
    with zipfile.ZipFile(out_pspx, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", defin.content_types_xml)
        z.writestr("_rels/.rels", _rels_xml(data_target))
        z.writestr("editor/CatalogReferences.xml", _catalog_references_xml(references))
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
        parts.append(f'<Relationship Type="{rtype}" Target="{target}"{mode_attr} Id="{rid}" />')
    parts.append("</Relationships>")
    return "".join(parts).encode("utf-8")


def _catalog_references_xml(references: list[tuple[str, str]]) -> bytes:
    """Reference each catalog by its logical name and absolute path (from the CatalogIndex)."""
    ns_xsd = "http://www.w3.org/2001/XMLSchema"
    ns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<EditorCatalogFileReferences xmlns:xsd="{ns_xsd}" xmlns:xsi="{ns_xsi}">',
    ]
    for logical, path in references:
        lines.append("  <EditorCatalogFileReference>")
        lines.append(f"    <Name>{xml_escape(logical)}</Name>")
        lines.append(f"    <Reference>{xml_escape(path)}</Reference>")
        lines.append("  </EditorCatalogFileReference>")
    lines.append("</EditorCatalogFileReferences>")
    return "\n".join(lines).encode("utf-8")


def generate_branch_table_xml(defin: SpecDefinition) -> bytes:
    """Build branchtable.xml from the structured SpecDefinition."""
    ns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    ns_xsd = "http://www.w3.org/2001/XMLSchema"
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<SpecificationBranchTable xmlns:xsi="{ns_xsi}" xmlns:xsd="{ns_xsd}">',
        "  <BranchSymbols>",
    ]
    for sym in defin.branch_symbols:
        out.append("    <BranchSymbol>")
        out.append(f"      <Name>{xml_escape(sym.name)}</Name>")
        out.append(f"      <Description>{xml_escape(sym.description)}</Description>")
        out.append("      <BranchPartReferences>")
        for part_type, fam_name, fam_id in sym.part_references:
            out.append("        <BranchPartReference>")
            out.append(
                f'          <PartReference PartType="{xml_escape(part_type)}" '
                f'PartFamilyName="{xml_escape(fam_name)}" '
                f'PartFamilyId="{xml_escape(fam_id)}" />'
            )
            out.append("          <Notes />")
            out.append("        </BranchPartReference>")
        out.append("      </BranchPartReferences>")
        out.append("    </BranchSymbol>")
    out.append("  </BranchSymbols>")
    out.append("  <Branches>")
    for cell in defin.branch_cells:
        out.append("    <BranchTableItem>")
        out.append(f'      <Header Units="in" Value="{xml_escape(cell.header)}" />')
        out.append(f'      <Branch Units="in" Value="{xml_escape(cell.branch)}" />')
        out.append("      <BranchOptions>")
        out.append(f"        <BranchSymbol>{xml_escape(cell.symbol)}</BranchSymbol>")
        out.append("      </BranchOptions>")
        out.append("    </BranchTableItem>")
    out.append("  </Branches>")
    out.append("</SpecificationBranchTable>")
    return "\n".join(out).encode("utf-8")


# =========================================================================== VERIFY
def verify(out_pspc: str, out_pspx: str, mat: Materialiser) -> dict:
    """Run programmatic checks and return a structured report (no external oracle required)."""
    result: dict = {"pspc": out_pspc, "pspx": out_pspx}
    con = ro_connect(out_pspc)
    try:
        result["integrity_check"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        result["counts"] = dict(con.execute(
            "SELECT b.PnPClassName, COUNT(*) FROM EngineeringItems e "
            "JOIN PnPBase b ON e.PnPID=b.PnPID GROUP BY b.PnPClassName"
        ).fetchall())
        result["component_total"] = sum(result["counts"].values())

        guid_ok = guid_bad = 0
        for table, col in [("PnPBase", "PnPGuid"), ("PartPort", "PnPGuid"),
                           ("EngineeringItems", "SizeRecordId"),
                           ("EngineeringItems", "PartFamilyId")]:
            if table not in common.table_names(con):
                continue
            for (val,) in con.execute(f'SELECT "{col}" FROM "{table}"').fetchall():
                if val is None:
                    continue
                if isinstance(val, (bytes, bytearray)) and len(val) == 16:
                    guid_ok += 1
                else:
                    guid_bad += 1
        result["guid_16byte_ok"] = guid_ok
        result["guid_16byte_bad"] = guid_bad

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
            "relation_relid_missing_pp": con.execute(
                "SELECT COUNT(*) FROM PnPRowRelations WHERE RELID NOT IN "
                "(SELECT PnPID FROM PartPort)").fetchone()[0],
            "ei_missing_base": con.execute(
                "SELECT COUNT(*) FROM EngineeringItems WHERE PnPID NOT IN "
                "(SELECT PnPID FROM PnPBase)").fetchone()[0],
        }
        result["graph_orphans"] = orphans
        result["graph_consistent"] = all(v == 0 for v in orphans.values())
    finally:
        con.close()

    result["pspx"] = _verify_pspx(out_pspx, out_pspc)
    result["materialisation_report"] = mat.report
    return result


def _verify_pspx(out_pspx: str, out_pspc: str) -> dict:
    info = {"opens_as_zip": False, "parts": [], "parse_errors": [], "catalog_count": None,
            "data_target": None, "data_target_ok": None}
    try:
        with zipfile.ZipFile(out_pspx, "r") as z:
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
    except Exception as exc:  # noqa: BLE001 - PoC reporting
        info["error"] = repr(exc)
    return info
