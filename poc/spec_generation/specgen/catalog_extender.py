"""Extend Plant 3D catalogs (.pcat, SQLite) with the missing hydrogen (-H2) variant families.

Standalone proof of concept (NOT part of the MCP server). Standard library only plus
:mod:`specgen.common`.

Problem: a piping class needs ``L-xxxx-H2`` (hydrogen service) L-codes that do NOT exist in the
catalogs -- only their base L-code is present, so the matcher resolves them as SUSTITUCION / BAJA.
This tool CLONES each base part family into a brand-new ``-H2`` family inside a COPY of the catalog
that holds it, so every H2 entry of the piping class now matches a real, dedicated family.

Generalisation vs the original PoC: the set of ``-H2`` variants is NOT a hard-coded list. It is
DEDUCED from the parsed piping-class entries (every entry whose L-code carries the variant suffix
contributes its base L-code), and each base L-code is routed to whichever catalog file actually
contains a base family bearing it. Catalogs without any target are copied unchanged.

Mechanism (validated empirically against the REPSOL .pcat catalogs):
  * A part FAMILY == every ``EngineeringItems`` row sharing one ``PartFamilyId`` (16-byte blob).
    The L-code lives at the end of ``PartFamilyLongDesc`` (and inside ``PartSizeLongDesc``).
  * Per component there is a graph (PnPBase, EngineeringItems, one/two type tables, Port, PartPort,
    PnPRowRelations, PipeRunComponent). Two id counters in ``sqlite_sequence``.
  * The component-type table set is discovered DYNAMICALLY per part. The cloner copies EVERYTHING
    verbatim and changes ONLY text: ``L-xxxx`` -> ``L-xxxx-H2`` plus the hydrogen tagline.

Inputs are NEVER modified: every catalog is copied to the output dir first and only the copy is
written. Inputs are read strictly read-only.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3

from . import common
from .common import columns, lcode_in_desc, new_guid_blob, now_ticks, ro_connect

# Hydrogen-service tagline appended to the cloned family/size descriptions (REPSOL bilingual style).
H2_TAGLINE = " - PARA SERVICIO DE HIDROGENO / FOR HYDROGEN SERVICE"
H2_SUFFIX = "-H2"

# Tables never treated as component-type tables even though they carry a PnPID column.
_SYSTEM_TABLES = {
    "PnPBase", "EngineeringItems", "PipeRunComponent", "Port", "PartPort",
    "PnPRowRelations", "LookUps", "RepositoryDescriptor", "PnPDatabase",
    "ValveActuatorMap", "StandardBoltLength",
}


# --------------------------------------------------------------------------- planning
def deduce_h2_targets(entries, catalog_paths: dict[str, str]) -> dict[str, list[str]]:
    """Deduce ``{catalog_path: [base L-code, ...]}`` from variant entries + catalog content.

    For every piping-class entry whose L-code is a variant (``-H2``), the base L-code is collected.
    Each base L-code is then routed to whichever catalog file holds a base family carrying it (a
    family whose ``PartFamilyLongDesc`` mentions the base code as a whole token). A base code may
    legitimately resolve in more than one catalog (it is added to each).

    ``catalog_paths`` maps a catalog path to itself or any identifier; we use the path as key.
    """
    wanted_bases: set[str] = set()
    for e in entries:
        if getattr(e, "is_hydrogen", False) and e.lcode_base:
            wanted_bases.add(e.lcode_base)

    targets: dict[str, list[str]] = {p: [] for p in catalog_paths}
    if not wanted_bases:
        return targets
    for path in catalog_paths:
        con = ro_connect(path)
        try:
            if "EngineeringItems" not in common.table_names(con):
                continue
            descs = [
                d for (d,) in con.execute(
                    "SELECT DISTINCT PartFamilyLongDesc FROM EngineeringItems "
                    "WHERE PartFamilyLongDesc IS NOT NULL"
                ).fetchall()
            ]
        finally:
            con.close()
        for base in sorted(wanted_bases):
            if any(lcode_in_desc(d, base) for d in descs):
                targets[path].append(base)
    return targets


# --------------------------------------------------------------------------- helpers
def _h2_text(text: str | None, lcode: str) -> str | None:
    """Rewrite the base L-code to its -H2 variant and append the H2 tagline once."""
    if text is None:
        return None
    new = re.sub(r"\b" + re.escape(lcode) + r"\b", lcode + H2_SUFFIX, text)
    if H2_TAGLINE.strip() not in new:
        new = new + H2_TAGLINE
    return new


def _type_tables(con: sqlite3.Connection) -> list[str]:
    """Every user table (excluding system/relationship tables) that has a PnPID column."""
    out: list[str] = []
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'PnP%'"
    ).fetchall():
        if name in _SYSTEM_TABLES:
            continue
        if "PnPID" in columns(con, name):
            out.append(name)
    return out


# --------------------------------------------------------------------------- cloner
class CatalogExtender:
    """Clone the base families of a single catalog into fresh -H2 families, in place on a COPY."""

    def __init__(self, copy_path: str, lcodes: list[str]) -> None:
        self.path = copy_path
        self.lcodes = lcodes
        self.con: sqlite3.Connection | None = None
        self.ts = now_ticks()
        self.next_base = 0
        self.next_rel = 0
        self._cols: dict[str, list[str]] = {}
        self._type_tables: list[str] = []
        self.families_created = 0
        self.rows_created = 0
        self.lcode_families: dict[str, int] = {}

    def _c(self, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = columns(self.con, table)
        return self._cols[table]

    def _insert(self, table: str, row: dict) -> None:
        cols = [c for c in self._c(table) if c in row]
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

    def run(self) -> None:
        self.con = sqlite3.connect(self.path)
        try:
            self._init_counters()
            self._type_tables = _type_tables(self.con)
            for lcode in self.lcodes:
                self._clone_lcode(lcode)
            self._persist_counters()
            self.con.commit()
        finally:
            self.con.close()
            self.con = None

    def _init_counters(self) -> None:
        seqs = dict(self.con.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        self.next_base = int(seqs.get("PnPSys_PnPBase_PnPID", 0) or 0)
        self.next_rel = int(seqs.get("PnPSys_RelationshipSystem_PnPID", 0) or 0)

    def _persist_counters(self) -> None:
        for name, val in (
            ("PnPSys_PnPBase_PnPID", self.next_base),
            ("PnPSys_RelationshipSystem_PnPID", self.next_rel),
        ):
            cur = self.con.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name=?", (val, name)
            )
            if cur.rowcount == 0:
                self.con.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, val)
                )

    def _clone_lcode(self, lcode: str) -> None:
        """Clone every base family of ``lcode`` into a new -H2 family."""
        fam_ids: list[bytes] = []
        seen: set[bytes] = set()
        for fid, desc in self.con.execute(
            "SELECT PartFamilyId, PartFamilyLongDesc FROM EngineeringItems "
            "WHERE PartFamilyLongDesc IS NOT NULL"
        ).fetchall():
            if fid in seen:
                continue
            if lcode_in_desc(desc, lcode):
                seen.add(fid)
                fam_ids.append(fid)
        for fid in fam_ids:
            self._clone_family(fid, lcode)
            self.families_created += 1
            self.lcode_families[lcode] = self.lcode_families.get(lcode, 0) + 1

    def _clone_family(self, family_id: bytes, lcode: str) -> None:
        new_family_id = new_guid_blob()
        src_pnpids = [
            r[0] for r in self.con.execute(
                "SELECT PnPID FROM EngineeringItems WHERE PartFamilyId = ? ORDER BY PnPID",
                (family_id,),
            ).fetchall()
        ]
        for src_pnpid in src_pnpids:
            self._clone_part(src_pnpid, lcode, new_family_id)
            self.rows_created += 1

    def _clone_part(self, src_pnpid: int, lcode: str, new_family_id: bytes) -> None:
        new_pnpid = self._alloc_base()

        base = dict(zip(self._c("PnPBase"), self.con.execute(
            "SELECT * FROM PnPBase WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()))
        base["PnPID"] = new_pnpid
        base["PnPGuid"] = new_guid_blob()
        base["PnPTimestamp"] = self.ts
        self._insert("PnPBase", base)

        ei = dict(zip(self._c("EngineeringItems"), self.con.execute(
            "SELECT * FROM EngineeringItems WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()))
        ei["PnPID"] = new_pnpid
        ei["SizeRecordId"] = new_guid_blob()
        ei["PartFamilyId"] = new_family_id
        if "CatalogPartFamilyId" in ei:
            ei["CatalogPartFamilyId"] = new_family_id
        ei["PartFamilyLongDesc"] = _h2_text(ei.get("PartFamilyLongDesc"), lcode)
        if ei.get("PartSizeLongDesc") and lcode in ei["PartSizeLongDesc"]:
            ei["PartSizeLongDesc"] = _h2_text(ei["PartSizeLongDesc"], lcode)
        self._insert("EngineeringItems", ei)

        prc = self.con.execute(
            "SELECT * FROM PipeRunComponent WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()
        if prc is not None:
            d = dict(zip(self._c("PipeRunComponent"), prc))
            d["PnPID"] = new_pnpid
            self._insert("PipeRunComponent", d)

        for table in self._type_tables:
            row = self.con.execute(
                f'SELECT * FROM "{table}" WHERE PnPID = ?', (src_pnpid,)
            ).fetchone()
            if row is not None:
                d = dict(zip(self._c(table), row))
                d["PnPID"] = new_pnpid
                self._insert(table, d)

        for pp_row in self.con.execute(
            "SELECT * FROM PartPort WHERE Part = ?", (src_pnpid,)
        ).fetchall():
            pp = dict(zip(self._c("PartPort"), pp_row))
            src_port = pp["Port"]
            new_port = self._alloc_base()

            port_base = dict(zip(self._c("PnPBase"), self.con.execute(
                "SELECT * FROM PnPBase WHERE PnPID = ?", (src_port,)
            ).fetchone()))
            port_base["PnPID"] = new_port
            port_base["PnPGuid"] = new_guid_blob()
            port_base["PnPTimestamp"] = self.ts
            self._insert("PnPBase", port_base)

            port = dict(zip(self._c("Port"), self.con.execute(
                "SELECT * FROM Port WHERE PnPID = ?", (src_port,)
            ).fetchone()))
            port["PnPID"] = new_port
            port["SizeRecordId"] = new_guid_blob()
            self._insert("Port", port)

            new_pp = self._alloc_rel()
            self._insert("PartPort", {
                "PnPID": new_pp,
                "PnPGuid": new_guid_blob(),
                "PnPTimestamp": self.ts,
                "Part": new_pnpid,
                "Port": new_port,
                "Name": pp.get("Name"),
            })
            self.con.execute(
                "INSERT INTO PnPRowRelations (ROWID, RELID, RelationshipTypeName) VALUES (?,?,?)",
                (new_pnpid, new_pp, "PartPort"),
            )


# --------------------------------------------------------------------------- driver
def extend_catalogs(targets: dict[str, list[str]], out_dir: str) -> dict[str, CatalogExtender]:
    """Copy every catalog into ``out_dir`` and create its -H2 families on the copy.

    ``targets`` maps a source catalog path to the base L-codes whose -H2 variant must be created in
    it (from :func:`deduce_h2_targets`). Catalogs with an empty target list are copied unchanged so
    the output dir is a complete, self-consistent catalog set.
    """
    os.makedirs(out_dir, exist_ok=True)
    extenders: dict[str, CatalogExtender] = {}
    for src_path, lcodes in targets.items():
        fname = os.path.basename(src_path)
        dst = os.path.join(out_dir, fname)
        shutil.copyfile(src_path, dst)   # NEVER touch the input; work on the copy
        ext = CatalogExtender(dst, lcodes)
        if lcodes:
            ext.run()
        extenders[fname] = ext
    return extenders


def verify(extenders: dict[str, CatalogExtender], out_dir: str,
           targets_by_fname: dict[str, list[str]]) -> dict:
    """Per-catalog integrity, count and graph-consistency checks on the extended copies."""
    report: dict = {}
    for fname, ext in extenders.items():
        dst = os.path.join(out_dir, fname)
        con = ro_connect(dst)
        try:
            r: dict = {}
            r["integrity_check"] = con.execute("PRAGMA integrity_check").fetchone()[0]
            r["families_created"] = ext.families_created
            r["rows_created"] = ext.rows_created

            present, absent = [], []
            for lcode in targets_by_fname.get(fname, []):
                h2 = lcode + H2_SUFFIX
                n = con.execute(
                    "SELECT COUNT(*) FROM EngineeringItems WHERE PartFamilyLongDesc LIKE ?",
                    (f"%{h2}%",),
                ).fetchone()[0]
                (present if n > 0 else absent).append(f"{h2}({n})")
            r["h2_present"] = present
            r["h2_absent"] = absent

            r["h2_rows_total"] = con.execute(
                "SELECT COUNT(*) FROM EngineeringItems WHERE PartFamilyLongDesc LIKE '%-H2%'"
            ).fetchone()[0]
            r["h2_families_total"] = con.execute(
                "SELECT COUNT(DISTINCT PartFamilyId) FROM EngineeringItems "
                "WHERE PartFamilyLongDesc LIKE '%-H2%'"
            ).fetchone()[0]

            bad = 0
            for col in ("PartFamilyId", "SizeRecordId"):
                for (v,) in con.execute(
                    f"SELECT {col} FROM EngineeringItems WHERE PartFamilyLongDesc LIKE '%-H2%'"
                ).fetchall():
                    if v is not None and not (isinstance(v, (bytes, bytearray)) and len(v) == 16):
                        bad += 1
            r["guid_16byte_bad"] = bad

            orphans = {
                "partport_part_missing_ei": con.execute(
                    "SELECT COUNT(*) FROM PartPort WHERE Part NOT IN "
                    "(SELECT PnPID FROM EngineeringItems)").fetchone()[0],
                "partport_port_missing_port": con.execute(
                    "SELECT COUNT(*) FROM PartPort WHERE Port NOT IN "
                    "(SELECT PnPID FROM Port)").fetchone()[0],
                "relation_relid_missing_pp": con.execute(
                    "SELECT COUNT(*) FROM PnPRowRelations WHERE RELID NOT IN "
                    "(SELECT PnPID FROM PartPort)").fetchone()[0],
                "ei_missing_base": con.execute(
                    "SELECT COUNT(*) FROM EngineeringItems WHERE PnPID NOT IN "
                    "(SELECT PnPID FROM PnPBase)").fetchone()[0],
            }
            r["graph_orphans"] = orphans
            r["graph_consistent"] = all(v == 0 for v in orphans.values())
            report[fname] = r
        finally:
            con.close()
    return report
