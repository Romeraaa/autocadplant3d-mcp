"""Extraccion de line-list desde P&IDs en PDF (texto vectorial, sin OCR).

Abre cada PDF con PyMuPDF (``fitz``), extrae las "words" de la capa de texto vectorial,
aplica los patrones de :mod:`patterns`, deduplica por ``(sheet, line_id)`` contando
ocurrencias y calcula la cobertura frente a los candidatos alfanumericos largos.

El import de ``fitz`` es perezoso para que este modulo se pueda importar en entornos sin
PyMuPDF (los tests de patrones no lo necesitan).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import patterns as P


def _import_fitz():
    """Importa ``fitz`` (PyMuPDF) de forma perezosa con un error claro en español.

    ``pymupdf`` es dependencia base del servidor; si aun asi no estuviera disponible,
    se levanta ``ImportError`` con un mensaje accionable que la capa ``api`` traduce a
    ``{"ok": False, "error": ...}``.
    """
    try:
        import fitz  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - solo si falta la dependencia base
        raise ImportError(
            "PyMuPDF (fitz) no esta disponible. Instala el servidor con "
            "'pip install -e .' (pymupdf es dependencia base)."
        ) from exc
    return fitz


@dataclass
class LineRecord:
    """Un registro de linea deduplicado dentro de una hoja (sheet)."""

    sheet: str
    line_id: str
    family: str
    diameter: str | None
    service: str | None
    area: str | None
    number: str | None
    clase: str | None
    name: str | None
    page: int
    x: float
    y: float
    count: int = 1


@dataclass
class SheetResult:
    """Resultado de procesar una hoja (un PDF de una pagina, o por-pagina)."""

    sheet: str
    lines: list[LineRecord] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)   # candidatos que no casaron
    candidates: int = 0                                       # nº de candidatos de cobertura
    recognized: int = 0                                       # nº de candidatos reconocidos como linea
    instruments: list[dict] = field(default_factory=list)
    equipment: list[dict] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Cobertura reconocidos/candidatos en [0,1]. 1.0 si no hay candidatos."""
        return 1.0 if self.candidates == 0 else self.recognized / self.candidates


def _iter_words(pdf_path: str):
    """Genera tuplas ``(page_no, x0, y0, text)`` de la capa de texto del PDF.

    ``page.get_text("words")`` devuelve ``(x0,y0,x1,y1,text,block,line,word_no)``.
    """
    fitz = _import_fitz()

    doc = fitz.open(pdf_path)
    try:
        for page_no, page in enumerate(doc, start=1):
            for w in page.get_text("words"):
                x0, y0, _x1, _y1, text = w[0], w[1], w[2], w[3], w[4]
                yield page_no, float(x0), float(y0), text
    finally:
        doc.close()


def extract_pdf(pdf_path: str, *, bonus: bool = False) -> SheetResult:
    """Extrae el line-list de un PDF y devuelve un :class:`SheetResult`.

    Args:
        pdf_path: ruta al PDF.
        bonus: si True, tambien puebla ``instruments`` y ``equipment`` (best-effort).
    """
    sheet = os.path.splitext(os.path.basename(pdf_path))[0]
    result = SheetResult(sheet=sheet)

    # dedup por (line_id) dentro de la hoja; conserva la primera posicion vista.
    seen: dict[str, LineRecord] = {}

    for page_no, x, y, text in _iter_words(pdf_path):
        token = text.strip()
        if not token:
            continue

        candidate = P.is_coverage_candidate(token)
        if candidate:
            result.candidates += 1

        match = P.parse_line(token)
        if match:
            if candidate:
                result.recognized += 1
            rec = seen.get(match.line_id)
            if rec is None:
                rec = LineRecord(
                    sheet=sheet,
                    line_id=match.line_id,
                    family=match.family,
                    diameter=match.diameter,
                    service=match.service,
                    area=match.area,
                    number=match.number,
                    clase=match.clase,
                    name=match.name,
                    page=page_no,
                    x=round(x, 2),
                    y=round(y, 2),
                    count=1,
                )
                seen[match.line_id] = rec
            else:
                rec.count += 1
            continue

        # No es linea: si es candidato de cobertura, va al bucket de revision.
        if candidate:
            result.unrecognized.append(token)

        if bonus:
            instr = P.parse_instrument(token)
            if instr:
                result.instruments.append({"sheet": sheet, "page": page_no, **instr})
                continue
            equip = P.parse_equipment(token)
            if equip:
                result.equipment.append({"sheet": sheet, "page": page_no, **equip})

    result.lines = list(seen.values())
    # dedup del bucket de no reconocidos preservando orden
    result.unrecognized = list(dict.fromkeys(result.unrecognized))
    return result


def extract_many(pdf_paths, *, bonus: bool = False) -> list[SheetResult]:
    """Extrae el line-list de varios PDFs. Devuelve un SheetResult por PDF."""
    return [extract_pdf(p, bonus=bonus) for p in pdf_paths]


if __name__ == "__main__":
    # Permite ``python -m autocad_mcp.pnid.extract ...`` (delega en la CLI).
    from .cli import main

    raise SystemExit(main())
