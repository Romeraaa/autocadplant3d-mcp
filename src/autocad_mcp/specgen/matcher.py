"""Match parsed piping-class entries against the catalogs with a confidence model.

Standalone proof of concept (NOT part of the MCP server). Standard library only.

The matcher indexes every catalog family that carries an L/H code inside ``PartFamilyLongDesc`` and,
for each :class:`~specgen.piping_class.PipingClassEntry`, scores EVERY candidate catalog row of the
family that bears the entry's L-code. The winner becomes the chosen part; the confidence level
encodes how unambiguous the win was:

  * ``ALTA``        -- one family resolves the L-code (or the end type resolves it), a single
                       dominant candidate (no rival within ``SCORE_MARGIN``), non-hydrogen, and the
                       end type is not silently ambiguous (H1).
  * ``MEDIA``       -- matched but ambiguous: several families compete, several rows tie because a
                       discriminating signal is missing, OR the end type could not be deduced and the
                       family mixes several EndTypes (H1 -- never trust ALTA blindly there).
  * ``SUSTITUCION`` -- a hydrogen (-H2) variant resolved to its base family (the dedicated -H2
                       family is absent from the catalog). Flagged, never silent.
  * ``BAJA``        -- no L-code, L-code absent from every catalog, or no catalog row at all.

Bug fixes vs the original PoC:
  * STUD-BOLT (H-291..H-298): the codes ARE in REPSOL_BRIDAS_JUNTAS_PERNOS.pcat, but (a) the old
    parser read the code from the description (bolts don't repeat it there -- now read from the
    dedicated ``L CODE`` column, see :mod:`specgen.piping_class`) and (b) the catalog bolt rows carry
    the *flange* size as ``NominalDiameter``, not the bolt diameter the Excel lists, so a strict
    diameter filter discarded every candidate. The matcher now RETRIES without the diameter filter
    when the filtered query is empty, degrading the confidence to MEDIA. This is generic: any family
    whose Excel diameter semantics differ from the catalog still resolves.
  * H1: when the end type was NOT deduced and the resolved family carries rows with more than one
    distinct ``EndType``, the match is degraded out of ALTA (to MEDIA) -- ALTA must stay reliable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from . import common
from .catalog_index import CatalogIndex
from .piping_class import PipingClassEntry

# Weights for the multi-signal candidate scorer (calibrated at family level against NXD-2): the
# catalogs carry no Material/MaterialCode, so the usable signals are the L-code, end type, nominal /
# branch diameter, schedule and pressure class.
W_LCODE_EXACT = 5.0       # the entry's full L-code (incl. -H2) is literally present in the family
W_LCODE_BASE = 3.0        # only the base L-code matched (the -H2 variant has no catalog family)
W_END_MATCH = 3.0         # EndType equals the deduced end type
W_END_MISMATCH = -2.0     # end type known but the candidate disagrees
W_DIAMETER = 3.0          # nominal diameter matched (rows are already filtered by it)
W_BRANCH_PORT = 2.0       # candidate has a port at the branch diameter (olets / swages / reducers)
W_SCH_MATCH = 2.0         # Schedule equals the entry schedule
W_SCH_MISMATCH = -1.0     # schedule known but the candidate disagrees
W_RATING_MATCH = 2.0      # PressureClass equals the entry rating
W_RATING_MISMATCH = -1.0  # rating known but the candidate disagrees

# A second candidate within this margin of the winner is "too close to call" -> MEDIA.
SCORE_MARGIN = 3.0


@dataclass
class _Candidate:
    """One scored catalog row competing to materialise a piping-class entry."""

    score: float
    family_desc: str
    catalog: str
    catalog_class: str
    size_record_id: bytes
    part_family_id: bytes
    pnpid: int
    schedule: str | None
    pressure_class: str | None
    end_type: str | None
    used_base_code: bool
    diameter_filtered: bool   # the row passed the nominal-diameter filter (vs a relaxed retry)


class CatalogMatcher:
    """Index catalog families by L-code and resolve entries with a confidence model.

    ``catalogs`` is a :class:`~specgen.catalog_index.CatalogIndex` (already opened); the matcher
    borrows its read-only handles and never closes them.
    """

    def __init__(self, catalogs: CatalogIndex) -> None:
        self.catalogs = catalogs
        self.handles = catalogs.handles
        # lcode -> list of (catalog, family_desc, end_type, class_name)
        self.lcode_families: dict[str, list[tuple[str, str, str | None, str]]] = defaultdict(list)
        # (catalog, family_desc) -> set of distinct EndTypes in that family (for H1)
        self._family_end_types: dict[tuple[str, str], set[str]] = defaultdict(set)
        for logical, con in self.handles.items():
            if "EngineeringItems" not in catalogs.tables.get(logical, set()):
                continue
            for desc, end, cls in con.execute(
                "SELECT DISTINCT e.PartFamilyLongDesc, e.EndType, b.PnPClassName "
                "FROM EngineeringItems e JOIN PnPBase b ON e.PnPID=b.PnPID "
                "WHERE e.PartFamilyLongDesc LIKE '%L-%' OR e.PartFamilyLongDesc LIKE '%H-%'"
            ).fetchall():
                if not desc:
                    continue
                for code in common.LCODE_RE.findall(desc):
                    self.lcode_families[code].append((logical, desc, end, cls))
                    if end:
                        self._family_end_types[(logical, desc)].add(end)

    def match(self, e: PipingClassEntry) -> None:
        """Resolve one entry in place: score candidates, pick the winner, assign confidence."""
        if not e.lcode:
            e.match_note = "sin L-code"
            e.confidence = "BAJA"
            return

        # Prefer the exact code (e.g. L-478-H2); fall back to the base code when the -H2 family is
        # absent. ``used_base`` drives the SUSTITUCION flag.
        fams = self.lcode_families.get(e.lcode)
        used_base = False
        if not fams and e.lcode_base and e.lcode_base != e.lcode:
            fams = self.lcode_families.get(e.lcode_base)
            used_base = bool(fams)
        if not fams:
            e.match_note = f"L-code {e.lcode} no esta en ningun catalogo"
            e.confidence = "BAJA"
            return

        n_families = len({(c[0], c[1]) for c in fams})
        candidates, relaxed = self._score_candidates(fams, used_base, e)
        if not candidates:
            e.match_note = f"L-code {e.lcode} sin filas en el catalogo"
            e.confidence = "BAJA"
            return

        candidates.sort(key=lambda c: c.score, reverse=True)
        win = candidates[0]
        e.catalog = win.catalog
        e.family_desc = win.family_desc
        e.catalog_class = win.catalog_class
        e.size_record_id = win.size_record_id
        e.part_family_id = win.part_family_id
        e.pnpid = win.pnpid
        e.score = win.score
        e.alternatives = [
            (c.family_desc, c.score, c.schedule, c.pressure_class) for c in candidates[1:4]
        ]

        # ----- confidence -----
        runner_up = candidates[1].score if len(candidates) > 1 else None
        ambiguous = runner_up is not None and (win.score - runner_up) < SCORE_MARGIN
        notes: list[str] = []

        is_substitution = e.is_hydrogen and (win.used_base_code or used_base)
        end_mismatch = bool(e.end_type and win.end_type and win.end_type != e.end_type)

        # H1: end type not deduced AND the chosen family mixes several EndTypes -> never ALTA.
        fam_ends = self._family_end_types.get((win.catalog, win.family_desc), set())
        h1_end_ambiguous = (not e.end_type) and len(fam_ends) > 1

        if is_substitution:
            e.confidence = "SUSTITUCION"
            notes.append("base sustituida (-H2 ausente en catalogo)")
        elif relaxed:
            # The diameter filter was empty and we retried without it (bolts and the like).
            e.confidence = "MEDIA"
            notes.append(f"diametro {e.main_diameter} no coincide con NominalDiameter del "
                         f"catalogo; familia resuelta por L-code (revisar talla)")
        elif n_families > 1 and (not e.end_type or end_mismatch):
            e.confidence = "MEDIA"
            notes.append(f"{n_families} familias comparten {e.lcode_base or e.lcode}; "
                         f"end={e.end_type or '?'} no desambigua")
        elif h1_end_ambiguous:
            e.confidence = "MEDIA"
            notes.append(f"end-type no deducido y la familia mezcla EndTypes {sorted(fam_ends)} "
                         f"(H1: no se asegura ALTA)")
        elif ambiguous:
            e.confidence = "MEDIA"
            notes.append(f"candidato no inequivoco (2o score {runner_up:.0f} vs {win.score:.0f})")
        else:
            e.confidence = "ALTA"

        if end_mismatch and e.confidence != "MEDIA":
            notes.append(f"end-type {e.end_type} != {win.end_type}")
        e.match_note = "; ".join(notes)

    def _score_candidates(
        self,
        fams: list[tuple[str, str, str | None, str]],
        used_base: bool,
        e: PipingClassEntry,
    ) -> tuple[list[_Candidate], bool]:
        """Score catalog rows of the L-code's families at the entry diameter.

        Returns ``(candidates, relaxed)``. If the nominal-diameter filter yields nothing, retries
        WITHOUT it (``relaxed=True``) so families whose Excel diameter semantics differ from the
        catalog (bolts: Excel lists bolt dia, catalog lists flange size) still resolve.
        """
        candidates = self._score_with_filter(fams, used_base, e, use_diameter=True)
        if candidates or e.main_diameter is None:
            return candidates, False
        relaxed = self._score_with_filter(fams, used_base, e, use_diameter=False)
        return relaxed, bool(relaxed)

    def _score_with_filter(
        self,
        fams: list[tuple[str, str, str | None, str]],
        used_base: bool,
        e: PipingClassEntry,
        *,
        use_diameter: bool,
    ) -> list[_Candidate]:
        out: list[_Candidate] = []
        for catalog, family_desc, _fam_end, cls in {(c[0], c[1], c[2], c[3]) for c in fams}:
            con = self.handles[catalog]
            params: list = [family_desc]
            sql = (
                "SELECT SizeRecordId, PartFamilyId, PnPID, NominalDiameter, Schedule, "
                "PressureClass, EndType FROM EngineeringItems WHERE PartFamilyLongDesc = ?"
            )
            if use_diameter and e.main_diameter is not None:
                sql += " AND ABS(NominalDiameter - ?) < 0.01"
                params.append(e.main_diameter)
            for srid, fam_id, pnpid, _nd, sch, pc, end in con.execute(sql, params).fetchall():
                if srid is None:
                    continue
                score = W_LCODE_BASE if used_base else W_LCODE_EXACT
                if e.end_type and end:
                    score += W_END_MATCH if end == e.end_type else W_END_MISMATCH
                if use_diameter and e.main_diameter is not None:
                    score += W_DIAMETER
                if e.branch_diameter is not None and self._has_branch_port(
                    con, pnpid, e.branch_diameter
                ):
                    score += W_BRANCH_PORT
                if e.schedule:
                    score += W_SCH_MATCH if (sch or "") == e.schedule else W_SCH_MISMATCH
                if e.rating:
                    score += W_RATING_MATCH if (pc or "") == e.rating else W_RATING_MISMATCH
                out.append(_Candidate(
                    score=score, family_desc=family_desc, catalog=catalog, catalog_class=cls,
                    size_record_id=srid, part_family_id=fam_id, pnpid=pnpid,
                    schedule=sch, pressure_class=pc, end_type=end, used_base_code=used_base,
                    diameter_filtered=use_diameter,
                ))
        return out

    @staticmethod
    def _has_branch_port(con, pnpid: int, branch_d: float) -> bool:
        """True if the part has a port at the branch diameter."""
        port_dias = [
            d for (d,) in con.execute(
                "SELECT p.NominalDiameter FROM PartPort pp JOIN Port p ON pp.Port = p.PnPID "
                "WHERE pp.Part = ? AND p.NominalDiameter IS NOT NULL",
                (pnpid,),
            ).fetchall()
        ]
        return any(abs(d - branch_d) < 0.01 for d in port_dias)
