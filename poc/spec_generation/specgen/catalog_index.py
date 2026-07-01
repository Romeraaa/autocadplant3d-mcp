"""Discover and index a directory of Plant 3D catalogs (.pcat, SQLite).

Standalone proof of concept (NOT part of the MCP server). Standard library only.

This module removes ALL hard-coded catalog names/paths from the toolkit. Given a directory it:

  * discovers every ``*.pcat`` file in it (sorted by filename for a deterministic priority),
  * reads each catalog's own ``RepositoryDescriptor.Name`` as the *logical* catalog name (the same
    name the Spec Editor shows and that ``CatalogReferences.xml`` must carry),
  * opens every catalog strictly read-only and builds a ``SizeRecordId -> catalog`` lookup so the
    materialiser can locate any part by its 16-byte SizeRecordId blob.

The logical-name/path map is also what the ``.pspx`` packager uses to emit a
``CatalogReferences.xml`` that points at the real files. Nothing here is REPSOL- or NXD-2-specific.
"""

from __future__ import annotations

import os
import sqlite3

from . import common


def discover_catalogs(catalog_dir: str) -> list[tuple[str, str]]:
    """Return ``[(logical_name, absolute_path), ...]`` for every .pcat in ``catalog_dir``.

    The logical name is the catalog's own ``RepositoryDescriptor.Name`` (falls back to the file
    stem if absent). Files are sorted by name so the SizeRecordId priority is deterministic.
    """
    if not os.path.isdir(catalog_dir):
        raise NotADirectoryError(f"directorio de catalogos no encontrado: {catalog_dir}")
    out: list[tuple[str, str]] = []
    for fname in sorted(os.listdir(catalog_dir)):
        if not fname.lower().endswith(".pcat"):
            continue
        path = os.path.join(catalog_dir, fname)
        logical = _catalog_logical_name(path) or os.path.splitext(fname)[0]
        out.append((logical, path))
    if not out:
        raise FileNotFoundError(f"no se encontro ningun .pcat en {catalog_dir}")
    return out


def _catalog_logical_name(path: str) -> str | None:
    con = common.ro_connect(path)
    try:
        row = con.execute("SELECT Name FROM RepositoryDescriptor LIMIT 1").fetchone()
        return (row[0] if row else None) or None
    except sqlite3.Error:
        return None
    finally:
        con.close()


class CatalogIndex:
    """Read-only handles to every catalog in a directory plus a SizeRecordId -> catalog lookup.

    The first catalog (by sorted filename) that carries a given SizeRecordId wins, so the lookup is
    deterministic and reproducible.
    """

    def __init__(self, catalog_dir: str) -> None:
        self.catalog_dir = catalog_dir
        self.entries = discover_catalogs(catalog_dir)
        self.handles: dict[str, sqlite3.Connection] = {}
        self.paths: dict[str, str] = {}
        self.tables: dict[str, set[str]] = {}
        self._srid_to_cat: dict[bytes, str] = {}
        for logical, path in self.entries:
            con = common.ro_connect(path)
            self.handles[logical] = con
            self.paths[logical] = path
            self.tables[logical] = common.table_names(con)
        for logical, con in self.handles.items():
            if "EngineeringItems" not in self.tables[logical]:
                continue
            for (srid,) in con.execute(
                "SELECT SizeRecordId FROM EngineeringItems WHERE SizeRecordId IS NOT NULL"
            ).fetchall():
                if srid not in self._srid_to_cat:
                    self._srid_to_cat[srid] = logical

    def find(self, size_record_id: bytes | None) -> str | None:
        """Return the logical catalog name holding this SizeRecordId, or None."""
        if size_record_id is None:
            return None
        return self._srid_to_cat.get(size_record_id)

    def references(self) -> list[tuple[str, str]]:
        """Return ``[(logical_name, absolute_path), ...]`` for the .pspx CatalogReferences.xml."""
        return list(self.entries)

    def close(self) -> None:
        for con in self.handles.values():
            con.close()
        self.handles.clear()
