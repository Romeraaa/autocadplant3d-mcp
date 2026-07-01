"""PoC: generate an AutoCAD Plant 3D specification (.pspc + .pspx) from a valid template.

This is a *standalone proof of concept*, NOT part of the MCP server. It uses only the
Python standard library (sqlite3, zipfile, uuid, xml.etree.ElementTree, datetime, shutil).

Two tracks are implemented:

* Track A (subsetting): take a known-valid spec (NXD-2.pspc/.pspx) and produce a new spec
  (POC-PIPE) that keeps ONLY the Pipe family (class 'Pipe' and its Port/PartPort graph),
  with a fresh RepositoryID and Name. This maximises the chance Plant 3D opens it and proves
  we can author a .pspc.

* Track B (sourcing from catalog): build pipe rows by SELECTING them from the source catalog
  REPSOL_TUBERIA.pcat by SizeRecordId (one TUBO family, several diameters), copying their rows
  into a fresh spec with new PnPIDs / PnPGuids and the schema metadata of the template. This
  proves the real catalog -> spec path.

Input files are opened strictly read-only (sqlite3 uri mode=ro). Outputs go to ./out/.

Documented assumptions (see README.md):
  * GUID BLOBs are 16-byte .NET-ordered values. The text form used in the .pspx XML matches
    uuid.UUID(bytes_le=blob); new GUID blobs are uuid.uuid4().bytes_le for consistency.
  * PnPTimestamp is .NET ticks (100-ns intervals since 0001-01-01).
  * The branch table is emitted empty for the subset (robust; branches mix part types).
  * PnPBase PnPIDs and relationship (PartPort) PnPIDs live in separate ID spaces.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

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
SOURCE_PCAT = os.path.join(SCRATCH, "REPSOL_TUBERIA.pcat")

# Component-type tables in the template that are NOT Pipe. Emptied for both tracks.
# (Their PnPBase rows are removed by the PnPBase prune; we still DELETE the rows here.)
NON_PIPE_COMPONENT_TABLES = [
    "BlindDisk", "BlindFlange", "BoltSet", "Cap", "Coupling", "Elbow",
    "Fasteners", "Flange", "Gasket", "Nipple", "Olet", "Reducer",
    "SingleBranchFitting", "SpacerDisk", "SpectacleBlind", "Swage", "Tee",
    "Valve", "ValveActuator", "ValveActuatorMap", "StandardBoltLength",
]

# OPC part name for the external spec data relationship.
DATA_REL_TYPE = "Plant/Specification/Data"


# --------------------------------------------------------------------------- helpers
def ro_connect(path: str) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def new_guid_blob() -> bytes:
    """Return a fresh 16-byte GUID blob in the byte order observed in the template (bytes_le)."""
    return uuid.uuid4().bytes_le


def blob_to_guid_text(blob: bytes) -> str:
    """Convert a 16-byte GUID blob to the textual GUID used in the .pspx XML."""
    return str(uuid.UUID(bytes_le=blob))


def now_ticks() -> int:
    """Current time as .NET ticks (100-ns intervals since 0001-01-01)."""
    dt = datetime.now(timezone.utc).replace(tzinfo=None)
    return int((dt - datetime(1, 1, 1)).total_seconds() * 1e7)


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of a table."""
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]


# --------------------------------------------------------------------------- Track A
def build_track_a(out_pspc: str) -> dict:
    """Produce POC-PIPE.pspc by copying the template and pruning everything that is not Pipe.

    Strategy: copy the template (inheriting all schema and metadata tables intact), then open
    the copy read-write and DELETE non-Pipe data, keeping the Pipe component graph.
    """
    shutil.copyfile(TEMPLATE_PSPC, out_pspc)
    con = sqlite3.connect(out_pspc)
    try:
        cur = con.cursor()

        keep_components = {
            r[0] for r in cur.execute(
                "SELECT PnPID FROM PnPBase WHERE PnPClassName='Pipe'"
            ).fetchall()
        }
        if not keep_components:
            raise RuntimeError("Template has no Pipe-class components")

        ph = ",".join("?" * len(keep_components))
        comp = tuple(keep_components)

        keep_partport = {
            r[0] for r in cur.execute(
                f"SELECT PnPID FROM PartPort WHERE Part IN ({ph})", comp
            ).fetchall()
        }
        keep_ports = {
            r[0] for r in cur.execute(
                f"SELECT Port FROM PartPort WHERE Part IN ({ph})", comp
            ).fetchall()
        }

        # PnPBase: keep Pipe components, their Ports, and the RepositoryDescriptor row.
        keep_base = keep_components | keep_ports
        base_ph = ",".join("?" * len(keep_base))
        cur.execute(
            f"DELETE FROM PnPBase WHERE PnPID NOT IN ({base_ph}) "
            f"AND PnPClassName <> 'RepositoryDescriptor'",
            tuple(keep_base),
        )

        # Component metadata / type tables.
        cur.execute(f"DELETE FROM EngineeringItems WHERE PnPID NOT IN ({ph})", comp)
        cur.execute(f"DELETE FROM PipeRunComponent WHERE PnPID NOT IN ({ph})", comp)
        cur.execute(f"DELETE FROM Pipe WHERE PnPID NOT IN ({ph})", comp)

        for tbl in NON_PIPE_COMPONENT_TABLES:
            cur.execute(f'DELETE FROM "{tbl}"')

        # Ports and relationships.
        if keep_ports:
            port_ph = ",".join("?" * len(keep_ports))
            cur.execute(f"DELETE FROM Port WHERE PnPID NOT IN ({port_ph})", tuple(keep_ports))
        else:
            cur.execute("DELETE FROM Port")

        if keep_partport:
            pp_ph = ",".join("?" * len(keep_partport))
            cur.execute(f"DELETE FROM PartPort WHERE PnPID NOT IN ({pp_ph})", tuple(keep_partport))
        else:
            cur.execute("DELETE FROM PartPort")

        # All PnPRowRelations are of type 'PartPort'; keep only those linking kept rows.
        cur.execute(
            f"DELETE FROM PnPRowRelations WHERE NOT (ROWID IN ({ph}) AND RELID IN "
            f"({','.join('?' * len(keep_partport)) or 'NULL'}))",
            comp + tuple(keep_partport),
        )

        # LookUps in this template are bolt/actuator lookups only -> irrelevant to Pipe.
        cur.execute("DELETE FROM LookUps")

        # New identity for the spec.
        cur.execute(
            "UPDATE RepositoryDescriptor SET Name=?, RepositoryID=?, Description=?",
            (
                "POC-PIPE",
                "{" + str(uuid.uuid4()) + "}",
                "PoC subsetting tuberia generado por script (Track A)",
            ),
        )
        cur.execute("UPDATE PnPDatabase SET DBID=?", (new_guid_blob(),))

        con.commit()
        cur.execute("VACUUM")
        con.commit()
    finally:
        con.close()

    return {"keep_components": len(keep_components)}


# --------------------------------------------------------------------------- Track B
# One catalog family with several diameters. Chosen for the PoC.
TRACK_B_FAMILY_DESC = "PIPE, SEAMLESS, PE, ASME B36.10"
TRACK_B_DIAMETERS = [0.5, 0.75, 1.0, 2.0]  # inches


def build_track_b(out_pspc: str) -> dict:
    """Produce POC-PIPE-FROMCAT.pspc by sourcing pipe rows from the catalog.

    Starts from a copy of the template emptied of all components, then inserts pipe rows
    selected from REPSOL_TUBERIA.pcat by family + nominal diameter, building a fresh
    component -> Port -> PartPort -> relation graph with new PnPIDs and PnPGuids.
    """
    shutil.copyfile(TEMPLATE_PSPC, out_pspc)
    con = sqlite3.connect(out_pspc)
    inserted = 0
    try:
        cur = con.cursor()

        # 1) Empty the entire component graph (reuse the same pruning idea, but keep nothing).
        cur.execute("DELETE FROM EngineeringItems")
        cur.execute("DELETE FROM PipeRunComponent")
        cur.execute("DELETE FROM Pipe")
        for tbl in NON_PIPE_COMPONENT_TABLES:
            cur.execute(f'DELETE FROM "{tbl}"')
        cur.execute("DELETE FROM Port")
        cur.execute("DELETE FROM PartPort")
        cur.execute("DELETE FROM PnPRowRelations")
        cur.execute("DELETE FROM LookUps")
        cur.execute("DELETE FROM PnPBase WHERE PnPClassName <> 'RepositoryDescriptor'")

        # 2) Counters (separate ID spaces). Start above any value the template ever used.
        base_max = cur.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence WHERE name='PnPSys_PnPBase_PnPID'"
        ).fetchone()[0]
        rel_max = cur.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence "
            "WHERE name='PnPSys_RelationshipSystem_PnPID'"
        ).fetchone()[0]
        next_base = (base_max or 0) + 1
        next_rel = (rel_max or 0) + 1

        # 3) Read source rows from the catalog (read-only).
        pcat = ro_connect(SOURCE_PCAT)
        try:
            ei_cols_src = columns(pcat, "EngineeringItems")
            d_ph = ",".join("?" * len(TRACK_B_DIAMETERS))
            src_rows = pcat.execute(
                f"SELECT * FROM EngineeringItems "
                f"WHERE PartFamilyLongDesc=? AND NominalDiameter IN ({d_ph}) "
                f"ORDER BY NominalDiameter",
                (TRACK_B_FAMILY_DESC, *TRACK_B_DIAMETERS),
            ).fetchall()
            cat_name = (
                pcat.execute(
                    "SELECT Name FROM RepositoryDescriptor LIMIT 1"
                ).fetchone() or (TRACK_B_FAMILY_DESC,)
            )[0]
        finally:
            pcat.close()

        ei_cols_dst = columns(con, "EngineeringItems")
        ts = now_ticks()

        for src in src_rows:
            srcd = dict(zip(ei_cols_src, src))

            comp_id = next_base
            next_base += 1
            port_id = next_base
            next_base += 1
            partport_id = next_rel
            next_rel += 1

            # PnPBase for component (class Pipe) and Port.
            cur.execute(
                "INSERT INTO PnPBase (PnPID, PnPClassName, PnPStatus, PnPRevision, PnPGuid, "
                "PnPTimestamp) VALUES (?,?,?,?,?,?)",
                (comp_id, "Pipe", 0, 1, new_guid_blob(), ts),
            )
            cur.execute(
                "INSERT INTO PnPBase (PnPID, PnPClassName, PnPStatus, PnPRevision, PnPGuid, "
                "PnPTimestamp) VALUES (?,?,?,?,?,?)",
                (port_id, "Port", 0, 1, new_guid_blob(), ts),
            )

            # EngineeringItems: copy every shared column verbatim (incl. GUID blobs, geometry).
            ei = {c: srcd.get(c) for c in ei_cols_dst}
            ei["PnPID"] = comp_id
            # In the catalog, the family id is PartFamilyId; the spec carries it as
            # CatalogPartFamilyId and reuses it as PartFamilyId for the PoC.
            ei["CatalogPartFamilyId"] = srcd.get("PartFamilyId")
            if ei.get("PartFamilyId") is None:
                ei["PartFamilyId"] = srcd.get("PartFamilyId")
            if not ei.get("CatalogId"):
                ei["CatalogId"] = cat_name
            cols_ins = ",".join(f'"{c}"' for c in ei_cols_dst)
            cur.execute(
                f"INSERT INTO EngineeringItems ({cols_ins}) "
                f"VALUES ({','.join('?' * len(ei_cols_dst))})",
                tuple(ei[c] for c in ei_cols_dst),
            )

            cur.execute("INSERT INTO PipeRunComponent (PnPID, Shop_Field) VALUES (?, ?)",
                        (comp_id, ""))
            linear_weight = srcd.get("Weight")
            cur.execute(
                "INSERT INTO Pipe (PnPID, Length, UseFixedLength, CutLength, MinCutLength, "
                "LinearWeight, LinearWeightUnit) VALUES (?,?,?,?,?,?,?)",
                (comp_id, 0.0, 0, None, None, linear_weight, "LB/FT"),
            )

            port_name = srcd.get("PortName") or "S1"
            cur.execute(
                "INSERT INTO Port (PnPID, SizeRecordId, PortName, NominalDiameter, NominalUnit, "
                "MatchingPipeOd, EndType, FlangeStd, GasketStd, Facing, FlangeThickness, "
                "PressureClass, Schedule, WallThickness, EngagementLength, LengthUnit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    port_id, srcd.get("SizeRecordId"), port_name,
                    srcd.get("NominalDiameter"), srcd.get("NominalUnit"),
                    srcd.get("MatchingPipeOd"), srcd.get("EndType"), srcd.get("FlangeStd"),
                    srcd.get("GasketStd"), srcd.get("Facing"), srcd.get("FlangeThickness"),
                    srcd.get("PressureClass"), srcd.get("Schedule"), srcd.get("WallThickness"),
                    srcd.get("EngagementLength"), srcd.get("LengthUnit"),
                ),
            )

            # PartPort (relationship object, NOT in PnPBase) + relation row.
            cur.execute(
                "INSERT INTO PartPort (PnPID, PnPGuid, PnPTimestamp, Part, Port, Name) "
                "VALUES (?,?,?,?,?,?)",
                (partport_id, new_guid_blob(), ts, comp_id, port_id, port_name),
            )
            cur.execute(
                "INSERT INTO PnPRowRelations (ROWID, RELID, RelationshipTypeName) "
                "VALUES (?,?,?)",
                (comp_id, partport_id, "PartPort"),
            )
            inserted += 1

        cur.execute(
            "UPDATE RepositoryDescriptor SET Name=?, RepositoryID=?, Description=?",
            (
                "POC-PIPE-FROMCAT",
                "{" + str(uuid.uuid4()) + "}",
                "PoC sourcing desde catalogo REPSOL_TUBERIA (Track B)",
            ),
        )
        cur.execute("UPDATE PnPDatabase SET DBID=?", (new_guid_blob(),))

        con.commit()
        cur.execute("VACUUM")
        con.commit()
    finally:
        con.close()

    return {"inserted": inserted, "family": TRACK_B_FAMILY_DESC,
            "diameters": TRACK_B_DIAMETERS}


# --------------------------------------------------------------------------- .pspx
def build_pspx(out_pspx: str, data_target: str) -> None:
    """Write a .pspx package next to the new .pspc.

    Copies every part of the template package, but:
      * rewrites _rels/.rels so the external Data target points to ``data_target``,
      * filters content/PartUsePriorities.xml to keep only PartType == 'Pipe',
      * emits an empty content/branchtable.xml (robust: branches mix part types).
    """
    zin = zipfile.ZipFile(TEMPLATE_PSPX, "r")
    try:
        with zipfile.ZipFile(out_pspx, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in zin.namelist():
                raw = zin.read(name)
                low = name.lower()
                if low == "_rels/.rels":
                    raw = _rewrite_rels(raw, data_target)
                elif low == "content/partusepriorities.xml":
                    raw = _filter_part_use_priorities(raw)
                elif low == "content/branchtable.xml":
                    raw = _empty_branch_table(raw)
                zout.writestr(name, raw)
    finally:
        zin.close()


def _rewrite_rels(raw: bytes, data_target: str) -> bytes:
    """Point the Plant/Specification/Data relationship to the new .pspc filename."""
    text = raw.decode("utf-8")
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.register_namespace("", ns)
    root = ET.fromstring(text)
    for rel in root.findall(f"{{{ns}}}Relationship"):
        if rel.get("Type") == DATA_REL_TYPE:
            rel.set("Target", os.path.basename(data_target))
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="utf-8"?>' + body).encode("utf-8")


def _filter_part_use_priorities(raw: bytes) -> bytes:
    """Keep only PartTypeUsePriority entries whose PartType is 'Pipe'."""
    text = raw.decode("utf-8")
    ns = "http://www.w3.org/2001/XMLSchema-instance"  # not the default; root has no prefix ns
    root = ET.fromstring(text)
    # Root: SpecificationPartUsePriorities > PartUsePriorites > PartTypeUsePriority*
    container = root.find("PartUsePriorites")
    if container is not None:
        for entry in list(container.findall("PartTypeUsePriority")):
            pt = entry.find("PartType")
            if pt is None or (pt.text or "").strip() != "Pipe":
                container.remove(entry)
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + body).encode("utf-8")


def _empty_branch_table(raw: bytes) -> bytes:
    """Return an empty but well-formed branch table, preserving the namespaces.

    Clears both <BranchSymbols> (symbol definitions, which reference non-Pipe part families)
    and <Branches> (the table cells that reference those symbols by name).
    """
    text = raw.decode("utf-8")
    root = ET.fromstring(text)
    for container_name in ("BranchSymbols", "Branches"):
        container = root.find(container_name)
        if container is not None:
            for child in list(container):
                container.remove(child)
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + body).encode("utf-8")


# --------------------------------------------------------------------------- verify
GUID_BLOB_CHECKS = [
    ("PnPBase", "PnPGuid"),
    ("PartPort", "PnPGuid"),
    ("EngineeringItems", "SizeRecordId"),
    ("EngineeringItems", "PartFamilyId"),
    ("EngineeringItems", "CatalogPartFamilyId"),
    ("Port", "SizeRecordId"),
    ("PnPDatabase", "DBID"),
]

COUNT_TABLES = [
    "EngineeringItems", "PipeRunComponent", "Pipe", "Port", "PartPort",
    "PnPBase", "PnPRowRelations", "LookUps",
] + NON_PIPE_COMPONENT_TABLES


def verify(pspc: str, pspx: str, label: str) -> dict:
    """Run programmatic checks on a generated spec and print a report. Returns a dict."""
    con = ro_connect(pspc)
    result: dict = {"label": label, "pspc": pspc, "pspx": pspx}
    try:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        result["integrity_check"] = integ

        counts = {}
        for t in COUNT_TABLES:
            counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        result["counts"] = counts

        # Only-Pipe check.
        bad_class = con.execute(
            "SELECT COUNT(*) FROM PnPBase b JOIN EngineeringItems e ON b.PnPID=e.PnPID "
            "WHERE b.PnPClassName <> 'Pipe'"
        ).fetchone()[0]
        result["engineering_items_non_pipe"] = bad_class

        # GUID 16-byte checks.
        guid_ok, guid_bad = 0, 0
        for table, col in GUID_BLOB_CHECKS:
            for (val,) in con.execute(f'SELECT "{col}" FROM "{table}"').fetchall():
                if val is None:
                    continue
                if isinstance(val, (bytes, bytearray)) and len(val) == 16:
                    guid_ok += 1
                else:
                    guid_bad += 1
        result["guid_16byte_ok"] = guid_ok
        result["guid_16byte_bad"] = guid_bad

        # Graph consistency.
        orphans = {}
        orphans["partport_part_missing_base"] = con.execute(
            "SELECT COUNT(*) FROM PartPort p WHERE p.Part NOT IN (SELECT PnPID FROM PnPBase)"
        ).fetchone()[0]
        orphans["partport_part_missing_ei"] = con.execute(
            "SELECT COUNT(*) FROM PartPort p WHERE p.Part NOT IN "
            "(SELECT PnPID FROM EngineeringItems)"
        ).fetchone()[0]
        orphans["partport_port_missing_port"] = con.execute(
            "SELECT COUNT(*) FROM PartPort p WHERE p.Port NOT IN (SELECT PnPID FROM Port)"
        ).fetchone()[0]
        orphans["partport_port_not_class_port"] = con.execute(
            "SELECT COUNT(*) FROM PartPort p WHERE p.Port NOT IN "
            "(SELECT PnPID FROM PnPBase WHERE PnPClassName='Port')"
        ).fetchone()[0]
        orphans["relation_rowid_missing_component"] = con.execute(
            "SELECT COUNT(*) FROM PnPRowRelations r WHERE r.ROWID NOT IN "
            "(SELECT PnPID FROM PipeRunComponent)"
        ).fetchone()[0]
        orphans["relation_relid_missing_partport"] = con.execute(
            "SELECT COUNT(*) FROM PnPRowRelations r WHERE r.RELID NOT IN "
            "(SELECT PnPID FROM PartPort)"
        ).fetchone()[0]
        orphans["ports_without_partport"] = con.execute(
            "SELECT COUNT(*) FROM Port WHERE PnPID NOT IN (SELECT Port FROM PartPort)"
        ).fetchone()[0]
        result["graph_orphans"] = orphans
        result["graph_consistent"] = all(v == 0 for v in orphans.values())
    finally:
        con.close()

    # .pspx checks.
    pspx_info = {"opens_as_zip": False, "parts_parsed": [], "parse_errors": [],
                 "data_target": None, "data_target_ok": None}
    try:
        z = zipfile.ZipFile(pspx, "r")
        pspx_info["opens_as_zip"] = True
        for name in z.namelist():
            if name.lower().endswith(".xml") or name.lower().endswith(".rels"):
                try:
                    ET.fromstring(z.read(name))
                    pspx_info["parts_parsed"].append(name)
                except ET.ParseError as exc:
                    pspx_info["parse_errors"].append(f"{name}: {exc}")
        rels = ET.fromstring(z.read("_rels/.rels"))
        rns = "http://schemas.openxmlformats.org/package/2006/relationships"
        for rel in rels.findall(f"{{{rns}}}Relationship"):
            if rel.get("Type") == DATA_REL_TYPE:
                pspx_info["data_target"] = rel.get("Target")
        pspx_info["data_target_ok"] = (
            pspx_info["data_target"] == os.path.basename(pspc)
        )
        z.close()
    except Exception as exc:  # noqa: BLE001 - PoC reporting
        pspx_info["error"] = repr(exc)
    result["pspx"] = pspx_info

    _print_report(result)
    return result


def _print_report(r: dict) -> None:
    print(f"\n===== VERIFICACION: {r['label']} =====")
    print(f"  pspc: {r['pspc']}")
    print(f"  integrity_check: {r['integrity_check']}")
    c = r["counts"]
    print(f"  Pipe={c['Pipe']}  EngineeringItems={c['EngineeringItems']}  "
          f"PipeRunComponent={c['PipeRunComponent']}  Port={c['Port']}  "
          f"PartPort={c['PartPort']}  PnPRowRelations={c['PnPRowRelations']}")
    print(f"  PnPBase={c['PnPBase']}  LookUps={c['LookUps']}")
    non_pipe_nonzero = {t: c[t] for t in NON_PIPE_COMPONENT_TABLES if c[t] != 0}
    print(f"  tablas no-Pipe con filas (debe estar vacio): "
          f"{non_pipe_nonzero or 'ninguna (OK)'}")
    print(f"  EngineeringItems no-Pipe (debe ser 0): {r['engineering_items_non_pipe']}")
    print(f"  GUID 16-byte OK={r['guid_16byte_ok']}  fallos={r['guid_16byte_bad']}")
    print(f"  grafo consistente: {r['graph_consistent']}  ({r['graph_orphans']})")
    p = r["pspx"]
    print(f"  pspx abre ZIP: {p['opens_as_zip']}  Data target: {p.get('data_target')} "
          f"(ok={p.get('data_target_ok')})")
    print(f"  pspx XML parseadas: {len(p.get('parts_parsed', []))}  "
          f"errores: {p.get('parse_errors') or 'ninguno'}")


# --------------------------------------------------------------------------- main
def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- Track A -----------------------------------------------------------
    a_pspc = os.path.join(OUT_DIR, "POC-PIPE.pspc")
    a_pspx = os.path.join(OUT_DIR, "POC-PIPE.pspx")
    print("== Track A: subsetting de NXD-2 -> solo Pipe ==")
    info_a = build_track_a(a_pspc)
    build_pspx(a_pspx, a_pspc)
    print(f"  componentes Pipe conservados: {info_a['keep_components']}")
    res_a = verify(a_pspc, a_pspx, "Track A (POC-PIPE)")

    track_a_ok = (
        res_a["integrity_check"] == "ok"
        and res_a["graph_consistent"]
        and res_a["guid_16byte_bad"] == 0
        and res_a["engineering_items_non_pipe"] == 0
        and res_a["pspx"]["opens_as_zip"]
        and not res_a["pspx"]["parse_errors"]
    )

    # ---- Track B (only if A is clean) -------------------------------------
    if track_a_ok:
        b_pspc = os.path.join(OUT_DIR, "POC-PIPE-FROMCAT.pspc")
        b_pspx = os.path.join(OUT_DIR, "POC-PIPE-FROMCAT.pspx")
        print("\n== Track B: sourcing desde REPSOL_TUBERIA.pcat ==")
        info_b = build_track_b(b_pspc)
        build_pspx(b_pspx, b_pspc)
        print(f"  familia: {info_b['family']}  diametros: {info_b['diameters']}  "
              f"filas insertadas: {info_b['inserted']}")
        verify(b_pspc, b_pspx, "Track B (POC-PIPE-FROMCAT)")
    else:
        print("\n== Track B OMITIDO: Track A no quedo limpio ==")

    print("\nListo. Ficheros en:", OUT_DIR)


if __name__ == "__main__":
    main()
