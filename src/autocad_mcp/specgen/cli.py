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

from . import api
from .report import format_coverage


def _build(args: argparse.Namespace) -> int:
    piping_class = os.path.abspath(args.piping_class)
    if not os.path.exists(piping_class):
        print(f"ERROR: no existe el piping class: {piping_class}", file=sys.stderr)
        return 2

    result = api.build(
        piping_class=piping_class,
        catalogs_dir=args.catalogs,
        out_dir=args.out,
        spec_name=args.spec_name,
        extend_h2=args.extend_h2,
        template_pspc=args.template_pspc,
    )
    if not result.get("ok") and "error" in result and "verify" not in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    ext = result.get("extend_h2")
    if ext is not None:
        print("== ampliar catalogos con variantes -H2 ==")
        print(f"  catalogos copiados: {len(ext['catalogs'])}  "
              f"L-codes base ampliados: {len(ext['lcodes_covered'])}  "
              f"familias -H2: {ext['families_created']}  filas nuevas: {ext['rows_created']}")
        for w in ext["warnings"]:
            print(f"  AVISO: {w}")

    print(format_coverage(result["coverage"]))
    print(f"\n  informe de revision: {result['files']['review_xlsx']}")
    print(f"\n  componentes materializados: {result['components_built']}")

    _print_verify(result["verify"], result["files"]["pspc"], result["files"]["pspx"])
    print(f"\nspec generada: {'OK' if result['ok'] else 'CON AVISOS'}  ->  "
          f"{result['files']['pspc']}")
    return 0 if result["ok"] else 1


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
