"""CLI de la extraccion de line-list desde P&IDs en PDF.

Consumidor DELGADO de :func:`autocad_mcp.pnid.api.extract_line_list`: no contiene logica de
parseo. Solo interpreta argumentos, llama a la capa ``api`` y renderiza el dict resultante.

Uso::

    python -m autocad_mcp.pnid --pdf A.pdf [--pdf B.pdf ...] --out CARPETA
    python -m autocad_mcp.pnid --dir CARPETA --out CARPETA [--format csv|xlsx|both]

Tambien funciona ``python -m autocad_mcp.pnid.extract ...``.
"""

from __future__ import annotations

import argparse
import sys

from . import api


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autocad_mcp.pnid",
        description="Extrae la line-list desde P&IDs en PDF (texto vectorial, sin OCR).",
    )
    p.add_argument("--pdf", action="append", help="Ruta a un PDF (repetible).")
    p.add_argument("--dir", help="Carpeta con PDFs (*.pdf).")
    p.add_argument("--out", help="Carpeta de salida (opcional; sin ella solo imprime el resumen).")
    p.add_argument(
        "--format",
        choices=["csv", "xlsx", "both"],
        default="xlsx",
        help="Formato de salida cuando hay --out (por defecto: xlsx).",
    )
    p.add_argument(
        "--bonus",
        action="store_true",
        help="Tambien extrae instrumentos y equipos (best-effort, hojas aparte).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    result = api.extract_line_list(
        pdfs=args.pdf,
        dir=args.dir,
        out_dir=args.out,
        fmt=args.format,
        bonus=args.bonus,
    )

    if not result.get("ok"):
        print(f"ERROR: {result.get('error')}", file=sys.stderr)
        return 2

    print(api.coverage_text(result))
    files = result.get("files")
    if files:
        print()
        print("Ficheros generados:")
        for f in files:
            print(f"  {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
