"""Read-only queries over AutoCAD Plant 3D project databases.

A Plant 3D project stores its engineering data in SQLite databases with a
``.dcf`` extension (Piping.dcf, ProcessPower.dcf, ...). This module reads those
databases directly — no .NET plugin and no running AutoCAD session required.

All access is strictly read-only: databases are opened with SQLite's
``mode=ro`` URI flag so a query can never modify or lock the live project.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from autocad_mcp.config import PLANT3D_ROOT

# Magic bytes at the start of every SQLite 3 database file.
_SQLITE_MAGIC = b"SQLite format 3\x00"


# ---------------------------------------------------------------------------
# Project / database resolution
# ---------------------------------------------------------------------------


def _is_sqlite(path: Path) -> bool:
    """Return True if ``path`` looks like a SQLite database file."""
    try:
        with path.open("rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def resolve_project_dir(project: str | None) -> Path:
    """Resolve a project reference to its folder.

    ``project`` may be an absolute path to the project folder, a path to a
    ``.dcf`` file inside it, or — when ``AUTOCAD_MCP_PLANT3D_ROOT`` is set —
    a bare project name resolved relative to that root.
    """
    if not project:
        if not PLANT3D_ROOT:
            raise ValueError(
                "No se indicó proyecto y AUTOCAD_MCP_PLANT3D_ROOT no está configurado."
            )
        raise ValueError("Falta el nombre o la ruta del proyecto (data.project).")

    p = Path(project)
    if p.suffix.lower() == ".dcf":
        p = p.parent
    if not p.is_absolute() and PLANT3D_ROOT:
        p = Path(PLANT3D_ROOT) / project

    if not p.is_dir():
        raise FileNotFoundError(f"No existe la carpeta de proyecto: {p}")
    return p


def find_project_root(path_inside: str) -> Path:
    """Walk up from a path to the folder of the Plant 3D project it belongs to.

    ``path_inside`` is typically the folder of the active drawing (AutoCAD's
    ``DWGPREFIX``) or a full drawing path. A project drawing lives in a
    subfolder of the project (``PID DWG``, ``Plant 3D Models``, ...), so the
    project root is the nearest ancestor containing ``Project.xml``.
    """
    if not path_inside:
        raise ValueError("No se pudo determinar la ruta del dibujo activo.")
    p = Path(path_inside)
    if p.is_file():
        p = p.parent
    for d in (p, *p.parents):
        if (d / "Project.xml").is_file():
            return d
    raise FileNotFoundError(
        f"El dibujo activo ({path_inside}) no pertenece a un proyecto Plant 3D "
        "(no se encontró Project.xml en las carpetas superiores)."
    )


def project_info(project: str) -> dict:
    """Return basic identification of a resolved project."""
    d = resolve_project_dir(project)
    return {
        "ok": True,
        "name": d.name,
        "path": str(d),
        "has_piping": (d / "Piping.dcf").is_file(),
        "has_pid": (d / "ProcessPower.dcf").is_file(),
    }


def _db_path(project_dir: Path, db_name: str) -> Path:
    """Return a validated path to a project ``.dcf`` SQLite database."""
    path = project_dir / db_name
    if not path.is_file():
        raise FileNotFoundError(f"No se encontró {db_name} en {project_dir}")
    if not _is_sqlite(path):
        raise ValueError(f"{path} no es una base de datos SQLite válida.")
    return path


def _ro_uri(path: Path) -> str:
    """Build a SQLite read-only URI, handling Windows UNC and drive paths.

    A UNC path (``//host/share/...``) maps to ``file:////host/share/...`` —
    the empty authority plus a path that keeps its leading ``//``. A normal
    absolute path (``C:/...``) maps to ``file:///C:/...``. The path is
    percent-encoded so spaces and accented characters are URI-safe.
    """
    posix = path.as_posix()
    encoded = quote(posix)
    prefix = "file://" if posix.startswith("//") else "file:///"
    return f"{prefix}{encoded}?mode=ro"


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite database strictly read-only."""
    con = sqlite3.connect(_ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Public queries
# ---------------------------------------------------------------------------


def list_projects(root: str | None = None) -> dict:
    """List Plant 3D projects found under a root folder.

    A folder is treated as a project when it contains a ``Project.xml``.
    """
    base = Path(root or PLANT3D_ROOT)
    if not (root or PLANT3D_ROOT):
        raise ValueError(
            "Indica una carpeta raíz (data.root) o configura AUTOCAD_MCP_PLANT3D_ROOT."
        )
    if not base.is_dir():
        raise FileNotFoundError(f"No existe la carpeta raíz: {base}")

    projects = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "Project.xml").is_file():
            projects.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "has_piping": (child / "Piping.dcf").is_file(),
                    "has_pid": (child / "ProcessPower.dcf").is_file(),
                }
            )
    return {"ok": True, "root": str(base), "count": len(projects), "projects": projects}


def _fmt_size(value: float | int, unit: str | None) -> str:
    """Format a nominal diameter compactly (drop trailing .0)."""
    if value is None:
        return "?"
    num = int(value) if float(value).is_integer() else value
    unit = unit or ""
    if unit == "in":
        return f'{num}"'
    return f"{num} {unit}".strip()


def line_summary(project: str) -> dict:
    """Summarize the piping lines of a Plant 3D project.

    Groups every piping component by its ``LineNumberTag`` and reports, per
    line: component count, distinct spools, services, specs, and the nominal
    diameters in use (kept separate per unit so inches and millimetres are
    never mixed into a misleading range).
    """
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    con = _connect_ro(db)
    try:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT
                COALESCE(NULLIF(prc.LineNumberTag, ''), '(SIN LÍNEA)') AS line,
                prc.Service                                            AS service,
                prc.SpoolNumber                                        AS spool,
                ei.Spec                                                AS spec,
                ei.NominalDiameter                                     AS dia,
                ei.NominalUnit                                         AS unit
            FROM PipeRunComponent prc
            LEFT JOIN EngineeringItems ei ON ei.PnPID = prc.PnPID
            """
        ).fetchall()
    finally:
        con.close()

    lines: dict[str, dict] = {}
    for r in rows:
        agg = lines.setdefault(
            r["line"],
            {
                "line": r["line"],
                "components": 0,
                "_spools": set(),
                "_services": set(),
                "_specs": set(),
                "_sizes": {},  # (dia, unit) -> count
            },
        )
        agg["components"] += 1
        if r["spool"]:
            agg["_spools"].add(r["spool"])
        if r["service"]:
            agg["_services"].add(r["service"])
        if r["spec"]:
            agg["_specs"].add(r["spec"])
        if r["dia"] is not None:
            key = (r["dia"], r["unit"])
            agg["_sizes"][key] = agg["_sizes"].get(key, 0) + 1

    out = []
    for agg in lines.values():
        # Sizes sorted by descending frequency; representative size is the most common.
        sizes_sorted = sorted(agg["_sizes"].items(), key=lambda kv: (-kv[1], kv[0][0]))
        sizes = [_fmt_size(dia, unit) for (dia, unit), _ in sizes_sorted]
        out.append(
            {
                "line": agg["line"],
                "components": agg["components"],
                "spools": len(agg["_spools"]),
                "services": sorted(agg["_services"]),
                "specs": sorted(agg["_specs"]),
                "main_size": sizes[0] if sizes else None,
                "sizes": sizes,
            }
        )

    out.sort(key=lambda d: d["line"])
    untagged = next((l for l in out if l["line"] == "(SIN LÍNEA)"), None)
    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "line_count": len([l for l in out if l["line"] != "(SIN LÍNEA)"]),
        "component_count": sum(l["components"] for l in out),
        "untagged_components": untagged["components"] if untagged else 0,
        "lines": out,
    }


def find_untagged(project: str) -> dict:
    """List piping components that lack a line number tag.

    A component is considered untagged when its ``LineNumberTag`` is NULL or,
    once trimmed, is empty or a literal ``?``. (A plain ``LIKE '%?%'`` would
    wrongly catch valid tags that legitimately contain a question mark.)

    Returns every untagged component identified by its ``PnPID`` plus its
    engineering properties (class, description, spec, nominal size), together
    with breakdowns by class and by spec. Components cannot be located in the
    drawing from here — that needs the .NET plugin — so they are reported by
    PnPID and properties only.
    """
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    con = _connect_ro(db)
    try:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT
                prc.PnPID            AS pnpid,
                ei.PartCategory      AS class,
                ei.ShortDescription  AS description,
                ei.Spec              AS spec,
                ei.NominalDiameter   AS dia,
                ei.NominalUnit       AS unit
            FROM PipeRunComponent prc
            LEFT JOIN EngineeringItems ei ON ei.PnPID = prc.PnPID
            WHERE prc.LineNumberTag IS NULL
               OR TRIM(prc.LineNumberTag) IN ('', '?')
            ORDER BY ei.Spec, ei.PartCategory, prc.PnPID
            """
        ).fetchall()
    finally:
        con.close()

    by_class: dict[str, int] = {}
    by_spec: dict[str, int] = {}
    components = []
    for r in rows:
        cls_key = r["class"] if r["class"] is not None else "(sin clase)"
        by_class[cls_key] = by_class.get(cls_key, 0) + 1

        spec_key = r["spec"] if r["spec"] else "(sin spec)"
        by_spec[spec_key] = by_spec.get(spec_key, 0) + 1

        components.append(
            {
                "pnpid": r["pnpid"],
                "class": r["class"],
                "description": r["description"],
                "spec": r["spec"],
                "size": _fmt_size(r["dia"], r["unit"]) if r["dia"] is not None else None,
            }
        )

    def _ranked(counts: dict[str, int], key_name: str) -> list[dict]:
        return [
            {key_name: name, "count": count}
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "untagged_count": len(components),
        "by_class": _ranked(by_class, "class"),
        "by_spec": _ranked(by_spec, "spec"),
        "components": components,
        "note": (
            "La localización del objeto en el dibujo requiere el plugin .NET; "
            "aquí solo se identifica por PnPID y propiedades."
        ),
    }
