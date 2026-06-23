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
# Schema introspection
# ---------------------------------------------------------------------------


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    """Return True if ``table`` exists in the database."""
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names of ``table`` (empty if it has none/absent).

    Uses ``PRAGMA table_info`` so callers can select only the columns that are
    actually present — the ``P3dLineGroup`` and ``PnPDrawings`` schemas vary
    between projects (custom client columns, optional ``IsoNumber``, ...).
    """
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {r["name"] for r in rows}


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


# ---------------------------------------------------------------------------
# Line list (LINE LIST)
# ---------------------------------------------------------------------------

# Columns we read from the P3dLineGroup header when present. Each is optional:
# the real schema varies per project, so only the existing ones are selected
# (PRAGMA) and the rest degrade to None.
_LINEGROUP_HEADER_COLS = (
    "Service",
    "NominalSpec",
    "NominalSize",
    "InsulationType",
    "InsulationThickness",
)


def _build_line_aggregates(rows: list) -> dict[str, dict]:
    """Aggregate piping component rows into per-line records.

    ``rows`` must expose ``line``, ``spec``, ``dia`` and ``unit`` (as in
    :func:`line_summary`). Returns a mapping ``LineNumberTag -> aggregate``
    where each aggregate holds the component count, the set of real specs and
    the per-``(dia, unit)`` size histogram. Kept pure (no I/O) so it can be
    unit-tested independently of SQLite.
    """
    lines: dict[str, dict] = {}
    for r in rows:
        agg = lines.setdefault(
            r["line"],
            {
                "line": r["line"],
                "components": 0,
                "_specs": set(),   # original spec strings
                "_sizes": {},      # (dia, unit) -> count
            },
        )
        agg["components"] += 1
        if r["spec"] and str(r["spec"]).strip():
            agg["_specs"].add(str(r["spec"]).strip())
        if r["dia"] is not None:
            key = (r["dia"], r["unit"])
            agg["_sizes"][key] = agg["_sizes"].get(key, 0) + 1
    return lines


def _format_sizes(size_hist: dict) -> tuple[str | None, list[str]]:
    """Return (main_size, sizes) from a ``(dia, unit) -> count`` histogram.

    Sizes are sorted by descending frequency (then by diameter) and formatted
    with :func:`_fmt_size`, keeping the unit separation so inches and
    millimetres are never merged into a misleading range. ``main_size`` is the
    most frequent one.
    """
    sizes_sorted = sorted(size_hist.items(), key=lambda kv: (-kv[1], kv[0][0]))
    sizes = [_fmt_size(dia, unit) for (dia, unit), _ in sizes_sorted]
    return (sizes[0] if sizes else None), sizes


def _spec_mixed(specs: set[str], ignore_specs: set[str]) -> bool:
    """True if more than one *non-auxiliary* real spec is present.

    ``specs`` are original spec strings; ``ignore_specs`` is a set of
    *normalized* auxiliary spec names to exclude (same criterion as
    :func:`validate_specs`).
    """
    real = {_norm(s) for s in specs} - ignore_specs
    return len(real) > 1


def list_lines(project: str, data: dict | None = None) -> dict:
    """Produce the LINE LIST of a Plant 3D project (one row per piping line).

    The grain is one row per *valid* ``PipeRunComponent.LineNumberTag`` (NULL,
    empty and ``?`` tags are excluded, as in :func:`find_untagged`). Per line it
    reports, each from its own reliable source:

    * ``components`` — number of components on the line.
    * ``service`` — from the ``P3dLineGroup`` header (the per-component
      ``PipeRunComponent.Service`` is contaminated by branches, so it is not
      used).
    * ``nominal_spec`` / ``nominal_size`` — from the ``P3dLineGroup`` header.
    * ``specs`` — distinct real specs aggregated from ``EngineeringItems.Spec``;
      ``spec_mixed`` is True when more than one non-auxiliary spec coexists.
    * ``main_size`` / ``sizes`` — the actual nominal diameters in use, kept
      separate per unit (inches vs millimetres are never mixed into a range).
    * ``insulation_type`` / ``insulation_thickness`` — from the header. A tag
      may map to several header groups; distinct non-empty values are returned
      as a list (a single value when they all agree, ``null`` when absent).
    * ``model_dwgs`` — the 3D **model** drawings the line lives in (via
      ``P3dDrawingLineGroupRelationship`` → ``PnPDrawings."Dwg Name"``). This is
      NOT the source P&ID.

    Schema robustness: the ``P3dLineGroup`` and ``PnPDrawings`` schemas vary per
    project, so columns are probed with ``PRAGMA table_info`` and only existing
    ones are selected; missing pieces degrade gracefully to ``null``/``[]`` with
    a note. ``data`` accepts ``ignore_specs`` (auxiliary specs excluded from the
    ``spec_mixed`` check) and ``limit`` (max lines returned; defaults to 50,
    0 = no cap — omitted lines are reported, never truncated silently).
    """
    data = data or {}
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    ignore_raw = data.get("ignore_specs")
    ignore_specs = {
        _norm(s)
        for s in (ignore_raw if ignore_raw is not None else _DEFAULT_IGNORE_SPECS)
    }
    limit = data.get("limit", _DEFAULT_LIMIT)

    notes: list[str] = []

    con = _connect_ro(db)
    try:
        cur = con.cursor()

        # --- Component aggregation (specs + sizes) per valid line ----------
        comp_rows = cur.execute(
            """
            SELECT
                prc.LineNumberTag    AS line,
                ei.Spec              AS spec,
                ei.NominalDiameter   AS dia,
                ei.NominalUnit       AS unit
            FROM PipeRunComponent prc
            LEFT JOIN EngineeringItems ei ON ei.PnPID = prc.PnPID
            WHERE prc.LineNumberTag IS NOT NULL
              AND TRIM(prc.LineNumberTag) NOT IN ('', '?')
            """
        ).fetchall()
        aggregates = _build_line_aggregates(comp_rows)

        # --- Header (P3dLineGroup) per Tag, schema-tolerant ----------------
        # A tag may correspond to several header groups; we keep, per tag,
        # the distinct non-empty values of each selected column. The dict is
        # keyed by the *normalized* tag (_norm: TRIM+UPPER) so the cross with
        # the component grain matches despite leading/trailing spaces or case
        # differences between LineNumberTag and P3dLineGroup.Tag.
        header: dict[str, dict[str, set[str]]] = {}
        have_header = _table_exists(con, "P3dLineGroup")
        present_cols: list[str] = []
        if have_header:
            cols = _table_columns(con, "P3dLineGroup")
            if "Tag" not in cols:
                have_header = False
            else:
                present_cols = [c for c in _LINEGROUP_HEADER_COLS if c in cols]
        if have_header:
            select_cols = ", ".join(f'"{c}"' for c in ["Tag", *present_cols])
            hrows = cur.execute(
                f"SELECT {select_cols} FROM P3dLineGroup"
            ).fetchall()
            for r in hrows:
                tag = r["Tag"]
                if not tag or str(tag).strip() in ("", "?"):
                    continue
                key = _norm(tag)
                bucket = header.setdefault(key, {c: set() for c in present_cols})
                for c in present_cols:
                    val = r[c]
                    if val is not None and str(val).strip():
                        bucket[c].add(str(val).strip())
            missing = [c for c in _LINEGROUP_HEADER_COLS if c not in present_cols]
            if missing:
                notes.append(
                    "Columnas ausentes en P3dLineGroup (devueltas como null): "
                    + ", ".join(missing)
                    + "."
                )
        else:
            notes.append(
                "No se encontró la cabecera P3dLineGroup (o sin columna 'Tag'): "
                "service, nominal_spec, nominal_size e insulation se devuelven "
                "como null."
            )

        # --- Model drawings per line via relation table --------------------
        # Keyed by the *normalized* tag, consistent with ``header``.
        dwgs_by_tag: dict[str, set[str]] = {}
        have_rel = (
            have_header
            and _table_exists(con, "P3dDrawingLineGroupRelationship")
            and _table_exists(con, "PnPDrawings")
        )
        if have_rel:
            # Guard not only the tables' existence but the specific columns the
            # JOIN relies on: the relation needs LineGroup/Drawing and the
            # drawings table needs PnPID and "Dwg Name". A missing column would
            # otherwise raise sqlite3.OperationalError and break the whole tool,
            # so we degrade to model_dwgs=[] plus a note instead.
            rel_cols = _table_columns(con, "P3dDrawingLineGroupRelationship")
            dwg_cols = _table_columns(con, "PnPDrawings")
            missing_rel = [c for c in ("LineGroup", "Drawing") if c not in rel_cols]
            missing_dwg = [c for c in ("PnPID", "Dwg Name") if c not in dwg_cols]
            if missing_rel or missing_dwg:
                have_rel = False
                faltan = ", ".join(
                    [f"P3dDrawingLineGroupRelationship.{c}" for c in missing_rel]
                    + [f'PnPDrawings."{c}"' for c in missing_dwg]
                )
                notes.append(
                    "Columnas ausentes para el cruce de dibujos del modelo "
                    f"({faltan}): model_dwgs vacío."
                )
        if have_rel:
            # P3dLineGroup.PnPID = relationship.LineGroup ; relationship.Drawing
            # = PnPDrawings.PnPID. Resolve the model DWG name per line Tag.
            try:
                rel_rows = cur.execute(
                    """
                    SELECT lg.Tag AS tag, d."Dwg Name" AS dwg
                    FROM P3dLineGroup lg
                    JOIN P3dDrawingLineGroupRelationship rel
                      ON rel.LineGroup = lg.PnPID
                    JOIN PnPDrawings d
                      ON d.PnPID = rel.Drawing
                    """
                ).fetchall()
            except sqlite3.Error:
                # Any other schema incompatibility (e.g. a missing PnPID on
                # P3dLineGroup) degrades gracefully rather than failing.
                rel_rows = []
                notes.append(
                    "No se pudo resolver el cruce de dibujos del modelo "
                    "(incompatibilidad de esquema): model_dwgs vacío."
                )
            for r in rel_rows:
                tag = r["tag"]
                if not tag or str(tag).strip() in ("", "?"):
                    continue
                key = _norm(tag)
                if r["dwg"] and str(r["dwg"]).strip():
                    dwgs_by_tag.setdefault(key, set()).add(str(r["dwg"]).strip())
        else:
            notes.append(
                "Sin relación de dibujos del modelo disponible: model_dwgs vacío."
            )
    finally:
        con.close()

    def _hval(key: str, col: str):
        """Single value (or list, or None) of a header column for a match key.

        ``key`` is the *normalized* tag (_norm) used to cross the component
        grain with the header — never the raw LineNumberTag.
        """
        vals = sorted(header.get(key, {}).get(col, set()))
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else vals

    lines_out: list[dict] = []
    for agg in aggregates.values():
        tag = agg["line"]            # raw LineNumberTag — kept as-is in output
        key = _norm(tag)             # normalized match key for header/dwgs cross
        main_size, sizes = _format_sizes(agg["_sizes"])
        lines_out.append(
            {
                "line": tag,
                "components": agg["components"],
                "service": _hval(key, "Service"),
                "nominal_spec": _hval(key, "NominalSpec"),
                "nominal_size": _hval(key, "NominalSize"),
                "specs": sorted(agg["_specs"]),
                "spec_mixed": _spec_mixed(agg["_specs"], ignore_specs),
                "main_size": main_size,
                "sizes": sizes,
                "insulation_type": _hval(key, "InsulationType"),
                "insulation_thickness": _hval(key, "InsulationThickness"),
                "model_dwgs": sorted(dwgs_by_tag.get(key, set())),
            }
        )

    lines_out.sort(key=lambda d: d["line"])
    count = len(lines_out)
    capped, omitted = _capped(lines_out, limit)

    notes.append(
        "El 'P&ID de origen' y la localización del objeto en el dibujo NO están "
        "disponibles vía SQLite (requerirían el plugin .NET); 'model_dwgs' es el "
        "DWG del MODELO 3D, no el P&ID."
    )

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "limit": limit,
        "ignore_specs": sorted(
            ignore_raw if ignore_raw is not None else _DEFAULT_IGNORE_SPECS
        ),
        "count": count,
        "omitted": omitted,
        "lines": capped,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Component listing (filtered inventory)
# ---------------------------------------------------------------------------

# Canonical class filter -> set of normalized PartCategory values it maps to.
# The mapping lets the caller use friendly names ("valve", "fitting") instead
# of the raw EngineeringItems.PartCategory strings. A value that is none of
# these canonical keys is treated as a literal PartCategory (passthrough),
# compared normalized (_norm: TRIM+UPPER).
_CLASS_MAP: dict[str, set[str]] = {
    "pipe": {"PIPE"},
    "valve": {"VALVES"},
    "fitting": {"FITTINGS", "OLET"},
    "flange": {"FLANGES"},
    "instrument": {"INSTRUMENTS"},
    # "support" is special-cased below: PartCategory NULL / '' / 'DEFAULT'.
}

# Normalized PartCategory values that identify a pipe support (these components
# carry no real category in EngineeringItems).
_SUPPORT_CATEGORIES = {"", "DEFAULT"}


def _is_blank_tag(value: str | None) -> bool:
    """True if ``value`` is an absent component tag.

    Treats NULL, empty, and placeholder tags made only of ``?`` and spaces
    (e.g. ``?``, ``?-?``, ``? - ?``) as "no tag" — the same lenient criterion
    used for line tags elsewhere, extended so multi-segment placeholders are
    also caught.
    """
    if value is None:
        return True
    stripped = str(value).strip()
    if stripped in ("", "?"):
        return True
    # Only question marks, dashes and whitespace -> placeholder, not a real tag.
    return all(ch in "?- \t" for ch in stripped)


def list_components(project: str, data: dict | None = None) -> dict:
    """List piping components of a Plant 3D project, optionally filtered.

    Reads ``Piping.dcf`` (``PipeRunComponent`` joined to ``EngineeringItems``
    by ``PnPID``) and returns one entry per component identified by ``PnPID``
    plus its engineering properties (class, tag, description, spec, nominal
    size, line). A single SELECT is issued and every filter is applied in
    Python — the component volume is small (a few thousand rows).

    ``data`` accepts these optional filters:

    * ``classes`` — list of classes to include. Canonical keys map to one or
      more ``PartCategory`` values: ``pipe`` -> {Pipe}, ``valve`` -> {Valves},
      ``fitting`` -> {Fittings, Olet}, ``flange`` -> {Flanges},
      ``instrument`` -> {Instruments}, ``support`` -> components whose
      ``PartCategory`` is NULL/empty/``Default``. Any non-canonical value is
      treated as a literal ``PartCategory`` (passthrough), compared normalized.
      Omitted/empty means all classes.
    * ``line`` — keep only components whose ``LineNumberTag`` matches (``_norm``
      TRIM+UPPER, exact).
    * ``spec`` — keep only components whose ``EngineeringItems.Spec`` matches
      (``_norm``, exact).
    * ``size`` — keep only a given nominal diameter. **Requires a unit** to
      avoid mixing inches and millimetres: pass ``{"value": <num>, "unit":
      "in"|"mm"}``. ``size`` without a usable unit is NOT applied (the size
      filter is skipped and a note is added) rather than guessing a unit.
    * ``limit`` — max components returned (default 50, 0 = no cap). Omitted
      components are reported via ``omitted``, never truncated silently.

    Components cannot be located in the drawing from here — that needs the .NET
    plugin — so they are reported by ``PnPID`` and properties only.

    Schema robustness: ``PipeRunComponent.Tag`` (the per-component tag) is
    optional and absent in some projects; its presence is probed with
    ``PRAGMA table_info`` and, when missing, ``tag`` degrades to ``null`` with
    a note.
    """
    data = data or {}
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    limit = data.get("limit", _DEFAULT_LIMIT)
    notes: list[str] = []

    # --- Parse the class filter into canonical sets + passthrough values ----
    classes_raw = data.get("classes")
    want_categories: set[str] = set()   # normalized PartCategory values to keep
    want_support = False
    classes_echo: list[str] = []
    if classes_raw:
        for c in classes_raw:
            key = str(c).strip().lower()
            classes_echo.append(key)
            if key == "support":
                want_support = True
            elif key in _CLASS_MAP:
                want_categories |= _CLASS_MAP[key]
            else:
                # Passthrough: treat as a literal PartCategory value.
                want_categories.add(_norm(c))

    # --- Parse the size filter (unit is mandatory) --------------------------
    size_raw = data.get("size")
    size_value: float | None = None
    size_unit: str | None = None
    if size_raw is not None:
        if isinstance(size_raw, dict):
            size_value = size_raw.get("value")
            size_unit = size_raw.get("unit")
        else:
            # A bare number with no unit: do not guess in/mm.
            size_value = size_raw
        if size_value is None or not (size_unit and str(size_unit).strip()):
            notes.append(
                "Filtro 'size' ignorado: requiere unidad explícita "
                '({"value": <num>, "unit": "in"|"mm"}) para no mezclar in/mm.'
            )
            size_value = None
            size_unit = None

    line_filter = data.get("line")
    line_norm = _norm(line_filter) if line_filter else None
    spec_filter = data.get("spec")
    spec_norm = _norm(spec_filter) if spec_filter else None

    # --- Single SELECT (Tag column probed for graceful degradation) ---------
    con = _connect_ro(db)
    try:
        have_tag = "Tag" in _table_columns(con, "PipeRunComponent")
        tag_expr = "prc.Tag" if have_tag else "NULL"
        cur = con.cursor()
        rows = cur.execute(
            f"""
            SELECT
                prc.PnPID            AS pnpid,
                ei.PartCategory      AS part_category,
                ei.ShortDescription  AS description,
                ei.Spec              AS spec,
                ei.NominalDiameter   AS dia,
                ei.NominalUnit       AS unit,
                prc.LineNumberTag    AS line,
                {tag_expr}           AS tag
            FROM PipeRunComponent prc
            LEFT JOIN EngineeringItems ei ON ei.PnPID = prc.PnPID
            """
        ).fetchall()
    finally:
        con.close()

    if not have_tag:
        notes.append(
            "La columna 'Tag' no existe en PipeRunComponent en este proyecto: "
            "el tag de componente se devuelve como null."
        )

    def _class_matches(part_category: str | None) -> bool:
        """Apply the class filter to a component's PartCategory."""
        norm_cat = _norm(part_category)
        is_support = norm_cat in _SUPPORT_CATEGORIES
        if want_support and is_support:
            return True
        return norm_cat in want_categories

    by_class: dict[str, int] = {}
    components: list[dict] = []
    apply_class = bool(classes_raw)
    apply_size = size_value is not None

    for r in rows:
        # --- class filter ---------------------------------------------------
        if apply_class and not _class_matches(r["part_category"]):
            continue

        # --- line filter ----------------------------------------------------
        if line_norm is not None and _norm(r["line"]) != line_norm:
            continue

        # --- spec filter ----------------------------------------------------
        if spec_norm is not None and _norm(r["spec"]) != spec_norm:
            continue

        # --- size filter (value + unit, units never mixed) ------------------
        if apply_size:
            dia = r["dia"]
            if dia is None:
                continue
            try:
                if abs(float(dia) - float(size_value)) >= 1e-6:
                    continue
            except (TypeError, ValueError):
                continue
            if _norm(r["unit"]) != _norm(size_unit):
                continue

        # --- sanitize line (blank/placeholder -> None in output) -----------
        line_val = None if _is_blank_tag(r["line"]) else str(r["line"]).strip()

        # --- sanitize component tag ----------------------------------------
        tag_val = None if _is_blank_tag(r["tag"]) else str(r["tag"]).strip()

        # --- class label for grouping/counting -----------------------------
        cls_label = (
            r["part_category"]
            if r["part_category"] is not None and str(r["part_category"]).strip()
            else "(sin clase)"
        )
        by_class[cls_label] = by_class.get(cls_label, 0) + 1

        components.append(
            {
                "pnpid": r["pnpid"],
                "class": r["part_category"],
                "tag": tag_val,
                "description": r["description"],
                "spec": r["spec"],
                "size": _fmt_size(r["dia"], r["unit"]) if r["dia"] is not None else None,
                "line": line_val,
            }
        )

    by_class_ranked = [
        {"class": name, "count": count}
        for name, count in sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    count = len(components)
    capped, omitted = _capped(components, limit)

    filters_echo: dict = {}
    if classes_raw:
        filters_echo["classes"] = classes_echo
    if line_norm is not None:
        filters_echo["line"] = line_norm
    if spec_norm is not None:
        filters_echo["spec"] = spec_norm
    if size_value is not None:
        filters_echo["size"] = {"value": size_value, "unit": _norm(size_unit)}

    notes.append(
        "La localización del objeto en el dibujo requiere el plugin .NET; "
        "aquí solo se identifica por PnPID y propiedades."
    )

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "limit": limit,
        "filters": filters_echo,
        "count": count,
        "omitted": omitted,
        "by_class": by_class_ranked,
        "components": capped,
        "notes": notes,
    }


def list_valves(project: str, data: dict | None = None) -> dict:
    """List valve components of a Plant 3D project (preset of ``list_components``).

    Thin wrapper that calls :func:`list_components` with the class filter pinned
    to ``valve`` (i.e. ``EngineeringItems.PartCategory`` == ``Valves``). Any
    ``classes`` value supplied by the caller is ignored and overridden. All
    other filters (``line``, ``spec``, ``size``, ``limit``) are forwarded as-is.
    The caller's ``data`` dict is never mutated. Output shape is identical to
    :func:`list_components`.
    """
    merged = dict(data or {})
    merged["classes"] = ["valve"]  # pin class; ignore/override any caller 'classes'
    return list_components(project, merged)


def list_instruments(project: str, data: dict | None = None) -> dict:
    """List instrument components of a Plant 3D project (preset of ``list_components``).

    Thin wrapper that calls :func:`list_components` with the class filter pinned
    to ``instrument`` (i.e. ``EngineeringItems.PartCategory`` == ``Instruments``).
    Any ``classes`` value supplied by the caller is ignored and overridden. All
    other filters (``line``, ``spec``, ``size``, ``limit``) are forwarded as-is.
    The caller's ``data`` dict is never mutated. Output shape is identical to
    :func:`list_components`.
    """
    merged = dict(data or {})
    merged["classes"] = ["instrument"]  # pin class; ignore/override any caller 'classes'
    return list_components(project, merged)


def bom(project: str, data: dict | None = None) -> dict:
    """Build the Bill of Materials of a Plant 3D project by aggregating components.

    This is an aggregation on top of :func:`list_components`: it does NOT emit
    its own SQL. It calls :func:`list_components` as the base reader (with no
    cap) so that class mapping, tag sanitisation and size formatting stay
    consistent, then groups the returned components in Python.

    Grouping key is the tuple ``(class, spec, size, description)`` — each distinct
    combination is one BOM line with a quantity (the count of components in that
    group). ``spec``, ``size`` and ``description`` may be ``None`` and are kept
    as-is on the line (and as valid key values). A ``None``/empty ``class`` is
    normalised to the label ``"(sin clase)"`` for output/ordering only (``None``
    and ``""`` collapse into the same group).

    ``data`` accepts the same scope filters as :func:`list_components`
    (``classes``, ``line``, ``spec``, ``size``) plus ``limit`` — but here
    ``limit`` caps the number of BOM LINES returned (default 50, 0 = no cap),
    not the number of components. The caller's ``data`` dict is never mutated.

    Quantities are component counts, not lengths (pipe length is a separate
    tool). The drawing-location note from the base reader is not propagated, as
    it does not apply to a BOM; the ignored-``size`` note is kept when present.
    """
    data = data or {}
    limit = data.get("limit", _DEFAULT_LIMIT)

    # Read every matching component (no inner cap) so we aggregate over all.
    inner = list_components(
        project,
        {
            "classes": data.get("classes"),
            "line": data.get("line"),
            "spec": data.get("spec"),
            "size": data.get("size"),
            "limit": 0,  # no cap: aggregate over all components
        },
    )

    # --- aggregate components into BOM lines ------------------------------
    # key -> {"class", "spec", "size", "description", "quantity"}
    groups: dict[tuple, dict] = {}
    by_class: dict[str, int] = {}
    for comp in inner["components"]:
        cls_raw = comp.get("class")
        cls_label = (
            cls_raw
            if cls_raw is not None and str(cls_raw).strip()
            else "(sin clase)"
        )
        spec = comp.get("spec")
        size = comp.get("size")
        desc = comp.get("description")
        key = (cls_label, spec, size, desc)
        line = groups.get(key)
        if line is None:
            groups[key] = {
                "class": cls_label,
                "spec": spec,
                "size": size,
                "description": desc,
                "quantity": 1,
            }
        else:
            line["quantity"] += 1
        by_class[cls_label] = by_class.get(cls_label, 0) + 1

    # --- order: class asc, quantity desc, description asc (None last) -----
    lineas = sorted(
        groups.values(),
        key=lambda b: (b["class"], -b["quantity"], b["description"] or ""),
    )

    by_class_ranked = [
        {"class": name, "count": count}
        for name, count in sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    capped, omitted = _capped(lineas, limit)

    # --- notes: keep relevant inner notes, drop the .NET-location one -----
    notes: list[str] = [
        n for n in inner["notes"] if "plugin .NET" not in n
    ]
    notes.append(
        "Cada línea del BOM agrupa componentes por (clase, spec, tamaño, "
        "descripción); la cantidad es el recuento de componentes, no longitudes "
        "(la longitud de tubería es otra herramienta)."
    )

    return {
        "ok": True,
        "project": inner["project"],
        "path": inner["path"],
        "limit": limit,
        "filters": inner["filters"],
        "total_components": len(inner["components"]),
        "line_count": len(lineas),
        "omitted": omitted,
        "by_class": by_class_ranked,
        "bom": capped,
        "notes": notes,
    }
