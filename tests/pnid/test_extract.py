"""Tests de ``extract.py`` / ``api.py`` con un PDF sintetico generado en el propio test.

No dependen de los PDFs de red (fragiles). Un smoke test opcional sobre la carpeta indicada
en la env var ``PNID_SAMPLE_DIR`` se salta con ``pytest.skip`` si no existe.
"""

from __future__ import annotations

import os

import pytest

fitz = pytest.importorskip("fitz", reason="PyMuPDF no instalado")

from autocad_mcp.pnid import api
from autocad_mcp.pnid.extract import extract_pdf
from .conftest import HAVE_SAMPLE_PDFS, SAMPLE_PDF_DIR


TOKENS_LINEA = [
    'C29-2"-P-1026',
    'C29-2"-P-1026',   # duplicado a proposito -> debe deduplicar con count=2
    '8"-H2-REFINERIA',
    'C10-3"H-00013-C2',
    '6"-HIDROGENO',
]
TOKENS_RUIDO = ["KG/CM2", "460-F-2", "FCV-202"]


@pytest.fixture()
def synthetic_pdf(tmp_path):
    """Genera un PDF de una pagina con tokens de linea conocidos + ruido."""
    path = tmp_path / "SINTETICO.pdf"
    doc = fitz.open()
    page = doc.new_page()
    y = 100.0
    for tok in TOKENS_LINEA + TOKENS_RUIDO:
        page.insert_text((72.0, y), tok, fontsize=10)
        y += 20.0
    doc.save(str(path))
    doc.close()
    return str(path)


def test_extract_recupera_lineas_y_deduplica(synthetic_pdf):
    res = extract_pdf(synthetic_pdf)

    ids = {r.line_id for r in res.lines}
    assert 'C29-2"-P-1026' in ids
    assert '8"-H2-REFINERIA' in ids
    assert 'C10-3"H-00013-C2' in ids
    assert '6"-HIDROGENO' in ids

    # 4 lineas unicas (una aparecia dos veces)
    assert len(res.lines) == 4

    dup = next(r for r in res.lines if r.line_id == 'C29-2"-P-1026')
    assert dup.count == 2

    # el ruido no entra como linea
    assert "KG/CM2" not in ids
    assert "460-F-2" not in ids

    # posicion capturada
    assert dup.x is not None and dup.y is not None
    assert dup.page == 1


def test_api_dict_serializable(synthetic_pdf):
    out = api.extract_line_list(pdfs=[synthetic_pdf])
    assert out["ok"] is True
    assert out["error"] is None
    assert len(out["lines"]) == 4
    # sheet == nombre de fichero sin extension
    assert all(ln["sheet"] == "SINTETICO" for ln in out["lines"])
    # cobertura presente
    assert "coverage" in out and out["coverage"]["recognized"] >= 4


def test_api_escribe_csv_y_xlsx(synthetic_pdf, tmp_path):
    out_dir = tmp_path / "salida"
    out = api.extract_line_list(pdfs=[synthetic_pdf], out_dir=str(out_dir), fmt="both")
    assert out["ok"] is True
    nombres = {os.path.basename(f) for f in out["files"]}
    assert "LINE_LIST.csv" in nombres
    assert "LINE_LIST.xlsx" in nombres
    assert "COBERTURA.txt" in nombres
    for f in out["files"]:
        assert os.path.isfile(f)


def test_api_fallo_escritura_devuelve_ok_false(synthetic_pdf, tmp_path, monkeypatch):
    """Un fallo de disco al escribir la salida NO debe propagar excepcion cruda.

    Blindaje del invariante "siempre devuelve dict": la lectura del PDF va bien, pero
    ``report.write_xlsx`` lanza (permisos, disco lleno, ruta imposible) -> ok=False en español.
    """
    from autocad_mcp.pnid import report

    def _boom(*_args, **_kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(report, "write_xlsx", _boom)

    out_dir = tmp_path / "salida"
    out = api.extract_line_list(pdfs=[synthetic_pdf], out_dir=str(out_dir), fmt="xlsx")
    assert out["ok"] is False
    assert "No se pudieron escribir los ficheros de salida" in out["error"]


def test_api_sin_pdf_error():
    out = api.extract_line_list()
    assert out["ok"] is False
    assert "PDF" in out["error"]


def test_api_pdf_corrupto_devuelve_ok_false(tmp_path):
    """Un PDF corrupto (bytes basura) NO debe propagar la excepcion cruda de fitz.

    Blindaje del invariante "siempre devuelve dict": el error se traduce a un mensaje en
    español que identifica el fichero que fallo.
    """
    bad = tmp_path / "corrupto.pdf"
    bad.write_bytes(b"%PDF-1.4 esto no es un pdf valido \x00\x01\x02 basura")

    out = api.extract_line_list(pdfs=[str(bad)])  # no debe lanzar
    assert out["ok"] is False
    assert "No se pudo leer el PDF" in out["error"]
    assert "corrupto.pdf" in out["error"]


@pytest.mark.skipif(not HAVE_SAMPLE_PDFS, reason="PNID_SAMPLE_DIR no definida o inexistente")
def test_smoke_pdfs_reales():
    out = api.extract_line_list(dir=SAMPLE_PDF_DIR)
    assert out["ok"] is True
    # debe reconocer bastantes lineas en los 3 P&IDs reales
    assert len(out["lines"]) > 100
    assert out["coverage"]["candidates"] > 0
