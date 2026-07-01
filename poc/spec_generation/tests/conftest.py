"""Shared fixtures and sample-data discovery for the specgen test suite.

Unit tests need no files; the integration tests are ``skipif``-gated on the sample piping class
and catalogs living in the scratchpad. Make the package importable regardless of CWD.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

# Sample data lives in this session's scratchpad (accented OneDrive path -> use absolute).
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


@pytest.fixture(scope="session")
def scratch_dir() -> str:
    return SCRATCH


@pytest.fixture(scope="session")
def sample_xlsx() -> str:
    return SAMPLE_XLSX


@pytest.fixture(scope="session")
def sample_template_pspc() -> str | None:
    return SAMPLE_TEMPLATE_PSPC if os.path.exists(SAMPLE_TEMPLATE_PSPC) else None
