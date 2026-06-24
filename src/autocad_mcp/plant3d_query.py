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


def _filters_echo(
    line_norm: str | None,
    spec_norm: str | None,
    size_value: float | None,
    size_unit: str | None,
) -> dict:
    """Build the applied-filters echo (same style as :func:`list_components`)."""
    filters_echo: dict = {}
    if line_norm is not None:
        filters_echo["line"] = line_norm
    if spec_norm is not None:
        filters_echo["spec"] = spec_norm
    if size_value is not None:
        filters_echo["size"] = {"value": size_value, "unit": _norm(size_unit)}
    return filters_echo


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


# ---------------------------------------------------------------------------
# Pipe length (total run lengths)
# ---------------------------------------------------------------------------

# Las claves canónicas de agrupación admitidas por pipe_length.
_PIPE_LENGTH_GROUP_BY = ("line", "spec", "size")

# Etiqueta para los tramos sin número de línea válido cuando se agrupa por línea
# (mismo criterio visual que line_summary).
_NO_LINE_LABEL = "(SIN LÍNEA)"


def _build_pipe_length_aggregates(
    rows: list, group_by: str
) -> tuple[dict, dict, int, dict]:
    """Aggregate raw pipe rows into per-group length totals. Pure (no I/O).

    ``rows`` must expose ``line``, ``spec``, ``dia``, ``dia_unit``,
    ``length_unit`` and ``length`` (as produced by :func:`pipe_length`'s base
    SELECT). Lengths are summed **per length unit** — totals of different
    ``LengthUnit`` are never collapsed into one figure (in practice a project
    uses a single unit, but mixed units are kept separate and reported).

    The grouping key depends on ``group_by``:

    * ``"line"`` — the raw ``LineNumberTag``; rows with a blank/placeholder tag
      (``_is_blank_tag``) are grouped under :data:`_NO_LINE_LABEL`.
    * ``"spec"`` — the ``EngineeringItems.Spec`` (None/empty -> ``"(sin spec)"``).
    * ``"size"`` — the nominal diameter formatted with :func:`_fmt_size`, kept
      separate per unit (inches vs millimetres are never merged).

    Returns a tuple ``(groups, totals_by_unit, total_count, untagged)``:

    * ``groups`` — ``group_value -> {pipe_count, lengths: {unit -> sum}}``.
    * ``totals_by_unit`` — ``length_unit -> total length`` over every row.
    * ``total_count`` — total number of pipe rows in scope.
    * ``untagged`` — ``{"pipe_count", "lengths": {unit -> sum}}`` for rows whose
      ``LineNumberTag`` is blank/placeholder (reported regardless of group_by).
    """
    groups: dict[str, dict] = {}
    totals_by_unit: dict[str, float] = {}
    total_count = 0
    untagged = {"pipe_count": 0, "lengths": {}}

    for r in rows:
        length = r["length"]
        # Sin longitud no se puede acumular el tramo; se ignora (NO se cuenta).
        if length is None:
            continue
        try:
            length_val = float(length)
        except (TypeError, ValueError):
            continue

        # La unidad de longitud forma parte de la identidad del total: nunca se
        # mezclan longitudes de distinta LengthUnit. None -> clave "?".
        lunit = r["length_unit"]
        lunit_key = str(lunit).strip() if lunit and str(lunit).strip() else "?"

        total_count += 1
        totals_by_unit[lunit_key] = totals_by_unit.get(lunit_key, 0.0) + length_val

        # Los tramos sin línea válida se reportan SIEMPRE en 'untagged'.
        is_untagged = _is_blank_tag(r["line"])
        if is_untagged:
            untagged["pipe_count"] += 1
            untagged["lengths"][lunit_key] = (
                untagged["lengths"].get(lunit_key, 0.0) + length_val
            )

        # --- clave de agrupación según group_by ----------------------------
        if group_by == "spec":
            spec = r["spec"]
            gkey = str(spec).strip() if spec and str(spec).strip() else "(sin spec)"
        elif group_by == "size":
            gkey = (
                _fmt_size(r["dia"], r["dia_unit"]) if r["dia"] is not None else "?"
            )
        else:  # "line"
            gkey = (
                _NO_LINE_LABEL if is_untagged else str(r["line"]).strip()
            )

        grp = groups.setdefault(gkey, {"pipe_count": 0, "lengths": {}})
        grp["pipe_count"] += 1
        grp["lengths"][lunit_key] = grp["lengths"].get(lunit_key, 0.0) + length_val

    return groups, totals_by_unit, total_count, untagged


def _round_lengths(lengths: dict[str, float]) -> dict[str, float] | float:
    """Round a ``unit -> sum`` map to 2 decimals.

    Collapses to a single number when only one unit is present (the common
    case); otherwise returns the per-unit dict so distinct units stay separate.
    """
    rounded = {u: round(v, 2) for u, v in lengths.items()}
    if len(rounded) == 1:
        return next(iter(rounded.values()))
    return rounded


def pipe_length(project: str, data: dict | None = None) -> dict:
    """Sum real pipe run lengths of a Plant 3D project, grouped and filtered.

    Lengths live in the dedicated ``Pipe`` table (one row per pipe run,
    ``PartCategory='Pipe'``), joined to ``EngineeringItems`` and
    ``PipeRunComponent`` by ``PnPID``. The column read is ``Pipe.Length``;
    ``CutLength`` and the fixed-length columns are ignored. Lengths from valves,
    fittings, etc. are NOT included (those tables' ``Length`` is a physical
    component dimension, not a run length).

    The length unit is read from ``EngineeringItems.LengthUnit`` (never assumed)
    and treated as part of the total's identity: lengths of different units are
    never collapsed into one figure (a project normally uses a single unit). The
    diameter unit (``NominalUnit``) is orthogonal and only used for the
    ``size`` grouping/filter.

    ``data`` accepts:

    * ``group_by`` — ``"line"`` (default) | ``"spec"`` | ``"size"``: the key the
      returned groups are aggregated by. For ``"line"``, pipe runs without a
      valid ``LineNumberTag`` are grouped under ``"(SIN LÍNEA)"``; for ``"spec"``
      / ``"size"`` they fall into their natural spec/size group.
    * ``line`` — keep only runs whose ``LineNumberTag`` matches (``_norm``
      TRIM+UPPER, exact).
    * ``spec`` — keep only runs whose ``EngineeringItems.Spec`` matches
      (``_norm``, exact).
    * ``size`` — keep only a given nominal **diameter**; requires a unit
      (``{"value": <num>, "unit": "in"|"mm"}``); without a usable unit the filter
      is skipped and a note is added (same handling as :func:`list_components`).
    * ``limit`` — max GROUPS returned (default 50, 0 = no cap). Omitted groups
      are reported via ``omitted``, never truncated silently.

    Untagged pipe runs (blank/``?`` ``LineNumberTag``) are always reported in a
    top-level ``untagged`` field, independently of ``group_by``. The caller's
    ``data`` dict is never mutated.

    Schema robustness: the ``Pipe`` table and its ``Length`` column are probed
    with ``PRAGMA table_info`` before querying; if either is absent the tool
    degrades gracefully (``ok: True``, empty groups, zero totals, explanatory
    note) instead of raising. ``EngineeringItems.LengthUnit`` is likewise
    optional and degrades to ``length_unit: null`` with a note.
    """
    data = data or {}
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    # --- group_by (validado; default "line") -------------------------------
    group_by = str(data.get("group_by") or "line").strip().lower()
    notes: list[str] = []
    if group_by not in _PIPE_LENGTH_GROUP_BY:
        notes.append(
            f"group_by '{group_by}' no reconocido; se usa 'line' "
            "(válidos: line, spec, size)."
        )
        group_by = "line"

    limit = data.get("limit", _DEFAULT_LIMIT)

    # --- filtros de alcance (misma semántica que list_components) ----------
    line_filter = data.get("line")
    line_norm = _norm(line_filter) if line_filter else None
    spec_filter = data.get("spec")
    spec_norm = _norm(spec_filter) if spec_filter else None

    # size: filtra por DIÁMETRO nominal y exige unidad (no se adivina in/mm).
    size_raw = data.get("size")
    size_value: float | None = None
    size_unit: str | None = None
    if size_raw is not None:
        if isinstance(size_raw, dict):
            size_value = size_raw.get("value")
            size_unit = size_raw.get("unit")
        else:
            size_value = size_raw
        if size_value is None or not (size_unit and str(size_unit).strip()):
            notes.append(
                "Filtro 'size' ignorado: requiere unidad explícita "
                '({"value": <num>, "unit": "in"|"mm"}) para no mezclar in/mm. '
                "Filtra el diámetro nominal, no la longitud."
            )
            size_value = None
            size_unit = None

    def _base_response(extra_notes: list[str]) -> dict:
        """Respuesta degradada (ok) cuando no hay longitudes que sumar."""
        filters_echo = _filters_echo(line_norm, spec_norm, size_value, size_unit)
        return {
            "ok": True,
            "project": project_dir.name,
            "path": str(project_dir),
            "limit": limit,
            "group_by": group_by,
            "filters": filters_echo,
            "length_unit": None,
            "total_pipe_count": 0,
            "total_length": 0,
            "untagged": {"pipe_count": 0, "length": 0},
            "group_count": 0,
            "omitted": 0,
            "groups": [],
            "notes": notes + extra_notes,
        }

    con = _connect_ro(db)
    try:
        # --- robustez de esquema: Pipe + Pipe.Length deben existir ----------
        if not _table_exists(con, "Pipe"):
            return _base_response(
                [
                    "El proyecto no expone una tabla 'Pipe' en Piping.dcf; "
                    "las longitudes de tubería no están disponibles vía SQLite."
                ]
            )
        pipe_cols = _table_columns(con, "Pipe")
        if "Length" not in pipe_cols:
            return _base_response(
                [
                    "La tabla 'Pipe' no tiene columna 'Length' en este proyecto; "
                    "las longitudes de tubería no están disponibles vía SQLite."
                ]
            )

        # LengthUnit es opcional: si falta, se selecciona NULL y se anota.
        have_length_unit = "LengthUnit" in _table_columns(con, "EngineeringItems")
        length_unit_expr = "ei.LengthUnit" if have_length_unit else "NULL"
        if not have_length_unit:
            notes.append(
                "EngineeringItems no tiene columna 'LengthUnit' en este proyecto; "
                "length_unit se devuelve como null (unidad no determinada)."
            )

        cur = con.cursor()
        rows = cur.execute(
            f"""
            SELECT
                prc.LineNumberTag   AS line,
                ei.Spec             AS spec,
                ei.NominalDiameter  AS dia,
                ei.NominalUnit      AS dia_unit,
                {length_unit_expr}  AS length_unit,
                p.Length            AS length
            FROM Pipe p
            JOIN EngineeringItems ei  ON ei.PnPID  = p.PnPID
            JOIN PipeRunComponent prc ON prc.PnPID = p.PnPID
            """
        ).fetchall()
    finally:
        con.close()

    # --- aplicar filtros de alcance en Python (una sola pasada) ------------
    apply_size = size_value is not None
    filtered: list = []
    for r in rows:
        if line_norm is not None and _norm(r["line"]) != line_norm:
            continue
        if spec_norm is not None and _norm(r["spec"]) != spec_norm:
            continue
        if apply_size:
            dia = r["dia"]
            if dia is None:
                continue
            try:
                if abs(float(dia) - float(size_value)) >= 1e-6:
                    continue
            except (TypeError, ValueError):
                continue
            if _norm(r["dia_unit"]) != _norm(size_unit):
                continue
        filtered.append(r)

    # --- agregación pura ----------------------------------------------------
    groups, totals_by_unit, total_count, untagged = _build_pipe_length_aggregates(
        filtered, group_by
    )

    # --- construir grupos de salida (ordenados por longitud desc, valor asc) -
    groups_out: list[dict] = []
    for gkey, agg in groups.items():
        length_out = _round_lengths(agg["lengths"])
        # Para ordenar por longitud necesitamos un escalar: suma de todas las
        # unidades del grupo (sólo relevante si hubiera unidades mezcladas).
        sort_length = sum(agg["lengths"].values())
        unit_out = (
            next(iter(agg["lengths"].keys()))
            if len(agg["lengths"]) == 1
            else sorted(agg["lengths"].keys())
        )
        groups_out.append(
            {
                "group": gkey,
                "pipe_count": agg["pipe_count"],
                "length": length_out,
                "length_unit": unit_out,
                "_sort_length": sort_length,
            }
        )

    groups_out.sort(key=lambda g: (-g["_sort_length"], str(g["group"])))
    for g in groups_out:
        del g["_sort_length"]

    group_count = len(groups_out)
    capped, omitted = _capped(groups_out, limit)

    # --- unidad(es) de longitud a nivel global ------------------------------
    units_present = sorted(totals_by_unit.keys())
    if not units_present:
        length_unit_out = None
    elif len(units_present) == 1:
        length_unit_out = units_present[0]
    else:
        length_unit_out = units_present
        notes.append(
            "Se han detectado varias unidades de longitud "
            f"({', '.join(units_present)}); los totales se reportan por unidad "
            "sin mezclarlas."
        )

    filters_echo = _filters_echo(line_norm, spec_norm, size_value, size_unit)

    notes.append(
        "Las longitudes provienen de la tabla 'Pipe' (solo tramos de tubería, "
        "PartCategory='Pipe'); no incluyen dimensiones físicas de válvulas, "
        "accesorios ni instrumentos."
    )
    notes.append(
        "La unidad de longitud se lee de EngineeringItems.LengthUnit (no se "
        "asume); es independiente de la unidad de diámetro (NominalUnit)."
    )

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "limit": limit,
        "group_by": group_by,
        "filters": filters_echo,
        "length_unit": length_unit_out,
        "total_pipe_count": total_count,
        "total_length": _round_lengths(totals_by_unit) if totals_by_unit else 0,
        "untagged": {
            "pipe_count": untagged["pipe_count"],
            "length": _round_lengths(untagged["lengths"]) if untagged["lengths"] else 0,
        },
        "group_count": group_count,
        "omitted": omitted,
        "groups": capped,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Weld list (weld count / breakdown)
# ---------------------------------------------------------------------------

# Tablas dedicadas de soldaduras en Piping.dcf, una fila por soldadura.
# La clave del mapa es la etiqueta de subtipo (weld_type) que se expone en la
# salida; el valor es el nombre real de la tabla en el SQLite.
_WELD_TABLES = {
    "butt": "Buttweld",
    "socket": "Socketweld",
    "tap": "TapWeld",
}

# Subtipos válidos para el filtro weld_type (las claves de _WELD_TABLES).
_WELD_TYPES = tuple(_WELD_TABLES.keys())

# Claves canónicas de agrupación admitidas por weld_list.
_WELD_GROUP_BY = ("line", "size", "spec", "shop_field", "type")

# Etiqueta para soldaduras sin Shop_Field reconocible (NULL o fuera de
# {SHOP, FIELD}).
_UNKNOWN_SHOP_FIELD = "(desconocido)"


def _norm_shop_field(value: str | None) -> str:
    """Normalize a Shop_Field value to ``"shop"`` / ``"field"`` / unknown.

    The ``Shop_Field`` column holds SHOP / FIELD; anything else (NULL, blank or
    an unexpected token) collapses to :data:`_UNKNOWN_SHOP_FIELD`.
    """
    v = _norm(value)
    if v == "SHOP":
        return "shop"
    if v == "FIELD":
        return "field"
    return _UNKNOWN_SHOP_FIELD


def _build_weld_aggregates(
    rows: list, group_by: str
) -> tuple[dict, dict, dict, int, dict]:
    """Aggregate raw weld rows into per-group counts. Pure (no I/O).

    ``rows`` must expose ``weld_type`` (``"butt"``/``"socket"``/``"tap"``),
    ``shop_field`` (already normalized to ``"shop"``/``"field"``/unknown via
    :func:`_norm_shop_field`), ``line`` (the resolved raw line Tag, or None),
    ``spec``, ``dia`` and ``dia_unit`` (as produced by :func:`weld_list`'s base
    join). Every row is exactly one weld, so the unit of aggregation is a count.

    The grouping key depends on ``group_by``:

    * ``"line"`` — the raw resolved line Tag; welds with a blank/placeholder or
      absent Tag (``_is_blank_tag``) are grouped under :data:`_NO_LINE_LABEL`.
    * ``"size"`` — the nominal diameter formatted with :func:`_fmt_size`, kept
      separate per unit (inches vs millimetres are never merged).
    * ``"spec"`` — the ``EngineeringItems.Spec`` (None/empty -> ``"(sin spec)"``).
    * ``"shop_field"`` — the normalized Shop_Field (shop/field/unknown).
    * ``"type"`` — the weld subtype (butt/socket/tap).

    Returns ``(groups, by_type, by_shop_field, total_count, untagged)``:

    * ``groups`` — ``group_value -> weld_count``.
    * ``by_type`` — ``weld_type -> weld_count`` over every row (global breakdown).
    * ``by_shop_field`` — ``shop_field -> weld_count`` over every row (global).
    * ``total_count`` — total number of welds in scope.
    * ``untagged`` — ``{"weld_count"}`` for welds whose resolved line Tag is
      blank/placeholder/absent (reported regardless of group_by).
    """
    groups: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_shop_field: dict[str, int] = {}
    total_count = 0
    untagged = {"weld_count": 0}

    for r in rows:
        total_count += 1

        wtype = r["weld_type"]
        by_type[wtype] = by_type.get(wtype, 0) + 1

        sf = r["shop_field"]
        by_shop_field[sf] = by_shop_field.get(sf, 0) + 1

        # Las soldaduras sin línea válida se reportan SIEMPRE en 'untagged'.
        is_untagged = _is_blank_tag(r["line"])
        if is_untagged:
            untagged["weld_count"] += 1

        # --- clave de agrupación según group_by ----------------------------
        if group_by == "size":
            gkey = (
                _fmt_size(r["dia"], r["dia_unit"]) if r["dia"] is not None else "?"
            )
        elif group_by == "spec":
            spec = r["spec"]
            gkey = str(spec).strip() if spec and str(spec).strip() else "(sin spec)"
        elif group_by == "shop_field":
            gkey = sf
        elif group_by == "type":
            gkey = wtype
        else:  # "line"
            gkey = _NO_LINE_LABEL if is_untagged else str(r["line"]).strip()

        groups[gkey] = groups.get(gkey, 0) + 1

    return groups, by_type, by_shop_field, total_count, untagged


def weld_list(project: str, data: dict | None = None) -> dict:
    """Count and break down the welds of a Plant 3D project, grouped and filtered.

    Welds live in three dedicated tables in ``Piping.dcf`` — ``Buttweld``,
    ``Socketweld`` and ``TapWeld`` — one row per weld, each carrying ``PnPID``,
    ``Shop_Field`` and ``WeldNumber``. The weld subtype (``"butt"``/``"socket"``/
    ``"tap"``) is derived from which table a row comes from. Each weld joins 1:1
    to ``EngineeringItems`` by ``PnPID`` for its nominal diameter (size) and
    spec, and its line is resolved via ``P3dLineGroupPartRelationship`` →
    ``P3dLineGroup.Tag`` (weld.PnPID = relationship.Part ; relationship.LineGroup
    = P3dLineGroup.PnPID).

    ``WeldNumber`` is **not** used: it is NULL in practice (weld numbering is
    assigned on the isometrics), so this tool counts and breaks down welds, it
    does not number them.

    ``data`` accepts:

    * ``group_by`` — ``"line"`` (default) | ``"size"`` | ``"spec"`` |
      ``"shop_field"`` | ``"type"``: the key the returned groups are aggregated
      by. For ``"line"``, welds without a valid resolved line Tag are grouped
      under ``"(SIN LÍNEA)"``; for the others they fall into their natural group.
      An unrecognized value falls back to ``"line"`` with a note.
    * ``line`` — keep only welds whose resolved line Tag matches (``_norm``
      TRIM+UPPER, exact).
    * ``spec`` — keep only welds whose ``EngineeringItems.Spec`` matches
      (``_norm``, exact).
    * ``size`` — keep only a given nominal **diameter**; requires a unit
      (``{"value": <num>, "unit": "in"|"mm"}``); without a usable unit the filter
      is skipped and a note is added (same handling as :func:`list_components`).
    * ``shop_field`` — ``"shop"`` | ``"field"`` (normalized): keep only welds of
      that fabrication kind.
    * ``weld_type`` — ``"butt"`` | ``"socket"`` | ``"tap"``: keep only that
      subtype.
    * ``limit`` — max GROUPS returned (default 50, 0 = no cap). Omitted groups
      are reported via ``omitted``, never truncated silently.

    ``by_type`` and ``by_shop_field`` are global breakdowns (over the filtered
    scope) always present, independent of ``group_by``. Welds without a valid
    line are always reported in a top-level ``untagged`` field. The caller's
    ``data`` dict is never mutated.

    Schema robustness: each weld table and its columns (``PnPID``,
    ``Shop_Field``) are probed with ``PRAGMA table_info`` before querying. If
    NONE of the three tables exists the tool degrades gracefully (``ok: True``,
    zero welds, empty lists, explanatory note). Present-but-not-all tables are
    used with a note for the absent ones; a missing ``Shop_Field`` column makes
    that table's welds ``"(desconocido)"`` with a note. If the line-resolution
    tables/columns (``P3dLineGroupPartRelationship.Part``/``.LineGroup``,
    ``P3dLineGroup.PnPID``/``.Tag``) are absent, every weld degrades to untagged
    /``"(SIN LÍNEA)"`` plus a note instead of raising.
    """
    data = data or {}
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    # --- group_by (validado; default "line") -------------------------------
    group_by = str(data.get("group_by") or "line").strip().lower()
    notes: list[str] = []
    if group_by not in _WELD_GROUP_BY:
        notes.append(
            f"group_by '{group_by}' no reconocido; se usa 'line' "
            "(válidos: line, size, spec, shop_field, type)."
        )
        group_by = "line"

    limit = data.get("limit", _DEFAULT_LIMIT)

    # --- filtros de alcance (misma semántica que list_components) ----------
    line_filter = data.get("line")
    line_norm = _norm(line_filter) if line_filter else None
    spec_filter = data.get("spec")
    spec_norm = _norm(spec_filter) if spec_filter else None

    # shop_field: "shop" | "field" (normalizado). Valor no reconocido se ignora.
    shop_field_raw = data.get("shop_field")
    shop_field_norm: str | None = None
    if shop_field_raw is not None and str(shop_field_raw).strip():
        sf = _norm_shop_field(shop_field_raw)
        if sf in ("shop", "field"):
            shop_field_norm = sf
        else:
            notes.append(
                f"Filtro 'shop_field' '{shop_field_raw}' no reconocido "
                "(válidos: shop, field); se ignora."
            )

    # weld_type: "butt" | "socket" | "tap". Valor no reconocido se ignora.
    weld_type_raw = data.get("weld_type")
    weld_type_norm: str | None = None
    if weld_type_raw is not None and str(weld_type_raw).strip():
        wt = str(weld_type_raw).strip().lower()
        if wt in _WELD_TYPES:
            weld_type_norm = wt
        else:
            notes.append(
                f"Filtro 'weld_type' '{weld_type_raw}' no reconocido "
                "(válidos: butt, socket, tap); se ignora."
            )

    # size: filtra por DIÁMETRO nominal y exige unidad (no se adivina in/mm).
    size_raw = data.get("size")
    size_value: float | None = None
    size_unit: str | None = None
    if size_raw is not None:
        if isinstance(size_raw, dict):
            size_value = size_raw.get("value")
            size_unit = size_raw.get("unit")
        else:
            size_value = size_raw
        if size_value is None or not (size_unit and str(size_unit).strip()):
            notes.append(
                "Filtro 'size' ignorado: requiere unidad explícita "
                '({"value": <num>, "unit": "in"|"mm"}) para no mezclar in/mm. '
                "Filtra el diámetro nominal."
            )
            size_value = None
            size_unit = None

    def _filters_echo_weld() -> dict:
        """Echo de los filtros aplicados (estilo list_components)."""
        echo: dict = {}
        if line_norm is not None:
            echo["line"] = line_norm
        if spec_norm is not None:
            echo["spec"] = spec_norm
        if size_value is not None:
            echo["size"] = {"value": size_value, "unit": _norm(size_unit)}
        if shop_field_norm is not None:
            echo["shop_field"] = shop_field_norm
        if weld_type_norm is not None:
            echo["weld_type"] = weld_type_norm
        return echo

    def _base_response(extra_notes: list[str]) -> dict:
        """Respuesta degradada (ok) cuando no hay soldaduras que contar."""
        return {
            "ok": True,
            "project": project_dir.name,
            "path": str(project_dir),
            "limit": limit,
            "group_by": group_by,
            "filters": _filters_echo_weld(),
            "total_welds": 0,
            "by_type": [],
            "by_shop_field": [],
            "untagged": {"weld_count": 0},
            "group_count": 0,
            "omitted": 0,
            "groups": [],
            "notes": notes + extra_notes,
        }

    con = _connect_ro(db)
    try:
        # --- robustez de esquema: qué tablas de soldadura existen -----------
        # Para cada subtipo presente, comprobamos también si tiene Shop_Field
        # (PnPID es imprescindible para el cruce; si falta, la tabla se omite).
        present: dict[str, dict] = {}  # weld_type -> {"table", "have_shop_field"}
        absent_types: list[str] = []
        for wtype, table in _WELD_TABLES.items():
            if weld_type_norm is not None and wtype != weld_type_norm:
                # Si se filtró por un subtipo concreto, ignoramos los demás.
                continue
            if not _table_exists(con, table):
                absent_types.append(wtype)
                continue
            cols = _table_columns(con, table)
            if "PnPID" not in cols:
                absent_types.append(wtype)
                continue
            present[wtype] = {
                "table": table,
                "have_shop_field": "Shop_Field" in cols,
            }

        if not present:
            # Ninguna tabla de soldadura utilizable.
            if weld_type_norm is not None:
                msg = (
                    f"El proyecto no expone la tabla de soldaduras "
                    f"'{_WELD_TABLES[weld_type_norm]}' (subtipo '{weld_type_norm}') "
                    "en Piping.dcf."
                )
            else:
                msg = (
                    "El proyecto no expone ninguna tabla de soldaduras "
                    "(Buttweld/Socketweld/TapWeld) en Piping.dcf; las soldaduras "
                    "no están disponibles vía SQLite."
                )
            return _base_response([msg])

        if absent_types and weld_type_norm is None:
            faltan = ", ".join(_WELD_TABLES[t] for t in absent_types)
            notes.append(
                f"Tablas de soldadura ausentes (omitidas): {faltan}."
            )
        # Tablas presentes pero sin Shop_Field: sus soldaduras serán
        # "(desconocido)" en el desglose taller/campo.
        no_sf = [
            _WELD_TABLES[t] for t, info in present.items() if not info["have_shop_field"]
        ]
        if no_sf:
            notes.append(
                "Sin columna 'Shop_Field' en: "
                + ", ".join(no_sf)
                + f"; sus soldaduras se cuentan como '{_UNKNOWN_SHOP_FIELD}'."
            )

        # --- mapa PnPID de soldadura -> Tag de línea ------------------------
        # Resuelto vía P3dLineGroupPartRelationship (Part = PnPID del componente,
        # LineGroup = PnPID de la cabecera) ⨝ P3dLineGroup (Tag). Degradamos a
        # mapa vacío (todas untagged) si faltan tablas o columnas.
        line_by_pnpid: dict[str, str] = {}
        have_rel = _table_exists(
            con, "P3dLineGroupPartRelationship"
        ) and _table_exists(con, "P3dLineGroup")
        if have_rel:
            rel_cols = _table_columns(con, "P3dLineGroupPartRelationship")
            lg_cols = _table_columns(con, "P3dLineGroup")
            missing_rel = [c for c in ("Part", "LineGroup") if c not in rel_cols]
            missing_lg = [c for c in ("PnPID", "Tag") if c not in lg_cols]
            if missing_rel or missing_lg:
                have_rel = False
                faltan = ", ".join(
                    [f"P3dLineGroupPartRelationship.{c}" for c in missing_rel]
                    + [f"P3dLineGroup.{c}" for c in missing_lg]
                )
                notes.append(
                    "Columnas ausentes para resolver la línea de las soldaduras "
                    f"({faltan}): todas se reportan sin línea."
                )
        else:
            notes.append(
                "Sin tablas de relación línea-componente "
                "(P3dLineGroupPartRelationship / P3dLineGroup): las soldaduras "
                "se reportan sin línea."
            )
        if have_rel:
            try:
                rel_rows = con.execute(
                    """
                    SELECT rel.Part AS part, lg.Tag AS tag
                    FROM P3dLineGroupPartRelationship rel
                    JOIN P3dLineGroup lg ON lg.PnPID = rel.LineGroup
                    """
                ).fetchall()
            except sqlite3.Error:
                rel_rows = []
                notes.append(
                    "No se pudo resolver la línea de las soldaduras "
                    "(incompatibilidad de esquema): se reportan sin línea."
                )
            for r in rel_rows:
                part = r["part"]
                tag = r["tag"]
                if part is None:
                    continue
                if tag is not None and str(tag).strip():
                    # Un PnPID se asocia a una sola línea; el primero gana.
                    line_by_pnpid.setdefault(str(part), str(tag).strip())

        # --- SELECT plano por cada tabla de soldadura presente --------------
        # Casamos 1:1 con EngineeringItems por PnPID (spec + diámetro). La línea
        # se resuelve en Python con line_by_pnpid (no en SQL) igual que el resto
        # del módulo agrega tras leer.
        raw_rows: list[dict] = []
        cur = con.cursor()
        for wtype, info in present.items():
            sf_expr = "w.Shop_Field" if info["have_shop_field"] else "NULL"
            wrows = cur.execute(
                f"""
                SELECT
                    w.PnPID            AS pnpid,
                    {sf_expr}          AS shop_field,
                    ei.Spec            AS spec,
                    ei.NominalDiameter AS dia,
                    ei.NominalUnit     AS dia_unit
                FROM "{info['table']}" w
                LEFT JOIN EngineeringItems ei ON ei.PnPID = w.PnPID
                """
            ).fetchall()
            for r in wrows:
                pnpid = r["pnpid"]
                raw_rows.append(
                    {
                        "weld_type": wtype,
                        "shop_field": _norm_shop_field(r["shop_field"]),
                        "spec": r["spec"],
                        "dia": r["dia"],
                        "dia_unit": r["dia_unit"],
                        # Línea resuelta (raw Tag) o None si no hay relación.
                        "line": line_by_pnpid.get(str(pnpid)) if pnpid is not None else None,
                    }
                )
    finally:
        con.close()

    # --- aplicar filtros de alcance en Python (una sola pasada) ------------
    # weld_type ya se acotó al elegir las tablas presentes.
    apply_size = size_value is not None
    filtered: list[dict] = []
    for r in raw_rows:
        if line_norm is not None and _norm(r["line"]) != line_norm:
            continue
        if spec_norm is not None and _norm(r["spec"]) != spec_norm:
            continue
        if shop_field_norm is not None and r["shop_field"] != shop_field_norm:
            continue
        if apply_size:
            dia = r["dia"]
            if dia is None:
                continue
            try:
                if abs(float(dia) - float(size_value)) >= 1e-6:
                    continue
            except (TypeError, ValueError):
                continue
            if _norm(r["dia_unit"]) != _norm(size_unit):
                continue
        filtered.append(r)

    # --- agregación pura ----------------------------------------------------
    groups, by_type, by_shop_field, total_count, untagged = _build_weld_aggregates(
        filtered, group_by
    )

    # --- grupos de salida (ordenados por recuento desc, valor asc) ----------
    groups_out = [
        {"group": gkey, "weld_count": count}
        for gkey, count in sorted(
            groups.items(), key=lambda kv: (-kv[1], str(kv[0]))
        )
    ]
    group_count = len(groups_out)
    capped, omitted = _capped(groups_out, limit)

    # --- desgloses globales (siempre presentes), ranked desc ----------------
    by_type_ranked = [
        {"type": name, "count": count}
        for name, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    by_shop_field_ranked = [
        {"shop_field": name, "count": count}
        for name, count in sorted(by_shop_field.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    notes.append(
        "WeldNumber no se usa (NULL en el proyecto; la numeración se asigna en "
        "los isométricos): esta herramienta cuenta y desglosa, no numera."
    )
    notes.append(
        "Cobertura de línea parcial: un pequeño % de soldaduras puede no tener "
        "línea válida resuelta (van a 'untagged'), coherente con find_untagged."
    )
    notes.append(
        "Origen: tablas Buttweld/Socketweld/TapWeld de Piping.dcf; el subtipo "
        "(type) se deriva de la tabla de origen."
    )

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "limit": limit,
        "group_by": group_by,
        "filters": _filters_echo_weld(),
        "total_welds": total_count,
        "by_type": by_type_ranked,
        "by_shop_field": by_shop_field_ranked,
        "untagged": {"weld_count": untagged["weld_count"]},
        "group_count": group_count,
        "omitted": omitted,
        "groups": capped,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Bolt & gasket list (flange make-up material count / breakdown)
# ---------------------------------------------------------------------------

# Tablas dedicadas de material de montaje de bridas en Piping.dcf, una fila por
# item. La clave del mapa es la etiqueta de item_type expuesta en la salida; el
# valor es el nombre real de la tabla SQLite. NO se usa la tabla 'Fasteners'
# (superconjunto genérico que mezcla soldaduras, roscas, juntas y pernos).
_BG_TABLES = {
    "bolt": "BoltSet",
    "gasket": "Gasket",
}

# Tipos de item válidos para el filtro item_type (las claves de _BG_TABLES).
_BG_ITEM_TYPES = tuple(_BG_TABLES.keys())

# Claves canónicas de agrupación admitidas por bolt_gasket_list.
_BG_GROUP_BY = ("line", "size", "spec", "material", "item_type", "shop_field", "bolt_size")


def _bg_empty_metrics() -> dict:
    """Return a fresh, zeroed bolt/gasket metrics block.

    The bolt/gasket count is multi-metric (bolts and gaskets carry a different
    quantity semantics), so every aggregation bucket — ``totals``, each group
    and ``untagged`` — shares this shape:

    * ``item_count`` — number of rows (bolt sets + gaskets).
    * ``bolt_sets`` — number of ``BoltSet`` rows.
    * ``individual_bolts`` — Σ ``NumberInSet`` over the bolt sets.
    * ``gaskets`` — number of ``Gasket`` rows.
    """
    return {
        "item_count": 0,
        "bolt_sets": 0,
        "individual_bolts": 0,
        "gaskets": 0,
    }


def _bg_accumulate(metrics: dict, row: dict) -> None:
    """Add one bolt/gasket ``row`` to a metrics block (in place).

    ``row`` must expose ``item_type`` (``"bolt"``/``"gasket"``) and, for bolts,
    ``num_in_set`` (already parsed to a float count; gaskets count as 1 each).
    """
    metrics["item_count"] += 1
    if row["item_type"] == "bolt":
        metrics["bolt_sets"] += 1
        metrics["individual_bolts"] += row["num_in_set"]
    else:  # "gasket"
        metrics["gaskets"] += 1


def _build_bolt_gasket_aggregates(
    rows: list, group_by: str
) -> tuple[dict, dict, dict, dict, dict]:
    """Aggregate raw bolt/gasket rows into per-group metrics. Pure (no I/O).

    ``rows`` must expose ``item_type`` (``"bolt"``/``"gasket"``), ``num_in_set``
    (bolts: parsed ``NumberInSet`` count; gaskets: 0), ``shop_field`` (already
    normalized via :func:`_norm_shop_field`), ``line`` (resolved raw line Tag or
    None), ``spec``, ``material`` (sanitized None/empty -> None), ``dia`` and
    ``dia_unit`` (the flange nominal diameter from ``EngineeringItems``) and
    ``bolt_size`` (bolts only; the bolt diameter, e.g. ``5/8"``/``M16``).

    The grouping key depends on ``group_by``:

    * ``"line"`` — the raw resolved line Tag; items with a blank/placeholder or
      absent Tag (``_is_blank_tag``) are grouped under :data:`_NO_LINE_LABEL`.
    * ``"size"`` — the flange nominal diameter formatted with :func:`_fmt_size`,
      kept separate per unit (inches vs millimetres never merged).
    * ``"spec"`` — the ``EngineeringItems.Spec`` (None/empty -> ``"(sin spec)"``).
    * ``"material"`` — the ``EngineeringItems.Material`` (None -> ``"(sin)"``).
    * ``"item_type"`` — bolt/gasket.
    * ``"shop_field"`` — the normalized Shop_Field (shop/field/unknown).
    * ``"bolt_size"`` — the bolt diameter (gaskets have no bolt size -> ``"(sin)"``;
      meaningful chiefly alongside ``item_type="bolt"``).

    Returns ``(groups, by_item_type, by_shop_field, totals, untagged)`` where
    ``groups`` maps ``group_value -> metrics block`` and the rest are metrics
    blocks (``by_item_type``/``by_shop_field`` keyed by their breakdown value).
    """
    groups: dict[str, dict] = {}
    by_item_type: dict[str, dict] = {}
    by_shop_field: dict[str, dict] = {}
    totals = _bg_empty_metrics()
    untagged = _bg_empty_metrics()

    for r in rows:
        _bg_accumulate(totals, r)

        itype = r["item_type"]
        _bg_accumulate(by_item_type.setdefault(itype, _bg_empty_metrics()), r)

        sf = r["shop_field"]
        _bg_accumulate(by_shop_field.setdefault(sf, _bg_empty_metrics()), r)

        # Los items sin línea válida se reportan SIEMPRE en 'untagged'.
        is_untagged = _is_blank_tag(r["line"])
        if is_untagged:
            _bg_accumulate(untagged, r)

        # --- clave de agrupación según group_by ----------------------------
        if group_by == "size":
            gkey = (
                _fmt_size(r["dia"], r["dia_unit"]) if r["dia"] is not None else "?"
            )
        elif group_by == "spec":
            spec = r["spec"]
            gkey = str(spec).strip() if spec and str(spec).strip() else "(sin spec)"
        elif group_by == "material":
            mat = r["material"]
            gkey = str(mat).strip() if mat and str(mat).strip() else "(sin)"
        elif group_by == "item_type":
            gkey = itype
        elif group_by == "shop_field":
            gkey = sf
        elif group_by == "bolt_size":
            bs = r["bolt_size"]
            gkey = str(bs).strip() if bs and str(bs).strip() else "(sin)"
        else:  # "line"
            gkey = _NO_LINE_LABEL if is_untagged else str(r["line"]).strip()

        _bg_accumulate(groups.setdefault(gkey, _bg_empty_metrics()), r)

    return groups, by_item_type, by_shop_field, totals, untagged


def bolt_gasket_list(project: str, data: dict | None = None) -> dict:
    """Count and break down the bolts and gaskets of a Plant 3D project.

    Bolts and gaskets (flange make-up material) live in two dedicated tables in
    ``Piping.dcf`` — ``BoltSet`` (one row per bolt set) and ``Gasket`` (one row
    per gasket) — each carrying ``PnPID`` and ``Shop_Field``. The generic
    ``Fasteners`` table is **not** used (it is a superset mixing welds, threaded
    joints, gaskets and bolts). Each item joins 1:1 to ``EngineeringItems`` by
    ``PnPID`` for its spec, flange nominal diameter (``NominalDiameter`` +
    ``NominalUnit``), ``Material`` (sanitized to None) and, when present,
    ``PressureClass``/``Facing``. Its line is resolved via
    ``P3dLineGroupPartRelationship`` → ``P3dLineGroup.Tag`` (item.PnPID =
    relationship.Part ; relationship.LineGroup = P3dLineGroup.PnPID).

    Quantity is multi-metric: ``BoltSet.NumberInSet`` (TEXT, mixed formats —
    parsed with ``float``; non-numeric values contribute 0 with a note) is the
    number of bolts per set, while each ``Gasket`` row is one gasket. Every
    aggregation bucket reports ``item_count`` (rows), ``bolt_sets``,
    ``individual_bolts`` (Σ ``NumberInSet``) and ``gaskets``.

    ``data`` accepts:

    * ``group_by`` — ``"line"`` (default) | ``"size"`` | ``"spec"`` |
      ``"material"`` | ``"item_type"`` | ``"shop_field"`` | ``"bolt_size"``: the
      key the returned groups are aggregated by. For ``"line"``, items without a
      valid resolved line Tag are grouped under ``"(SIN LÍNEA)"``; for the others
      they fall into their natural group. ``"bolt_size"`` only applies to bolts
      (gaskets fall into ``"(sin)"``). An unrecognized value falls back to
      ``"line"`` with a note.
    * ``item_type`` — ``"bolt"`` | ``"gasket"`` (omitted = both): chooses which
      table(s) to read.
    * ``line`` — keep only items whose resolved line Tag matches (``_norm``,
      exact).
    * ``spec`` — keep only items whose ``EngineeringItems.Spec`` matches
      (``_norm``, exact).
    * ``size`` — keep only a given flange nominal **diameter**; requires a unit
      (``{"value": <num>, "unit": "in"|"mm"}``); without a usable unit the filter
      is skipped and a note is added.
    * ``shop_field`` — ``"shop"`` | ``"field"`` (normalized): keep only items of
      that fabrication kind.
    * ``limit`` — max GROUPS returned (default 50, 0 = no cap). Omitted groups
      are reported via ``omitted``, never truncated silently.

    ``by_item_type`` and ``by_shop_field`` are global breakdowns (over the
    filtered scope) always present, independent of ``group_by``. Items without a
    valid line are always reported in a top-level ``untagged`` field. The
    caller's ``data`` dict is never mutated.

    Schema robustness: each table and its columns are probed with ``PRAGMA
    table_info`` before querying. If NEITHER ``BoltSet`` nor ``Gasket`` exists
    the tool degrades gracefully (``ok: True``, zero totals, empty lists,
    explanatory note); if only one exists it is used with a note. Optional
    columns (``NumberInSet``, ``BoltCompatibleStd``, ``Facing``,
    ``PressureClass``) degrade to None/0 with a note when absent. If the
    line-resolution tables/columns are missing, every item degrades to
    untagged/``"(SIN LÍNEA)"`` plus a note instead of raising.
    """
    data = data or {}
    project_dir = resolve_project_dir(project)
    db = _db_path(project_dir, "Piping.dcf")

    # --- group_by (validado; default "line") -------------------------------
    group_by = str(data.get("group_by") or "line").strip().lower()
    notes: list[str] = []
    if group_by not in _BG_GROUP_BY:
        notes.append(
            f"group_by '{group_by}' no reconocido; se usa 'line' "
            "(válidos: line, size, spec, material, item_type, shop_field, "
            "bolt_size)."
        )
        group_by = "line"

    limit = data.get("limit", _DEFAULT_LIMIT)

    # --- filtros de alcance (misma semántica que weld_list) ----------------
    line_filter = data.get("line")
    line_norm = _norm(line_filter) if line_filter else None
    spec_filter = data.get("spec")
    spec_norm = _norm(spec_filter) if spec_filter else None

    # shop_field: "shop" | "field" (normalizado). Valor no reconocido se ignora.
    shop_field_raw = data.get("shop_field")
    shop_field_norm: str | None = None
    if shop_field_raw is not None and str(shop_field_raw).strip():
        sf = _norm_shop_field(shop_field_raw)
        if sf in ("shop", "field"):
            shop_field_norm = sf
        else:
            notes.append(
                f"Filtro 'shop_field' '{shop_field_raw}' no reconocido "
                "(válidos: shop, field); se ignora."
            )

    # item_type: "bolt" | "gasket". Valor no reconocido se ignora (= ambos).
    item_type_raw = data.get("item_type")
    item_type_norm: str | None = None
    if item_type_raw is not None and str(item_type_raw).strip():
        it = str(item_type_raw).strip().lower()
        if it in _BG_ITEM_TYPES:
            item_type_norm = it
        else:
            notes.append(
                f"Filtro 'item_type' '{item_type_raw}' no reconocido "
                "(válidos: bolt, gasket); se ignora."
            )

    # size: filtra por DIÁMETRO de brida y exige unidad (no se adivina in/mm).
    size_raw = data.get("size")
    size_value: float | None = None
    size_unit: str | None = None
    if size_raw is not None:
        if isinstance(size_raw, dict):
            size_value = size_raw.get("value")
            size_unit = size_raw.get("unit")
        else:
            size_value = size_raw
        if size_value is None or not (size_unit and str(size_unit).strip()):
            notes.append(
                "Filtro 'size' ignorado: requiere unidad explícita "
                '({"value": <num>, "unit": "in"|"mm"}) para no mezclar in/mm. '
                "Filtra el diámetro nominal de la brida."
            )
            size_value = None
            size_unit = None

    def _filters_echo_bg() -> dict:
        """Echo de los filtros aplicados (estilo weld_list)."""
        echo: dict = {}
        if line_norm is not None:
            echo["line"] = line_norm
        if spec_norm is not None:
            echo["spec"] = spec_norm
        if size_value is not None:
            echo["size"] = {"value": size_value, "unit": _norm(size_unit)}
        if shop_field_norm is not None:
            echo["shop_field"] = shop_field_norm
        if item_type_norm is not None:
            echo["item_type"] = item_type_norm
        return echo

    def _base_response(extra_notes: list[str]) -> dict:
        """Respuesta degradada (ok) cuando no hay items que contar."""
        return {
            "ok": True,
            "project": project_dir.name,
            "path": str(project_dir),
            "limit": limit,
            "group_by": group_by,
            "filters": _filters_echo_bg(),
            "totals": _bg_empty_metrics(),
            "by_item_type": [],
            "by_shop_field": [],
            "untagged": _bg_empty_metrics(),
            "group_count": 0,
            "omitted": 0,
            "groups": [],
            "notes": notes + extra_notes,
        }

    con = _connect_ro(db)
    try:
        # --- robustez de esquema: qué tablas existen y con qué columnas -----
        # PnPID es imprescindible para el cruce; si falta, la tabla se omite.
        # Columnas opcionales (NumberInSet, BoltCompatibleStd, Facing,
        # PressureClass, Material) se sondean por PRAGMA y degradan a None/0.
        present: dict[str, dict] = {}  # item_type -> info de columnas
        absent_types: list[str] = []
        no_num_in_set: list[str] = []
        no_sf: list[str] = []
        for itype, table in _BG_TABLES.items():
            if item_type_norm is not None and itype != item_type_norm:
                # Si se filtró por un item_type concreto, ignoramos el otro.
                continue
            if not _table_exists(con, table):
                absent_types.append(itype)
                continue
            cols = _table_columns(con, table)
            if "PnPID" not in cols:
                absent_types.append(itype)
                continue
            have_num = "NumberInSet" in cols
            if itype == "bolt" and not have_num:
                no_num_in_set.append(table)
            have_sf = "Shop_Field" in cols
            if not have_sf:
                no_sf.append(table)
            present[itype] = {
                "table": table,
                "have_num_in_set": have_num,
                "have_shop_field": have_sf,
                "have_bolt_size": "BoltSize" in cols,
            }

        if not present:
            # Ninguna tabla de montaje de bridas utilizable.
            if item_type_norm is not None:
                msg = (
                    f"El proyecto no expone la tabla '{_BG_TABLES[item_type_norm]}' "
                    f"(item_type '{item_type_norm}') en Piping.dcf."
                )
            else:
                msg = (
                    "El proyecto no expone las tablas BoltSet/Gasket en "
                    "Piping.dcf; los pernos y juntas no están disponibles vía "
                    "SQLite."
                )
            return _base_response([msg])

        if absent_types and item_type_norm is None:
            faltan = ", ".join(_BG_TABLES[t] for t in absent_types)
            notes.append(f"Tablas de montaje de bridas ausentes (omitidas): {faltan}.")
        if no_num_in_set:
            notes.append(
                "Sin columna 'NumberInSet' en: "
                + ", ".join(no_num_in_set)
                + "; individual_bolts no puede calcularse para esas filas (0)."
            )
        if no_sf:
            notes.append(
                "Sin columna 'Shop_Field' en: "
                + ", ".join(no_sf)
                + f"; esos items se cuentan como '{_UNKNOWN_SHOP_FIELD}'."
            )

        # --- mapa PnPID de item -> Tag de línea -----------------------------
        # Resuelto vía P3dLineGroupPartRelationship (Part = PnPID del componente,
        # LineGroup = PnPID de la cabecera) ⨝ P3dLineGroup (Tag). Degradamos a
        # mapa vacío (todos untagged) si faltan tablas o columnas.
        line_by_pnpid: dict[str, str] = {}
        have_rel = _table_exists(
            con, "P3dLineGroupPartRelationship"
        ) and _table_exists(con, "P3dLineGroup")
        if have_rel:
            rel_cols = _table_columns(con, "P3dLineGroupPartRelationship")
            lg_cols = _table_columns(con, "P3dLineGroup")
            missing_rel = [c for c in ("Part", "LineGroup") if c not in rel_cols]
            missing_lg = [c for c in ("PnPID", "Tag") if c not in lg_cols]
            if missing_rel or missing_lg:
                have_rel = False
                faltan = ", ".join(
                    [f"P3dLineGroupPartRelationship.{c}" for c in missing_rel]
                    + [f"P3dLineGroup.{c}" for c in missing_lg]
                )
                notes.append(
                    "Columnas ausentes para resolver la línea de pernos/juntas "
                    f"({faltan}): todos se reportan sin línea."
                )
        else:
            notes.append(
                "Sin tablas de relación línea-componente "
                "(P3dLineGroupPartRelationship / P3dLineGroup): los pernos y "
                "juntas se reportan sin línea."
            )
        if have_rel:
            try:
                rel_rows = con.execute(
                    """
                    SELECT rel.Part AS part, lg.Tag AS tag
                    FROM P3dLineGroupPartRelationship rel
                    JOIN P3dLineGroup lg ON lg.PnPID = rel.LineGroup
                    """
                ).fetchall()
            except sqlite3.Error:
                rel_rows = []
                notes.append(
                    "No se pudo resolver la línea de pernos/juntas "
                    "(incompatibilidad de esquema): se reportan sin línea."
                )
            for r in rel_rows:
                part = r["part"]
                tag = r["tag"]
                if part is None:
                    continue
                if tag is not None and str(tag).strip():
                    # Un PnPID se asocia a una sola línea; el primero gana.
                    line_by_pnpid.setdefault(str(part), str(tag).strip())

        # --- SELECT plano por cada tabla presente ---------------------------
        # Casamos 1:1 con EngineeringItems por PnPID (spec + diámetro de brida +
        # material). La línea se resuelve en Python con line_by_pnpid (no en SQL),
        # igual que el resto del módulo agrega tras leer.
        raw_rows: list[dict] = []
        bad_num_in_set = 0  # nº de NumberInSet no numéricos (contribuyen 0)
        cur = con.cursor()
        for itype, info in present.items():
            sf_expr = "x.Shop_Field" if info["have_shop_field"] else "NULL"
            if itype == "bolt":
                num_expr = "x.NumberInSet" if info["have_num_in_set"] else "NULL"
                bsize_expr = "x.BoltSize" if info["have_bolt_size"] else "NULL"
            else:
                num_expr = "NULL"
                bsize_expr = "NULL"
            irows = cur.execute(
                f"""
                SELECT
                    x.PnPID            AS pnpid,
                    {sf_expr}          AS shop_field,
                    {num_expr}         AS num_in_set,
                    {bsize_expr}       AS bolt_size,
                    ei.Spec            AS spec,
                    ei.Material        AS material,
                    ei.NominalDiameter AS dia,
                    ei.NominalUnit     AS dia_unit
                FROM "{info['table']}" x
                LEFT JOIN EngineeringItems ei ON ei.PnPID = x.PnPID
                """
            ).fetchall()
            for r in irows:
                pnpid = r["pnpid"]
                # NumberInSet es TEXTO con formatos mezclados ('4', '4.0', ...).
                # float() lo parsea; valores no numéricos -> 0 (no se lanza).
                num_in_set = 0.0
                if itype == "bolt":
                    raw_num = r["num_in_set"]
                    if raw_num is not None:
                        try:
                            num_in_set = float(raw_num)
                        except (TypeError, ValueError):
                            bad_num_in_set += 1
                # Material puede ser None o '' -> se sanea a None.
                mat = r["material"]
                material = (
                    str(mat) if mat is not None and str(mat).strip() else None
                )
                raw_rows.append(
                    {
                        "item_type": itype,
                        "shop_field": _norm_shop_field(r["shop_field"]),
                        "num_in_set": num_in_set,
                        "bolt_size": r["bolt_size"],
                        "spec": r["spec"],
                        "material": material,
                        "dia": r["dia"],
                        "dia_unit": r["dia_unit"],
                        # Línea resuelta (raw Tag) o None si no hay relación.
                        "line": line_by_pnpid.get(str(pnpid))
                        if pnpid is not None
                        else None,
                    }
                )
    finally:
        con.close()

    if bad_num_in_set:
        notes.append(
            f"NumberInSet con {bad_num_in_set} valor(es) no numérico(s): "
            "contribuyen 0 a individual_bolts."
        )

    # --- aplicar filtros de alcance en Python (una sola pasada) ------------
    # item_type ya se acotó al elegir las tablas presentes.
    apply_size = size_value is not None
    filtered: list[dict] = []
    for r in raw_rows:
        if line_norm is not None and _norm(r["line"]) != line_norm:
            continue
        if spec_norm is not None and _norm(r["spec"]) != spec_norm:
            continue
        if shop_field_norm is not None and r["shop_field"] != shop_field_norm:
            continue
        if apply_size:
            dia = r["dia"]
            if dia is None:
                continue
            try:
                if abs(float(dia) - float(size_value)) >= 1e-6:
                    continue
            except (TypeError, ValueError):
                continue
            if _norm(r["dia_unit"]) != _norm(size_unit):
                continue
        filtered.append(r)

    # --- agregación pura ----------------------------------------------------
    (
        groups,
        by_item_type,
        by_shop_field,
        totals,
        untagged,
    ) = _build_bolt_gasket_aggregates(filtered, group_by)

    # individual_bolts es una suma de floats; lo exponemos como int (los conteos
    # de pernos son enteros, aunque NumberInSet venga como '4.0').
    def _expose(m: dict) -> dict:
        return {
            "item_count": m["item_count"],
            "bolt_sets": m["bolt_sets"],
            "individual_bolts": int(round(m["individual_bolts"])),
            "gaskets": m["gaskets"],
        }

    # --- grupos de salida (ordenados por item_count desc, valor asc) --------
    groups_out = [
        {"group": gkey, **_expose(m)}
        for gkey, m in sorted(
            groups.items(), key=lambda kv: (-kv[1]["item_count"], str(kv[0]))
        )
    ]
    group_count = len(groups_out)
    capped, omitted = _capped(groups_out, limit)

    # --- desgloses globales (siempre presentes), ranked desc ----------------
    by_item_type_ranked = [
        {
            "item_type": name,
            "item_count": m["item_count"],
            "individual_bolts": int(round(m["individual_bolts"])),
        }
        for name, m in sorted(
            by_item_type.items(), key=lambda kv: (-kv[1]["item_count"], kv[0])
        )
    ]
    by_shop_field_ranked = [
        {"shop_field": name, "item_count": m["item_count"]}
        for name, m in sorted(
            by_shop_field.items(), key=lambda kv: (-kv[1]["item_count"], kv[0])
        )
    ]

    notes.append(
        "Origen: tablas BoltSet/Gasket de Piping.dcf (NO la tabla genérica "
        "Fasteners). El item_type se deriva de la tabla de origen."
    )
    notes.append(
        "individual_bolts proviene de BoltSet.NumberInSet (TEXTO parseado); "
        "cada Gasket cuenta como 1 junta (sin cantidad propia)."
    )
    notes.append(
        "NominalDiameter es el diámetro de la BRIDA; BoltSize es el diámetro del "
        "perno (solo en pernos)."
    )
    notes.append(
        "Cobertura de línea parcial: un % de pernos/juntas puede no tener línea "
        "válida resuelta (~20% en proyectos reales; van a 'untagged'), coherente "
        "con find_untagged."
    )
    notes.append(
        "No localiza el objeto en el dibujo (sin handle/GUID en SQLite; "
        "requeriría el plugin .NET)."
    )

    return {
        "ok": True,
        "project": project_dir.name,
        "path": str(project_dir),
        "limit": limit,
        "group_by": group_by,
        "filters": _filters_echo_bg(),
        "totals": _expose(totals),
        "by_item_type": by_item_type_ranked,
        "by_shop_field": by_shop_field_ranked,
        "untagged": _expose(untagged),
        "group_count": group_count,
        "omitted": omitted,
        "groups": capped,
        "notes": notes,
    }
