"""specgen -- generate AutoCAD Plant 3D specifications/catalogs from a piping-class Excel.

Standalone proof of concept (NOT part of the MCP server). Standard library plus ``openpyxl``.

The package turns a human-authored REPSOL piping-class workbook into a complete Plant 3D
specification (``.pspc`` + ``.pspx``) by matching every row against a directory of Plant 3D
catalogs (``.pcat``, SQLite) and materialising the chosen parts. Optionally it extends the
catalogs with the missing hydrogen (``-H2``) variant families so the H2 rows resolve to a
dedicated family instead of a substituted base.

Nothing is hard-coded: catalogs are discovered from a directory, the spec definition comes from
the piping class, the ``-H2`` variants are deduced from the Excel (not a fixed list), and the
branch table is taken from an optional template ``.pspc``/``.pspx`` (or emitted minimal/empty).

Modules:
  * :mod:`specgen.common`         -- dependency-free primitives (GUID/ticks, text, numeric parsing).
  * :mod:`specgen.piping_class`   -- parse the piping-class Excel into normalised entries.
  * :mod:`specgen.catalog_index`  -- discover + index a directory of ``.pcat`` catalogs.
  * :mod:`specgen.matcher`        -- match entries to catalog parts with a confidence model.
  * :mod:`specgen.spec_builder`   -- materialise the chosen parts into a ``.pspc`` + ``.pspx``.
  * :mod:`specgen.catalog_extender` -- clone base families into ``-H2`` variants on catalog copies.
  * :mod:`specgen.report`         -- the ``REVISION_MATCHING.xlsx`` review report + coverage.
  * :mod:`specgen.cli`            -- the ``python -m specgen build ...`` command-line entry point.
"""

from __future__ import annotations

__all__ = [
    "common",
    "piping_class",
    "catalog_index",
    "matcher",
    "spec_builder",
    "catalog_extender",
    "report",
    "api",
]

__version__ = "0.1.0"
