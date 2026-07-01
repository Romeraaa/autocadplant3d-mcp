"""Shared, dependency-free primitives for the spec-generation toolkit.

Standalone proof of concept (NOT part of the MCP server). Standard library only
(``sqlite3, uuid, re, datetime, unicodedata``). Everything here is purely functional /
read-only-friendly so it can be reused by the parser, matcher, builder, extender and CLI
without circular imports.

The GUID / .NET-ticks helpers and the text-normalisation helpers were extracted verbatim from
the original PoC modules (``generate_spec_poc`` and ``piping_class_reader``); the empirically
verified encoding (GUID BLOBs are 16-byte ``bytes_le``; ``PnPTimestamp`` is .NET ticks) is
preserved exactly so the generated specs keep opening in the Spec Editor.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone


# --------------------------------------------------------------------------- SQLite
def ro_connect(path: str) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only (URI ``mode=ro``)."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of a table."""
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]


def table_names(con: sqlite3.Connection) -> set[str]:
    """Return the set of user table names in a database."""
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


# --------------------------------------------------------------------------- GUID / ticks
def new_guid_blob() -> bytes:
    """Return a fresh 16-byte GUID blob in the byte order observed in the template (bytes_le)."""
    return uuid.uuid4().bytes_le


def blob_to_guid_text(blob: bytes) -> str:
    """Convert a 16-byte GUID blob to the textual GUID used in the .pspx XML (lower-case dashed)."""
    return str(uuid.UUID(bytes_le=blob))


def new_repository_id() -> str:
    """A fresh RepositoryID: textual GUID wrapped in braces, e.g. ``{....}``."""
    return "{" + str(uuid.uuid4()) + "}"


def now_ticks() -> int:
    """Current time as .NET ticks (100-ns intervals since 0001-01-01)."""
    dt = datetime.now(timezone.utc).replace(tzinfo=None)
    return int((dt - datetime(1, 1, 1)).total_seconds() * 1e7)


# --------------------------------------------------------------------------- text
def strip_accents(text: str) -> str:
    """Remove combining marks (accents) from a string."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def norm(text: str | None) -> str:
    """Lower-case, accent-free, whitespace-collapsed comparison key."""
    if text is None:
        return ""
    t = strip_accents(str(text)).lower()
    return re.sub(r"\s+", " ", t).strip()


def xml_escape(text: str | None) -> str:
    """Escape the five XML special characters for safe attribute/text emission."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- L/H codes
# An L/H code token, e.g. ``L-1276``, ``L-1746-H2``, ``H-291``. The optional trailing group
# captures a *variant suffix* (``-H2``, ``-H``, ...). Whole-word anchored on both sides.
LCODE_RE = re.compile(r"\b([LH]-\d+(?:-[A-Za-z0-9]+)?)\b")

# The variant suffix the piping class uses for hydrogen service. Configurable: the rule is
# "an L-code that ends in this suffix is a variant of the same L-code without it". Documented in
# the README. Currently only ``-H2`` (and the looser ``-H``) is used by REPSOL.
VARIANT_SUFFIX_RE = re.compile(r"-H2?$", re.IGNORECASE)


def extract_lcode(text: str | None) -> str | None:
    """Return the first L/H code token found in ``text`` (full, including any ``-H2`` suffix)."""
    if not text:
        return None
    m = LCODE_RE.search(str(text))
    return m.group(1) if m else None


def is_variant_code(lcode: str | None) -> bool:
    """True if ``lcode`` carries the variant suffix (e.g. ``L-1276-H2``)."""
    return bool(lcode and VARIANT_SUFFIX_RE.search(lcode))


def base_lcode(lcode: str | None) -> str | None:
    """Strip the variant suffix from an L-code (``L-1276-H2`` -> ``L-1276``)."""
    if not lcode:
        return None
    return VARIANT_SUFFIX_RE.sub("", lcode)


def lcode_in_desc(desc: str | None, lcode: str) -> bool:
    """True if ``lcode`` appears in ``desc`` as a whole base token.

    Rejects a longer number (``L-4530`` when looking for ``L-453``) and rejects a variant
    suffix (``L-453-H2`` is NOT a base match for ``L-453``).
    """
    if not desc:
        return False
    if re.search(re.escape(lcode) + r"\d", desc) is not None:
        return False
    return re.search(r"\b" + re.escape(lcode) + r"\b(?!-)", desc) is not None


# --------------------------------------------------------------------------- numeric parsing
def parse_diameter(value) -> float | None:
    """Parse a nominal diameter cell. Accepts numbers and strings like '1 1/2', '3/4', '2"'.

    Guards against a zero denominator (returns ``None`` rather than raising).
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace('"', "")
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", s)   # mixed fraction '1 1/2'
    if m:
        den = int(m.group(3))
        if den == 0:
            return None
        return int(m.group(1)) + int(m.group(2)) / den
    m = re.match(r"^(\d+)/(\d+)$", s)            # simple fraction '3/4'
    if m:
        den = int(m.group(2))
        if den == 0:
            return None
        return int(m.group(1)) / den
    try:
        return float(s)
    except ValueError:
        return None


def norm_schedule(value) -> str | None:
    """Normalise a SCH cell to the catalog vocab ('80', '160', 'STD', 'XS'...)."""
    if value is None or value == "":
        return None
    s = str(value).strip().upper()
    s = s.replace("SCH.", "").replace("SCH", "").strip()
    if s in ("", "-"):
        return None
    if re.match(r"^\d+\.0$", s):   # a numeric schedule read back as float -> drop '.0'
        s = s[:-2]
    return s


def norm_rating(value) -> str | None:
    """Normalise a RATING cell to digits only ('600 #' -> '600', '6000#' -> '6000')."""
    if value is None or value == "":
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits or None
