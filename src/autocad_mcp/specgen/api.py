"""Reusable, UI-agnostic orchestration of the specgen pipeline.

Both the CLI (``python -m autocad_mcp.specgen``) and the MCP ``specgen`` tool call these
functions so the phases (PARSE -> EXTEND -> MATCH -> REPORT -> BUILD -> VERIFY) live in ONE
place. Every function returns a plain-``dict`` result (JSON-serialisable, no ``bytes``/GUIDs)
so it can be emitted straight to the MCP client; the CLI renders it as text.

Nothing here writes to the input catalogs: the ``-H2`` extension always works on copies inside
``out``. ``analyze`` writes nothing unless an ``out`` is given (then only the review workbook).
"""

from __future__ import annotations

import os

from .catalog_extender import (
    deduce_h2_targets,
    extend_catalogs,
    verify as verify_h2,
)
from .catalog_index import CatalogIndex, discover_catalogs
from .matcher import CatalogMatcher
from .piping_class import parse_workbook
from .report import coverage, write_review_xlsx
from .spec_builder import ComponentRef, Materialiser, build_pspx, make_definition, verify


# --------------------------------------------------------------------------- helpers
def _match_entries(entries, match_dir: str):
    """Match every entry in place against the catalogs in ``match_dir``; index is closed."""
    index = CatalogIndex(match_dir)
    try:
        matcher = CatalogMatcher(index)
        for e in entries:
            matcher.match(e)
    finally:
        index.close()


def _gaps(entries) -> list[dict]:
    """The unmatched (no SizeRecordId) entries as review-friendly dicts."""
    gaps = []
    for e in entries:
        if e.size_record_id is None:
            gaps.append({
                "sheet": e.sheet,
                "description": (e.description.splitlines()[0] if e.description else "")[:120],
                "type": e.type_,
                "lcode": e.lcode or "",
                "diameter_in": e.main_diameter,
                "confidence": e.confidence,
                "note": e.match_note,
            })
    return gaps


def _by_family(entries) -> dict[str, int]:
    """Entry counts grouped by piping-class sheet (family)."""
    out: dict[str, int] = {}
    for e in entries:
        out[e.sheet] = out.get(e.sheet, 0) + 1
    return dict(sorted(out.items()))


def _extend(entries, catalogs_dir: str, out_dir: str) -> dict:
    """Run the -H2 extension into ``out_dir/catalogs`` and return a structured summary.

    Returns ``{match_dir, catalogs, families_created, rows_created, lcodes_covered,
    per_catalog, warnings}``. ``match_dir`` is where the caller should match afterwards.
    """
    src_paths = [p for _logical, p in discover_catalogs(catalogs_dir)]
    targets = deduce_h2_targets(entries, {p: p for p in src_paths})
    ext_dir = os.path.join(out_dir, "catalogs")
    extenders = extend_catalogs(targets, ext_dir)
    targets_by_fname = {os.path.basename(p): lc for p, lc in targets.items()}
    rep = verify_h2(extenders, ext_dir, targets_by_fname)

    n_fam = sum(e.families_created for e in extenders.values())
    n_rows = sum(e.rows_created for e in extenders.values())
    lcodes = sorted({lc for codes in targets.values() for lc in codes})
    warnings = []
    for fname, r in rep.items():
        if r["integrity_check"] != "ok":
            warnings.append(f"integrity_check != ok en {fname}")
        if not r["graph_consistent"]:
            warnings.append(f"grafo inconsistente en {fname}")
    return {
        "match_dir": ext_dir,
        "catalogs": sorted(extenders.keys()),
        "families_created": n_fam,
        "rows_created": n_rows,
        "lcodes_covered": lcodes,
        "per_catalog": {
            f: {"families_created": r["families_created"],
                "rows_created": r["rows_created"],
                "integrity_check": r["integrity_check"],
                "graph_consistent": r["graph_consistent"]}
            for f, r in rep.items()
        },
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- public API
def analyze(piping_class: str, catalogs_dir: str, out_dir: str | None = None,
            extend_h2: bool = False) -> dict:
    """PARSE + MATCH only (no spec built). Coverage + per-family counts + gap list.

    Read-only unless ``out_dir`` is given, in which case ``REVISION_MATCHING.xlsx`` is written
    there and its path returned. Set ``extend_h2`` to match against the -H2-extended catalogs
    (writes the extended copies under ``out_dir/catalogs``; requires ``out_dir``).
    """
    piping_class = os.path.abspath(piping_class)
    catalogs_dir = os.path.abspath(catalogs_dir)

    entries = parse_workbook(piping_class)
    if not entries:
        return {"ok": False, "error": "El piping class no produjo ninguna entrada."}

    match_dir = catalogs_dir
    extend_info = None
    if extend_h2:
        if not out_dir:
            return {"ok": False,
                    "error": "extend_h2 requiere 'out' (los catalogos -H2 se escriben en copias)."}
        os.makedirs(os.path.abspath(out_dir), exist_ok=True)
        extend_info = _extend(entries, catalogs_dir, os.path.abspath(out_dir))
        match_dir = extend_info["match_dir"]

    _match_entries(entries, match_dir)
    cov = coverage(entries)

    review_path = None
    if out_dir:
        out_dir = os.path.abspath(out_dir)
        os.makedirs(out_dir, exist_ok=True)
        review_path = os.path.join(out_dir, "REVISION_MATCHING.xlsx")
        write_review_xlsx(entries, review_path)

    result = {
        "ok": True,
        "piping_class": piping_class,
        "catalogs": catalogs_dir,
        "sheets": len({e.sheet for e in entries}),
        "coverage": cov,
        "by_family": _by_family(entries),
        "gaps": _gaps(entries),
        "review_xlsx": review_path,
    }
    if extend_info is not None:
        result["extend_h2"] = extend_info
    return result


def build(piping_class: str, catalogs_dir: str, out_dir: str, spec_name: str | None = None,
          extend_h2: bool = False, template_pspc: str | None = None) -> dict:
    """Full pipeline: PARSE -> (EXTEND) -> MATCH -> REPORT -> BUILD -> VERIFY.

    Returns ``{ok, files, coverage, verify, extend_h2?}`` with absolute file paths and the
    verification summary. ``ok`` is True only when the built spec passes every integrity gate.
    """
    piping_class = os.path.abspath(piping_class)
    catalogs_dir = os.path.abspath(catalogs_dir)
    out_dir = os.path.abspath(out_dir)
    spec_name = spec_name or os.path.splitext(os.path.basename(piping_class))[0]

    template_pspx = None
    if template_pspc:
        template_pspc = os.path.abspath(template_pspc)
        cand = os.path.splitext(template_pspc)[0] + ".pspx"
        template_pspx = cand if os.path.exists(cand) else None

    os.makedirs(out_dir, exist_ok=True)

    entries = parse_workbook(piping_class)
    if not entries:
        return {"ok": False, "error": "El piping class no produjo ninguna entrada."}

    match_dir = catalogs_dir
    extend_info = None
    if extend_h2:
        extend_info = _extend(entries, catalogs_dir, out_dir)
        match_dir = extend_info["match_dir"]

    index = CatalogIndex(match_dir)
    try:
        matcher = CatalogMatcher(index)
        for e in entries:
            matcher.match(e)
        cov = coverage(entries)

        review_path = os.path.join(out_dir, "REVISION_MATCHING.xlsx")
        write_review_xlsx(entries, review_path)

        components = [
            ComponentRef(
                class_name=e.catalog_class or "Pipe",
                size_record_id=e.size_record_id,
                part_family_id=e.part_family_id,
                pnpid_source=e.pnpid,
            )
            for e in entries
            if e.size_record_id is not None
        ]
        seed_pspc = template_pspc if (template_pspc and os.path.exists(template_pspc)) \
            else index.references()[0][1]
        defin = make_definition(
            name=spec_name,
            description=f"Spec generada por specgen desde {os.path.basename(piping_class)}",
            components=components,
            template_pspx=template_pspx,
        )
        out_pspc = os.path.join(out_dir, f"{spec_name}.pspc")
        out_pspx = os.path.join(out_dir, f"{spec_name}.pspx")
        mat = Materialiser(out_pspc, defin, index, seed_pspc=seed_pspc,
                           template_pspc=template_pspc)
        mat.build()
        build_pspx(out_pspx, defin, out_pspc, index.references())
        vr = verify(out_pspc, out_pspx, mat)
    finally:
        index.close()

    ok = (vr["integrity_check"] == "ok" and vr["graph_consistent"]
          and vr["guid_16byte_bad"] == 0 and vr["pspx"]["opens_as_zip"]
          and not vr["pspx"]["parse_errors"])

    files = {"pspc": out_pspc, "pspx": out_pspx, "review_xlsx": review_path}
    if extend_info is not None:
        files["catalogs_h2_dir"] = extend_info["match_dir"]
    result = {
        "ok": ok,
        "spec_name": spec_name,
        "files": files,
        "components_built": len(components),
        "coverage": cov,
        "verify": vr,
    }
    if extend_info is not None:
        result["extend_h2"] = extend_info
    return result


def extend_catalog(piping_class: str, catalogs_dir: str, out_dir: str) -> dict:
    """Create the ``-H2`` variant families on catalog copies under ``out_dir/catalogs``.

    Returns the same structured summary as the extend phase of ``build``, plus ``ok`` and the
    output directory.
    """
    piping_class = os.path.abspath(piping_class)
    catalogs_dir = os.path.abspath(catalogs_dir)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    entries = parse_workbook(piping_class)
    if not entries:
        return {"ok": False, "error": "El piping class no produjo ninguna entrada."}

    info = _extend(entries, catalogs_dir, out_dir)
    return {
        "ok": not info["warnings"],
        "piping_class": piping_class,
        "out_dir": out_dir,
        **info,
    }
