"""Fixtures for the specgen test suite.

Sample-data discovery + the ``needs_sample`` marker live in :mod:`_sample` (imported by the
tests directly, to avoid a bare ``conftest`` import clash with the top-level ``tests/conftest.py``).
The package is imported as ``autocad_mcp.specgen`` (installed with ``pip install -e``).
"""

from __future__ import annotations

import pytest

from _sample import SAMPLE_TEMPLATE_PSPC, SAMPLE_XLSX, SCRATCH


@pytest.fixture(scope="session")
def scratch_dir() -> str:
    return SCRATCH


@pytest.fixture(scope="session")
def sample_xlsx() -> str:
    return SAMPLE_XLSX


@pytest.fixture(scope="session")
def sample_template_pspc() -> str | None:
    import os
    return SAMPLE_TEMPLATE_PSPC if os.path.exists(SAMPLE_TEMPLATE_PSPC) else None
