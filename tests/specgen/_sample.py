"""Sample-data discovery shared by the specgen tests (avoids a bare ``conftest`` import clash).

The real REPSOL piping class + catalogs live in this session's scratchpad (accented OneDrive
path -> absolute). Tests that need them are ``skipif``-gated on :data:`HAVE_SAMPLE`.
"""

from __future__ import annotations

import os

import pytest

SCRATCH = (
    r"C:\Users\aromera\AppData\Local\Temp\claude"
    r"\C--Users-aromera-OneDrive---INGENIERIA-Y-DISE-O-ESTRUCTURAL-AVANZADO--S-L-AutocadMCP"
    r"\fd24ee29-daa9-49c0-884e-da26a0be4203\scratchpad"
)
SAMPLE_XLSX = os.path.join(SCRATCH, "piping_class.xlsx")
SAMPLE_TEMPLATE_PSPC = os.path.join(SCRATCH, "NXD-2.pspc")


def _has_catalogs() -> bool:
    if not os.path.isdir(SCRATCH):
        return False
    return any(f.lower().endswith(".pcat") for f in os.listdir(SCRATCH))


HAVE_SAMPLE = os.path.exists(SAMPLE_XLSX) and _has_catalogs()
needs_sample = pytest.mark.skipif(not HAVE_SAMPLE, reason="datos de muestra ausentes en scratchpad")
