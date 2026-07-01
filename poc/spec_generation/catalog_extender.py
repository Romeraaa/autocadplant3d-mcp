"""Extend REPSOL Plant 3D catalogs (.pcat, SQLite) with the missing hydrogen (-H2) variants.

Standalone proof of concept (NOT part of the MCP server). Standard library only:
``sqlite3, uuid, shutil, re, datetime, struct`` (the GUID/ticks primitives are reused from
``generate_spec_poc``, which depend on ``uuid`` / ``datetime`` only).

Problem: the piping class needs 35 ``L-xxxx-H2`` (hydrogen service) L-codes that do NOT exist in
the catalogs -- only their base L-code is present, so the matcher resolves them as SUSTITUCION /
BAJA. This tool CLONES each base part family into a brand-new ``-H2`` family inside a COPY of the
catalog, so every H2 entry of the piping class now matches a real, dedicated family.

Mechanism (already validated empirically against the REPSOL .pcat catalogs, not re-discovered):
  * A part FAMILY == every ``EngineeringItems`` row sharing one ``PartFamilyId`` (16-byte GUID
    BLOB). Each row (one size) has its own ``SizeRecordId`` (16-byte GUID BLOB). The REPSOL L-code
    lives at the end of ``PartFamilyLongDesc`` (and inside ``PartSizeLongDesc``); there is no
    Material column.
  * Per component there is a graph: ``PnPBase`` (PnPClassName, PnPGuid, PnPTimestamp .NET ticks),
    ``EngineeringItems``, one (Tee: two) component-type tables keyed by PnPID, ``Port`` (1..N, each
    also a ``PnPBase`` row of class 'Port'), ``PartPort`` (Part=part PnPID, Port=port PnPID; its own
    PnPID lives in the *relationship* id space), and ``PnPRowRelations`` (ROWID=part PnPID,
    RELID=PartPort PnPID, type 'PartPort'). ``PipeRunComponent`` keys run components by PnPID.
  * Two id counters in ``sqlite_sequence``: ``PnPSys_PnPBase_PnPID`` (parts + ports) and
    ``PnPSys_RelationshipSystem_PnPID`` (PartPort). New ids start above both so they never collide.

GUID encoding (consistent with the rest of this PoC, verified): new BLOBs are ``uuid.uuid4()
.bytes_le``; ``PnPTimestamp`` is .NET ticks (``now_ticks``).

The component-type table set is discovered DYNAMICALLY per part (every non-system table that has a
PnPID column and a row for that PnPID), so the cloner is uniform across Pipe / Fitting / Valve /
Flange / Gasket / Bolt without a hard-coded class->table map. The cloner copies EVERYTHING verbatim
(geometry, dimensions, ports, ISO symbol, schedule, class, ...) and changes ONLY text: ``L-xxxx`` ->
``L-xxxx-H2`` plus the REPSOL hydrogen-service tagline.

Inputs are NEVER modified: every catalog is copied into ``out/catalogs_h2/`` first and only the copy
is written. Inputs are read strictly read-only.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3

# Reuse the proven primitives (GUID blob == bytes_le, .NET ticks).
from generate_spec_poc import SCRATCH, new_guid_blob, now_ticks, columns

# --------------------------------------------------------------------------- paths
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
OUT_CATALOGS_DIR = os.path.join(OUT_DIR, "catalogs_h2")

# Hydrogen-service tagline appended to the cloned family/size descriptions, REPSOL bilingual style.
H2_TAGLINE = " - PARA SERVICIO DE HIDROGENO / FOR HYDROGEN SERVICE"

# Map each catalog file -> the base L-codes whose -H2 variant must be created in it.
H2_TARGETS: dict[str, list[str]] = {
    "REPSOL_TUBERIA.pcat": [
        "L-1005", "L-1276", "L-212", "L-6314", "L-7448", "L-6030", "L-6301",
    ],
    "REPSOL_ACCESORIOS_SW.pcat": [
        "L-1097", "L-1098", "L-1099", "L-1100", "L-1101", "L-1104", "L-1238",
        "L-453", "L-458", "L-463", "L-469", "L-474",
    ],
    "REPSOL_ACCESORIOS_BW.pcat": ["L-1629", "L-1632"],
    "REPSOL_BRIDAS_JUNTAS_PERNOS.pcat": [
        "L-1490", "L-1492", "L-1493", "L-1517", "L-6899", "L-8550",
    ],
    "REPSOL_VALVULAS.pcat": [
        "L-1452", "L-1453", "L-1454", "L-1712", "L-1718", "L-1746", "L-9120",
    ],
}

# Tables never treated as component-type tables even though they carry a PnPID column.
_SYSTEM_TABLES = {
    "PnPBase", "EngineeringItems", "PipeRunComponent", "Port", "PartPort",
    "PnPRowRelations", "LookUps", "RepositoryDescriptor", "PnPDatabase",
    "ValveActuatorMap", "StandardBoltLength",
}


# --------------------------------------------------------------------------- helpers
def _lcode_in_desc(desc: str | None, lcode: str) -> bool:
    """True if ``lcode`` appears in ``desc`` as a whole token (e.g. 'L-453' but not 'L-4530').

    The base L-code must not be the prefix of a longer number; this rejects 'L-4530' when looking
    for 'L-453'. An existing '-H2' suffix on the token also makes it NOT a base match.
    """
    if not desc:
        return False
    if re.search(re.escape(lcode) + r"\d", desc) is not None:
        return False
    return re.search(r"\b" + re.escape(lcode) + r"\b(?!-)", desc) is not None


def _h2_text(text: str | None, lcode: str) -> str | None:
    """Return ``text`` with the base L-code rewritten to the -H2 variant + the H2 tagline.

    The L-code token is replaced by ``<lcode>-H2``; the tagline is appended once.
    """
    if text is None:
        return None
    new = re.sub(r"\b" + re.escape(lcode) + r"\b", lcode + "-H2", text)
    if H2_TAGLINE.strip() not in new:
        new = new + H2_TAGLINE
    return new


def _type_tables(con: sqlite3.Connection) -> list[str]:
    """Every user table (excluding system/relationship tables) that has a PnPID column.

    These are the component-type tables (Pipe, Elbow, Tee, Valve, ValveBody, Flange, Gasket,
    BoltSet, ...). A part lives in one of them (a Tee lives in two); the cloner copies whichever
    actually hold a row for the part's PnPID.
    """
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
        # reporting
        self.families_created = 0
        self.rows_created = 0
        self.lcode_families: dict[str, int] = {}

    # ------------------------------------------------------------------ infra
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

    # ------------------------------------------------------------------ run
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
        # Distinct family ids whose description carries the base L-code (1..2 families).
        fam_ids: list[bytes] = []
        seen: set[bytes] = set()
        for fid, desc in self.con.execute(
            "SELECT PartFamilyId, PartFamilyLongDesc FROM EngineeringItems "
            "WHERE PartFamilyLongDesc IS NOT NULL"
        ).fetchall():
            if fid in seen:
                continue
            if _lcode_in_desc(desc, lcode):
                seen.add(fid)
                fam_ids.append(fid)
        if not fam_ids:
            return
        for fid in fam_ids:
            self._clone_family(fid, lcode)
            self.families_created += 1
            self.lcode_families[lcode] = self.lcode_families.get(lcode, 0) + 1

    def _clone_family(self, family_id: bytes, lcode: str) -> None:
        """Clone all rows (sizes) of one base family into a new -H2 family."""
        new_family_id = new_guid_blob()  # shared by every row of the cloned family
        src_pnpids = [
            r[0]
            for r in self.con.execute(
                "SELECT PnPID FROM EngineeringItems WHERE PartFamilyId = ? ORDER BY PnPID",
                (family_id,),
            ).fetchall()
        ]
        for src_pnpid in src_pnpids:
            self._clone_part(src_pnpid, lcode, new_family_id)
            self.rows_created += 1

    def _clone_part(self, src_pnpid: int, lcode: str, new_family_id: bytes) -> None:
        """Clone one part (size) and its full graph with fresh ids; H2-rewrite only text."""
        new_pnpid = self._alloc_base()

        # PnPBase (part).
        base = dict(zip(self._c("PnPBase"), self.con.execute(
            "SELECT * FROM PnPBase WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()))
        base["PnPID"] = new_pnpid
        base["PnPGuid"] = new_guid_blob()
        base["PnPTimestamp"] = self.ts
        self._insert("PnPBase", base)

        # EngineeringItems: copy verbatim, new ids, new family id, H2 text.
        ei = dict(zip(self._c("EngineeringItems"), self.con.execute(
            "SELECT * FROM EngineeringItems WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()))
        ei["PnPID"] = new_pnpid
        ei["SizeRecordId"] = new_guid_blob()
        ei["PartFamilyId"] = new_family_id
        ei["CatalogPartFamilyId"] = new_family_id
        ei["PartFamilyLongDesc"] = _h2_text(ei.get("PartFamilyLongDesc"), lcode)
        # PartSizeLongDesc only carries the L-code in some catalogs; rewrite if present.
        if ei.get("PartSizeLongDesc") and lcode in ei["PartSizeLongDesc"]:
            ei["PartSizeLongDesc"] = _h2_text(ei["PartSizeLongDesc"], lcode)
        self._insert("EngineeringItems", ei)

        # PipeRunComponent (run components; keyed by PnPID, may be a single-column marker row).
        prc = self.con.execute(
            "SELECT * FROM PipeRunComponent WHERE PnPID = ?", (src_pnpid,)
        ).fetchone()
        if prc is not None:
            d = dict(zip(self._c("PipeRunComponent"), prc))
            d["PnPID"] = new_pnpid
            self._insert("PipeRunComponent", d)

        # Component-type tables holding this PnPID (Pipe / Elbow / Tee+SingleBranchFitting / ...).
        for table in self._type_tables:
            row = self.con.execute(
                f'SELECT * FROM "{table}" WHERE PnPID = ?', (src_pnpid,)
            ).fetchone()
            if row is not None:
                d = dict(zip(self._c(table), row))
                d["PnPID"] = new_pnpid
                self._insert(table, d)

        # Ports + PartPort + PnPRowRelations.
        for pp_row in self.con.execute(
            "SELECT * FROM PartPort WHERE Part = ?", (src_pnpid,)
        ).fetchall():
            pp = dict(zip(self._c("PartPort"), pp_row))
            src_port = pp["Port"]
            new_port = self._alloc_base()

            # Port also has a PnPBase row of class 'Port'.
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
                "INSERT INTO PnPRowRelations (ROWID, RELID, RelationshipTypeName) "
                "VALUES (?,?,?)",
                (new_pnpid, new_pp, "PartPort"),
            )


# --------------------------------------------------------------------------- driver
def extend_all() -> dict[str, CatalogExtender]:
    """Copy every affected catalog into out/catalogs_h2/ and create its -H2 families on the copy."""
    os.makedirs(OUT_CATALOGS_DIR, exist_ok=True)
    extenders: dict[str, CatalogExtender] = {}
    for fname, lcodes in H2_TARGETS.items():
        src = os.path.join(SCRATCH, fname)
        dst = os.path.join(OUT_CATALOGS_DIR, fname)
        shutil.copyfile(src, dst)  # NEVER touch the input; work on the copy
        ext = CatalogExtender(dst, lcodes)
        ext.run()
        extenders[fname] = ext
    return extenders


# --------------------------------------------------------------------------- verify
def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def verify(extenders: dict[str, CatalogExtender]) -> dict:
    """Per-catalog integrity, count and graph-consistency checks on the extended copies."""
    report: dict = {}
    for fname, ext in extenders.items():
        dst = os.path.join(OUT_CATALOGS_DIR, fname)
        con = _ro(dst)
        try:
            r: dict = {}
            r["integrity_check"] = con.execute("PRAGMA integrity_check").fetchone()[0]
            r["families_created"] = ext.families_created
            r["rows_created"] = ext.rows_created

            # Every expected L-xxxx-H2 now appears in PartFamilyLongDesc.
            present, absent = [], []
            for lcode in H2_TARGETS[fname]:
                h2 = lcode + "-H2"
                n = con.execute(
                    "SELECT COUNT(*) FROM EngineeringItems WHERE PartFamilyLongDesc LIKE ?",
                    (f"%{h2}%",),
                ).fetchone()[0]
                (present if n > 0 else absent).append(f"{h2}({n})")
            r["h2_present"] = present
            r["h2_absent"] = absent

            # New rows == rows whose desc carries '-H2'.
            r["h2_rows_total"] = con.execute(
                "SELECT COUNT(*) FROM EngineeringItems WHERE PartFamilyLongDesc LIKE '%-H2%'"
            ).fetchone()[0]
            r["h2_families_total"] = con.execute(
                "SELECT COUNT(DISTINCT PartFamilyId) FROM EngineeringItems "
                "WHERE PartFamilyLongDesc LIKE '%-H2%'"
            ).fetchone()[0]

            # GUID blobs of the new H2 rows are 16 bytes.
            bad = 0
            for col in ("PartFamilyId", "CatalogPartFamilyId", "SizeRecordId"):
                for (v,) in con.execute(
                    f"SELECT {col} FROM EngineeringItems WHERE PartFamilyLongDesc LIKE '%-H2%'"
                ).fetchall():
                    if v is not None and not (isinstance(v, (bytes, bytearray)) and len(v) == 16):
                        bad += 1
            r["guid_16byte_bad"] = bad

            # Graph consistency (whole catalog: no orphans introduced).
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
            r["graph_orphans"] = orphans
            r["graph_consistent"] = all(v == 0 for v in orphans.values())
            report[fname] = r
        finally:
            con.close()
    _print_report(report)
    return report


def _print_report(report: dict) -> None:
    print("\n===== VERIFICACION: catalogos ampliados con variantes -H2 =====")
    for fname, r in report.items():
        print(f"\n  {fname}")
        print(f"    integrity_check: {r['integrity_check']}")
        print(f"    familias H2 creadas: {r['families_created']}  filas nuevas: {r['rows_created']}")
        print(f"    familias H2 en catalogo: {r['h2_families_total']}  "
              f"filas H2: {r['h2_rows_total']}")
        print(f"    GUID 16-byte malos: {r['guid_16byte_bad']}")
        print(f"    grafo consistente: {r['graph_consistent']}  ({r['graph_orphans']})")
        if r["h2_absent"]:
            print(f"    *** L-codes H2 AUSENTES: {r['h2_absent']}")
        else:
            print(f"    todos los {len(r['h2_present'])} L-codes -H2 presentes: OK")


# --------------------------------------------------------------------------- main
def main() -> None:
    print("== Ampliacion de catalogos: clonado de familias base -> variantes -H2 ==")
    extenders = extend_all()
    tot_fam = sum(e.families_created for e in extenders.values())
    tot_rows = sum(e.rows_created for e in extenders.values())
    print(f"  catalogos ampliados: {len(extenders)}  familias H2: {tot_fam}  "
          f"filas nuevas: {tot_rows}")
    verify(extenders)
    print(f"\nListo. Copias ampliadas en: {OUT_CATALOGS_DIR}")


if __name__ == "__main__":
    main()
