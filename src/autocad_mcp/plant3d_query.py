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


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

# Specs auxiliares de Plant 3D (soportes, instrumentación, custom parts,
# placeholders): aparecen mezcladas con las specs de proceso de forma legítima
# y no tienen por qué disponer de un fichero .pspc propio. Se excluyen de las
# comprobaciones de "specs mezcladas" y "spec fantasma" para evitar falsos
# positivos. La lista es parametrizable vía data["ignore_specs"].
_DEFAULT_IGNORE_SPECS = (
    "PipeSupportsSpec",
    "PlaceHolder Metric",
    "PlaceHolder Imperial",
    "CustomParts Metric",
    "CustomParts Imperial",
    "Instrumentation Metric",
    "Instrumentation Imperial",
)

# Número de ejemplos por defecto que se devuelven para cada comprobación
# (las listas pueden ser muy largas; nunca se truncan en silencio).
_DEFAULT_LIMIT = 50


def _norm(value: str | None) -> str:
    """Normalize a spec/text value for case- and space-insensitive comparison."""
    return (value or "").strip().upper()


def _norm_tight(value: str | None) -> str:
    """Normalize removing ALL whitespace and upper-casing.

    Used for low-confidence material comparison, where catalogue values are
    inconsistently spaced (``ASTM A403 GrWP304/304L`` vs ``ASTM A403GrWP304/304L``).
    """
    return "".join((value or "").split()).upper()


def _spec_sheet_stems(project_dir: Path) -> set[str] | None:
    """Return the set of spec names (``.pspc`` file stems) in ``Spec Sheets``.

    Returns ``None`` when the ``Spec Sheets`` folder does not exist, so callers
    can degrade gracefully (the local catalogue is simply not available).
    The ``.pspx`` files are ZIP archives and are ignored.
    """
    sheets_dir = project_dir / "Spec Sheets"
    if not sheets_dir.is_dir():
        return None
    return {p.stem for p in sheets_dir.glob("*.pspc")}


def _read_spec_catalogue(pspc_path: Path) -> dict[str, set[str]]:
    """Read the allowed Schedule and Material sets from a ``.pspc`` catalogue.

    Each ``.pspc`` is an independent SQLite database whose ``EngineeringItems``
    table lists the parts permitted by that spec. Returns the distinct, non-null
    ``Schedule`` and ``Material`` values found, normalized for comparison.
    """
    con = _connect_ro(pspc_path)
    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT Schedule, Material FROM EngineeringItems"
        ).fetchall()
    finally:
        con.close()

    schedules: set[str] = set()
    materials: set[str] = set()
    for r in rows:
        if r["Schedule"] is not None and str(r["Schedule"]).strip():
            schedules.add(_norm(str(r["Schedule"])))
        if r["Material"] is not None and str(r["Material"]).strip():
            materials.add(_norm_tight(str(r["Material"])))
    return {"schedules": schedules, "materials": materials}


def _capped(items: list, limit: int) -> tuple[list, int]:
    """Return ``items`` capped to ``limit`` plus the number omitted.

    A ``limit`` of 0 or below means no cap (return everything).
    """
    if limit and limit > 0 and len(items) > limit:
        return items[:limit], len(items) - limit
    return items, 0


def validate_specs(project: str, data: dict | None = None) -> dict:
    """Validate piping specs of a Plant 3D project against its data and catalogue.

    Runs four read-only checks over ``Piping.dcf`` and the local ``Spec Sheets``
    catalogue (``.pspc`` databases):

    * **mismatched_spec** — components whose actual ``EngineeringItems.Spec``
      differs from the ``"Required Spec"`` declared on the pipe run.
    * **mixed_specs** — line numbers that mix more than one (non-auxiliary)
      spec across their components. Each entry reports ``line``, ``n_specs``
      (count of distinct normalized specs), ``specs`` (those distinct
      normalized specs, sorted) and ``spec_components`` (number of components
      on the line that contributed a non-empty, non-auxiliary spec — not the
      line's total component count).
    * **empty_spec** — components with a NULL or empty ``Spec``.
    * **ghost_specs** — specs used in the project that have no ``.pspc`` file in
      ``Spec Sheets`` (degrades gracefully if that folder is absent).
    * **catalogue_violations** — components whose ``Schedule`` (reliable) or
      ``Material`` (low confidence) is not among those allowed by their spec's
      ``.pspc`` catalogue.

    ``data`` accepts ``ignore_specs`` (list of auxiliary spec names to exclude
    from mixed/ghost checks; defaults to :data:`_DEFAULT_IGNORE_SPECS`) and
    ``limit`` (max examples reported per check; defaults to 50, 0 = no cap).

    Components are reported by ``PnPID`` and properties only — they cannot be
    located in the drawing without the .NET plugin.
    """
    data = data or {}
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    ignore_raw = data.get("ignore_specs")
    ignore_specs = {
        _norm(s) for s in (ignore_raw if ignore_raw is not None else _DEFAULT_IGNORE_SPECS)
    }
    limit = data.get("limit", _DEFAULT_LIMIT)

    con = _connect_ro(db)
    try:
        cur = con.cursor()

        # All component rows joined to their engineering properties. A single
        # pass feeds every check; the joins/filters per check are done in Python
        # so the auxiliary-spec exclusion stays consistent and testable.
        rows = cur.execute(
            """
            SELECT
                prc.PnPID            AS pnpid,
                prc.LineNumberTag    AS line,
                prc."Required Spec"  AS required_spec,
                ei.Spec              AS actual_spec,
                ei.PartCategory      AS class,
                ei.ShortDescription  AS description,
                ei.Schedule          AS schedule,
                ei.Material          AS material,
                ei.NominalDiameter   AS dia,
                ei.NominalUnit       AS unit
            FROM PipeRunComponent prc
            LEFT JOIN EngineeringItems ei ON ei.PnPID = prc.PnPID
            """
        ).fetchall()
    finally:
        con.close()

    def _component(r, **extra) -> dict:
        comp = {
            "pnpid": r["pnpid"],
            "class": r["class"],
            "description": r["description"],
            "size": _fmt_size(r["dia"], r["unit"]) if r["dia"] is not None else None,
        }
        comp.update(extra)
        return comp

    # --- Check 1: actual Spec != Required Spec ----------------------------
    mismatched: list[dict] = []
    for r in rows:
        req, act = r["required_spec"], r["actual_spec"]
        if not req or not str(req).strip():
            continue
        if not act or not str(act).strip():
            continue
        if _norm(act) != _norm(req):
            mismatched.append(
                _component(
                    r,
                    line=r["line"],
                    required_spec=req,
                    actual_spec=act,
                )
            )

    # --- Check 2: mixed specs within a single line ------------------------
    # Exclude auxiliary specs, then collect the distinct *normalized* specs per
    # line so case/space variants collapse into one — consistent with the
    # trigger condition below.
    line_specs: dict[str, set[str]] = {}
    line_components: dict[str, int] = {}
    for r in rows:
        line = r["line"]
        if line is None or str(line).strip() in ("", "?"):
            continue
        spec = r["actual_spec"]
        if not spec or not str(spec).strip():
            continue
        if _norm(spec) in ignore_specs:
            continue
        line_specs.setdefault(line, set()).add(_norm(spec))
        line_components[line] = line_components.get(line, 0) + 1

    mixed: list[dict] = []
    for line, specs in line_specs.items():
        # ``specs`` already holds the distinct normalized specs, so more than
        # one means the line mixes specs.
        if len(specs) > 1:
            mixed.append(
                {
                    "line": line,
                    "n_specs": len(specs),
                    "specs": sorted(specs),
                    "spec_components": line_components[line],
                }
            )
    mixed.sort(key=lambda d: (-d["n_specs"], d["line"]))

    # --- Check 3a: empty / NULL spec --------------------------------------
    empty_spec: list[dict] = []
    for r in rows:
        spec = r["actual_spec"]
        if spec is None or not str(spec).strip():
            empty_spec.append(_component(r, line=r["line"]))

    # --- Specs actually used (non-auxiliary), for ghost & catalogue checks -
    used_specs: dict[str, str] = {}  # normalized -> original (representative)
    for r in rows:
        spec = r["actual_spec"]
        if not spec or not str(spec).strip():
            continue
        key = _norm(spec)
        if key in ignore_specs:
            continue
        used_specs.setdefault(key, str(spec).strip())

    # --- Check 3b: ghost specs (used but no .pspc) ------------------------
    stems = _spec_sheet_stems(project_dir)
    if stems is None:
        ghost_section = {
            "checkable": False,
            "note": (
                "No comprobable: la carpeta 'Spec Sheets' no existe en el "
                "proyecto (sin catálogo local de specs)."
            ),
            "count": 0,
            "specs": [],
        }
        catalogue_stems: dict[str, str] = {}
    else:
        catalogue_stems = {_norm(s): s for s in stems}
        ghost = sorted(
            used_specs[k] for k in used_specs.keys() - catalogue_stems.keys()
        )
        ghost_section = {
            "checkable": True,
            "count": len(ghost),
            "specs": ghost,
        }

    # --- Check 4: Schedule / Material outside catalogue -------------------
    if stems is None:
        catalogue_section = {
            "checkable": False,
            "note": (
                "No comprobable: sin carpeta 'Spec Sheets' no hay catálogo "
                "contra el que validar Schedule/Material."
            ),
            "schedule_violations": [],
            "material_violations": [],
            "schedule_count": 0,
            "material_count": 0,
        }
    else:
        # Load catalogue (allowed Schedule/Material sets) per used spec that
        # actually has a .pspc file.
        catalogues: dict[str, dict[str, set[str]]] = {}
        for key, original in used_specs.items():
            stem = catalogue_stems.get(key)
            if stem is None:
                continue  # ghost spec: no catalogue to validate against
            try:
                catalogues[key] = _read_spec_catalogue(
                    project_dir / "Spec Sheets" / f"{stem}.pspc"
                )
            except (OSError, sqlite3.Error):
                # A catalogue we cannot read is skipped rather than failing the
                # whole validation.
                continue

        schedule_violations: list[dict] = []
        material_violations: list[dict] = []
        for r in rows:
            spec = r["actual_spec"]
            if not spec or not str(spec).strip():
                continue
            cat = catalogues.get(_norm(spec))
            if cat is None:
                continue

            sched = r["schedule"]
            if (
                sched is not None
                and str(sched).strip()
                and cat["schedules"]
                and _norm(str(sched)) not in cat["schedules"]
            ):
                schedule_violations.append(
                    _component(r, line=r["line"], spec=spec, schedule=sched)
                )

            mat = r["material"]
            if (
                mat is not None
                and str(mat).strip()
                and cat["materials"]
                and _norm_tight(str(mat)) not in cat["materials"]
            ):
                material_violations.append(
                    _component(r, line=r["line"], spec=spec, material=mat)
                )

        sched_ex, sched_omitted = _capped(schedule_violations, limit)
        mat_ex, mat_omitted = _capped(material_violations, limit)
        catalogue_section = {
            "checkable": True,
            "schedule_count": len(schedule_violations),
            "schedule_violations": sched_ex,
            "schedule_omitted": sched_omitted,
            "material_count": len(material_violations),
            "material_confidence": "low",
            "material_note": (
                "Aviso de baja confianza: los catálogos tienen materiales sin "
                "normalizar; la comparación ignora espacios y mayúsculas pero "
                "puede dar falsos positivos."
            ),
            "material_violations": mat_ex,
            "material_omitted": mat_omitted,
        }

    # --- Assemble report with capped example lists ------------------------
    mismatched_ex, mismatched_omitted = _capped(mismatched, limit)
    mixed_ex, mixed_omitted = _capped(mixed, limit)
    empty_ex, empty_omitted = _capped(empty_spec, limit)

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "limit": limit,
        "ignore_specs": sorted(
            ignore_raw if ignore_raw is not None else _DEFAULT_IGNORE_SPECS
        ),
        "mismatched_spec": {
            "description": "Spec real distinta de la Required Spec declarada.",
            "count": len(mismatched),
            "omitted": mismatched_omitted,
            "examples": mismatched_ex,
        },
        "mixed_specs": {
            "description": "Líneas que mezclan más de una spec (excluidas las auxiliares).",
            "count": len(mixed),
            "omitted": mixed_omitted,
            "examples": mixed_ex,
        },
        "empty_spec": {
            "description": "Componentes sin spec (NULL o vacía).",
            "count": len(empty_spec),
            "omitted": empty_omitted,
            "examples": empty_ex,
        },
        "ghost_specs": {
            "description": "Specs usadas sin fichero .pspc en 'Spec Sheets'.",
            **ghost_section,
        },
        "catalogue_violations": {
            "description": (
                "Schedule/Material fuera del catálogo .pspc de su spec "
                "(Schedule fiable; Material de baja confianza)."
            ),
            **catalogue_section,
        },
        "note": (
            "La localización del objeto en el dibujo requiere el plugin .NET; "
            "aquí solo se identifica por PnPID y propiedades."
        ),
    }
