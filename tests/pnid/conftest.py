"""Configuracion de tests de la tool pnid.

El paquete se importa como ``autocad_mcp.pnid`` (instalado con ``pip install -e``), asi que no
hace falta manipular ``sys.path``.

La carpeta de PDFs de muestra reales para el smoke test se lee de la env var
``PNID_SAMPLE_DIR`` (recomendacion del review: nada hardcodeado). Si no esta definida o no
existe, el smoke test se salta.
"""

from __future__ import annotations

import os

SAMPLE_PDF_DIR = os.environ.get("PNID_SAMPLE_DIR")
HAVE_SAMPLE_PDFS = bool(SAMPLE_PDF_DIR) and os.path.isdir(SAMPLE_PDF_DIR)
