"""``python -m specgen build ...`` -- generate a Plant 3D spec from a piping-class Excel.

Standalone proof of concept (NOT part of the MCP server). ``openpyxl`` plus :mod:`specgen`.

Pipeline (all generalised, nothing hard-coded):
  1. PARSE   the piping-class Excel into normalised entries.
  2. EXTEND  (optional ``--extend-h2``): deduce the ``-H2`` variants from the entries, clone the
     base families into dedicated ``-H2`` families on COPIES of the catalogs, and point the matcher
     at the extended set so the H2 rows resolve to a real family.
  3. MATCH   every entry against the catalog directory with the confidence model.
  4. REPORT  the ``REVISION_MATCHING.xlsx`` review workbook + a textual coverage summary.
  5. BUILD   materialise the matched parts into ``<name>.pspc`` + ``<name>.pspx``; the branch table
     comes from ``--template-pspc`` (its sibling ``.pspx``) or is emitted minimal.
  6. VERIFY  integrity_check + graph consistency + valid ``.pspx`` ZIP/XML, printed as a summary.

Usage:
  python -m specgen build --piping-class CLASS.xlsx --catalogs DIR --out DIR
      [--spec-name NAME] [--extend-h2] [--template-pspc PATH]
"""

from __future__ import annotations

import argparse
import os
import sys

from . import spec_builder
from .catalog_extender import (
    deduce_h2_targets,
    extend_catalogs,
    verify as verify_h2,
)
from .catalog_index import CatalogIndex, discover_catalogs
from .matcher import CatalogMatcher
from .piping_class import parse_workbook
from .report import coverage, format_coverage, write_review_xlsx
from .spec_builder import ComponentRef, Materialiser, build_pspx, make_definition, verify


def _build(args: argparse.Namespace) -> int:
    piping_class = os.path.abspath(args.piping_class)
    catalogs_dir = os.path.abspath(args.catalogs)
    out_dir = os.path.abspath(args.out)
    spec_name = args.spec_name or os.path.splitext(os.path.basename(piping_class))[0]
    template_pspc = os.path.abspath(args.template_pspc) if args.template_pspc else None
    template_pspx = None
    if template_pspc:
        cand = os.path.splitext(template_pspc)[0] + ".pspx"
        template_pspx = cand if os.path.exists(cand) else None

    if not os.path.exists(piping_class):
        print(f"ERROR: no existe el piping class: {piping_class}", file=sys.stderr)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    # --- 1. PARSE -----------------------------------------------------------
    print("== 1. parsear piping class ==")
    entries = parse_workbook(piping_class, warn=lambda m: print(f"  aviso: {m}"))
    print(f"  entradas: {len(entries)} en {len({e.sheet for e in entries})} hojas")
    if not entries:
        print("ERROR: el piping class no produjo ninguna entrada", file=sys.stderr)
        return 1

    # --- 2. EXTEND H2 (optional) -------------------------------------------
    match_dir = catalogs_dir
    if args.extend_h2:
        print("\n== 2. ampliar catalogos con variantes -H2 ==")
        src_paths = [p for _logical, p in discover_catalogs(catalogs_dir)]
        targets = deduce_h2_targets(entries, {p: p for p in src_paths})
        ext_dir = os.path.join(out_dir, "catalogs")
        extenders = extend_catalogs(targets, ext_dir)
        targets_by_fname = {os.path.basename(p): lc for p, lc in targets.items()}
        rep = verify_h2(extenders, ext_dir, targets_by_fname)
        n_fam = sum(e.families_created for e in extenders.values())
        n_rows = sum(e.rows_created for e in extenders.values())
        n_codes = sum(len(lc) for lc in targets.values())
        print(f"  catalogos copiados: {len(extenders)}  L-codes base ampliados: {n_codes}  "
              f"familias -H2: {n_fam}  filas nuevas: {n_rows}")
        bad_integrity = [f for f, r in rep.items() if r["integrity_check"] != "ok"]
        bad_graph = [f for f, r in rep.items() if not r["graph_consistent"]]
        if bad_integrity:
            print(f"  AVISO: integrity_check != ok en {bad_integrity}")
        if bad_graph:
            print(f"  AVISO: grafo inconsistente en {bad_graph}")
        match_dir = ext_dir

    # --- 3. MATCH -----------------------------------------------------------
    print("\n== 3. emparejar contra catalogos ==")
    index = CatalogIndex(match_dir)
    try:
        matcher = CatalogMatcher(index)
        for e in entries:
            matcher.match(e)

        cov = coverage(entries)
        print(format_coverage(cov))

        # --- 4. REPORT ------------------------------------------------------
        review_path = os.path.join(out_dir, "REVISION_MATCHING.xlsx")
        write_review_xlsx(entries, review_path)
        print(f"\n  informe de revision: {review_path}")

        # --- 5. BUILD -------------------------------------------------------
        print("\n== 5. construir spec ==")
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
        print(f"  componentes a materializar: {len(components)}")

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

        # --- 6. VERIFY ------------------------------------------------------
        result = verify(out_pspc, out_pspx, mat)
    finally:
        index.close()

    _print_verify(result, out_pspc, out_pspx)
    ok = (result["integrity_check"] == "ok" and result["graph_consistent"]
          and result["guid_16byte_bad"] == 0 and result["pspx"]["opens_as_zip"]
          and not result["pspx"]["parse_errors"])
    print(f"\nspec generada: {'OK' if ok else 'CON AVISOS'}  ->  {out_pspc}")
    return 0 if ok else 1


def _print_verify(r: dict, out_pspc: str, out_pspx: str) -> None:
    print("\n== 6. verificacion ==")
    print(f"  integrity_check: {r['integrity_check']}")
    print(f"  componentes: {r['component_total']}  por clase: {r['counts']}")
    print(f"  GUID 16-byte OK={r['guid_16byte_ok']} fallos={r['guid_16byte_bad']}")
    print(f"  grafo consistente: {r['graph_consistent']}  ({r['graph_orphans']})")
    p = r["pspx"]
    print(f"  pspx abre ZIP: {p['opens_as_zip']}  catalogos referenciados: {p['catalog_count']}  "
          f"XML parseadas: {len(p['parts'])}  errores: {p['parse_errors'] or 'ninguno'}")
    print(f"  pspx Data target ok: {p['data_target_ok']} ({p['data_target']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="specgen", description="Generador de specs/catalogos de AutoCAD Plant 3D.")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Generar una spec desde un piping class Excel.")
    b.add_argument("--piping-class", required=True, help="Ruta al .xlsx del piping class.")
    b.add_argument("--catalogs", required=True, help="Directorio con los .pcat.")
    b.add_argument("--out", required=True, help="Directorio de salida.")
    b.add_argument("--spec-name", default=None,
                   help="Nombre de la spec (por defecto, el del piping class).")
    b.add_argument("--extend-h2", action="store_true",
                   help="Ampliar los catalogos con las variantes -H2 deducidas del Excel.")
    b.add_argument("--template-pspc", default=None,
                   help="Plantilla .pspc (su .pspx hermano aporta la branch table).")
    b.set_defaults(func=_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
