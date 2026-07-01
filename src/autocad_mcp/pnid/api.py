"""Capa publica UI-agnostica de la extraccion de line-list desde P&IDs en PDF.

Punto de entrada unico que consumen TANTO la CLI (``cli.py``) COMO la tool MCP ``pnid``.
Devuelve SIEMPRE un dict JSON-serializable (sin bytes, sin objetos ``fitz``), de forma que
la tool MCP sea un wrapper fino (como ``specgen/api.py``).

Funcion principal
-----------------
``extract_line_list(pdfs=None, dir=None, out_dir=None, fmt=None, bonus=False) -> dict``

* ``out_dir`` OPCIONAL: si es ``None`` no se escribe nada a disco; el dict lleva los registros.
  Si se indica, ademas se vuelca CSV/XLSX (segun ``fmt``) y la ruta va en ``files``.
* ``fmt``: ``"csv"`` | ``"xlsx"`` | ``"both"`` (por defecto ``"xlsx"`` cuando hay ``out_dir``).

Forma del dict devuelto
-----------------------
``{
    "ok": bool,
    "error": str | None,
    "sheets": [ {sheet, lines, candidates, recognized, coverage_pct}, ... ],
    "lines": [ {sheet, line_id, family, diameter, service, area, number, clase, name,
                page, x, y, count}, ... ],
    "coverage": {"recognized": int, "candidates": int, "pct": float},
    "unrecognized": [ {sheet, token}, ... ],
    "instruments": [...],   # solo si bonus=True
    "equipment": [...],     # solo si bonus=True
    "files": [str, ...],    # solo si out_dir
 }``
"""

from __future__ import annotations

import glob
import os
from dataclasses import asdict

from .extract import SheetResult, extract_pdf
from . import report


def _collect_pdfs(pdfs=None, dir=None) -> list[str]:
    """Reune rutas de PDF desde una lista y/o una carpeta, deduplicando y preservando orden."""
    out: list[str] = []
    if pdfs:
        out.extend(pdfs)
    if dir:
        out.extend(sorted(glob.glob(os.path.join(dir, "*.pdf"))))
    return list(dict.fromkeys(out))


def _results_to_dict(results: list[SheetResult], *, bonus: bool) -> dict:
    """Convierte una lista de SheetResult en el dict JSON-serializable publico."""
    sheets = []
    lines = []
    unrecognized = []
    tot_cand = tot_reco = 0
    for res in results:
        tot_cand += res.candidates
        tot_reco += res.recognized
        sheets.append(
            {
                "sheet": res.sheet,
                "lines": len(res.lines),
                "candidates": res.candidates,
                "recognized": res.recognized,
                "coverage_pct": round(res.coverage * 100, 1),
            }
        )
        for rec in res.lines:
            lines.append(asdict(rec))
        for tok in res.unrecognized:
            unrecognized.append({"sheet": res.sheet, "token": tok})

    lines.sort(key=lambda r: (r["sheet"], r["line_id"]))
    pct = 100.0 if tot_cand == 0 else round(tot_reco / tot_cand * 100, 1)

    out = {
        "ok": True,
        "error": None,
        "sheets": sheets,
        "lines": lines,
        "coverage": {"recognized": tot_reco, "candidates": tot_cand, "pct": pct},
        "unrecognized": unrecognized,
    }
    if bonus:
        out["instruments"] = [it for res in results for it in res.instruments]
        out["equipment"] = [eq for res in results for eq in res.equipment]
    return out


def extract_line_list(
    pdfs=None,
    dir=None,
    out_dir: str | None = None,
    fmt: str | None = None,
    bonus: bool = False,
) -> dict:
    """Extrae la line-list de uno o varios P&IDs en PDF (texto vectorial, sin OCR).

    Args:
        pdfs: lista de rutas a PDFs (o None).
        dir: carpeta con ``*.pdf`` (o None). Se combina con ``pdfs``.
        out_dir: si se indica, escribe CSV/XLSX ahi; si None, no escribe nada.
        fmt: "csv" | "xlsx" | "both". Por defecto "xlsx" cuando hay ``out_dir``.
        bonus: si True, incluye instrumentos y equipos en el dict / ficheros.

    Returns:
        dict JSON-serializable (ver docstring del modulo). SIEMPRE devuelve un dict:
        ante error de entrada, PDF ilegible (corrupto/cifrado/truncado) o falta de
        PyMuPDF, ``{"ok": False, "error": "..."}`` con mensaje en español, sin lanzar.
    """
    paths = _collect_pdfs(pdfs=pdfs, dir=dir)
    if not paths:
        return {"ok": False, "error": "No se indico ningun PDF (parametros pdfs o dir)."}

    faltan = [p for p in paths if not os.path.isfile(p)]
    if faltan:
        return {"ok": False, "error": "No existen los ficheros: " + ", ".join(faltan)}

    # Lectura por fichero para identificar cual falla. Cualquier excepcion de PyMuPDF
    # (p.ej. pymupdf.FileDataError en un PDF corrupto/cifrado/truncado) o la falta de la
    # dependencia (ImportError) se traduce a un dict {"ok": False, ...} en español.
    results: list[SheetResult] = []
    for ruta in paths:
        try:
            results.append(extract_pdf(ruta, bonus=bonus))
        except Exception as e:  # noqa: BLE001 - blindaje del invariante "siempre devuelve dict"
            return {
                "ok": False,
                "error": f"No se pudo leer el PDF '{ruta}': {e}",
            }

    result = _results_to_dict(results, bonus=bonus)

    if out_dir:
        chosen = fmt or "xlsx"
        # El bloque de escritura se blinda aparte: un fallo de disco (permisos, disco
        # lleno, ruta invalida) no debe romper el invariante "siempre devuelve un dict".
        try:
            files: list[str] = []
            if chosen in ("csv", "both"):
                files += report.write_csv(results, out_dir)
            if chosen in ("xlsx", "both"):
                files += report.write_xlsx(results, out_dir)
                if chosen == "xlsx":
                    files.append(report.write_coverage_txt(results, out_dir))
        except Exception as e:  # noqa: BLE001 - blindaje del invariante "siempre devuelve dict"
            return {
                "ok": False,
                "error": f"No se pudieron escribir los ficheros de salida en '{out_dir}': {e}",
            }
        result["files"] = files

    return result


def coverage_text(results_dict: dict) -> str:
    """Render de texto del informe de cobertura a partir del dict de :func:`extract_line_list`.

    UI-agnostico: no depende de SheetResult, solo del dict publico. Util para la CLI y para
    la tool MCP que quiera devolver el resumen como texto.
    """
    out = ["INFORME DE COBERTURA - line-list desde P&ID PDF", "=" * 50, ""]
    unrec_by_sheet: dict[str, list[str]] = {}
    for u in results_dict.get("unrecognized", []):
        unrec_by_sheet.setdefault(u["sheet"], []).append(u["token"])
    for sh in results_dict.get("sheets", []):
        out.append(
            f"[{sh['sheet']}] lineas unicas: {sh['lines']:3d} | "
            f"candidatos: {sh['candidates']:4d} | reconocidos: {sh['recognized']:4d} | "
            f"cobertura: {sh['coverage_pct']:5.1f}%"
        )
        toks = unrec_by_sheet.get(sh["sheet"], [])
        if toks:
            out.append(f"    no reconocidos ({len(toks)}):")
            out.extend(f"      - {t}" for t in toks)
        out.append("")
    cov = results_dict.get("coverage", {})
    tot_lineas = len(results_dict.get("lines", []))
    out.append("-" * 50)
    out.append(
        f"TOTAL  lineas unicas: {tot_lineas} | candidatos: {cov.get('candidates', 0)} | "
        f"reconocidos: {cov.get('recognized', 0)} | cobertura global: {cov.get('pct', 0.0)}%"
    )
    return "\n".join(out)
