"""Punto de entrada: ``python -m autocad_mcp.pnid ...``.

Tambien se admite ``python -m autocad_mcp.pnid.extract ...`` (ver extract.py, que reexporta
el ``main`` de la CLI para ese caso de uso).
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
