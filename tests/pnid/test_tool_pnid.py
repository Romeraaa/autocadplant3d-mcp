"""Tests de la rama de la tool ``server.pnid`` (rápidos, sin PDFs reales).

Se mockea ``pnid_api.extract_line_list`` para no depender de PyMuPDF ni de PDFs: se verifica
el comportamiento de la tool respecto a los adjuntos y la validación de entrada.
Cubre: attach_files=True -> lista con EmbeddedResource; attach_files=False -> JSON string;
ok=False -> JSON sin adjuntar; sin 'out'+attach -> se usa un directorio temporal.
"""

from __future__ import annotations

import base64
import json

import pytest
from mcp.types import EmbeddedResource, TextContent

from autocad_mcp import server

CSV_MIME = "text/csv"
TXT_MIME = "text/plain"


def _valid_dir(tmp_path):
    d = tmp_path / "pids"
    d.mkdir()
    return str(d)


@pytest.fixture
def fake_extract(monkeypatch, tmp_path):
    """Patch pnid_api.extract_line_list para devolver un dict con ficheros dummy en out_dir."""
    captured = {}

    def _extract(*, pdfs, dir, out_dir, fmt, bonus):
        captured["out_dir"] = out_dir
        captured["pdfs"] = pdfs
        captured["dir"] = dir
        files = []
        if out_dir:
            import os
            os.makedirs(out_dir, exist_ok=True)
            csv_p = os.path.join(out_dir, "LINE_LIST.csv")
            txt_p = os.path.join(out_dir, "COBERTURA.txt")
            with open(csv_p, "w", encoding="utf-8") as fh:
                fh.write("sheet,line_id\n")
            with open(txt_p, "w", encoding="utf-8") as fh:
                fh.write("cobertura")
            files = [csv_p, txt_p]
        return {
            "ok": True,
            "error": None,
            "sheets": [],
            "lines": [],
            "coverage": {"recognized": 0, "candidates": 0, "pct": 100.0},
            "unrecognized": [],
            "files": files,
        }

    from autocad_mcp.pnid import api as pnid_api
    monkeypatch.setattr(pnid_api, "extract_line_list", _extract)
    return captured


async def _call(data, operation="line_list"):
    return await server.pnid(operation=operation, data=data)


async def test_attach_true_returns_embedded(fake_extract, tmp_path):
    out = await _call({"dir": _valid_dir(tmp_path), "out": str(tmp_path / "o")})
    assert isinstance(out, list)
    assert isinstance(out[0], TextContent)
    embedded = [c for c in out if isinstance(c, EmbeddedResource)]
    assert len(embedded) == 2
    assert all(base64.b64decode(c.resource.blob) for c in embedded)
    mimes = {c.resource.mimeType for c in embedded}
    assert CSV_MIME in mimes and TXT_MIME in mimes


async def test_attach_false_returns_json(fake_extract, tmp_path):
    out = await _call({
        "dir": _valid_dir(tmp_path), "out": str(tmp_path / "o"), "attach_files": False,
    })
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert "attachments_skipped" not in parsed


async def test_ok_false_returns_json_without_attach(monkeypatch, tmp_path):
    def _extract(**kwargs):
        return {"ok": False, "error": "No se pudo leer el PDF 'x.pdf': roto"}

    from autocad_mcp.pnid import api as pnid_api
    monkeypatch.setattr(pnid_api, "extract_line_list", _extract)

    out = await _call({"dir": _valid_dir(tmp_path)})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "No se pudo leer" in parsed["error"]


async def test_without_out_uses_temp_dir(fake_extract, tmp_path):
    out = await _call({"dir": _valid_dir(tmp_path)})
    assert isinstance(out, list)
    text = json.loads(out[0].text)
    assert "note" in text and "no persistidos en disco" in text["note"]
    assert "pnid_" in fake_extract["out_dir"]


async def test_temp_dir_removed_after_attach(fake_extract, tmp_path):
    """Sin 'out' + attach_files=True (ok=True): el temp dir NO debe sobrevivir a la llamada."""
    import os

    out = await _call({"dir": _valid_dir(tmp_path)})
    assert isinstance(out, list)  # se adjuntó correctamente
    temp_dir = fake_extract["out_dir"]
    assert "pnid_" in temp_dir
    # El contenido viaja inline (base64); el temp dir auto-creado ya no existe en disco.
    assert not os.path.exists(temp_dir)
    embedded = [c for c in out if isinstance(c, EmbeddedResource)]
    assert len(embedded) == 2  # los ficheros se incrustaron antes de borrar


async def test_pdf_shortcut_collected(fake_extract, tmp_path):
    pdf = tmp_path / "A.pdf"
    pdf.write_bytes(b"x")
    await _call({"pdf": str(pdf), "out": str(tmp_path / "o")})
    assert fake_extract["pdfs"] == [str(pdf)]


async def test_no_input_errors():
    out = await _call({})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    assert "pdf" in parsed["error"].lower()


async def test_missing_dir_errors(tmp_path):
    out = await _call({"dir": str(tmp_path / "no_existe")})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    assert "dir" in parsed["error"]


async def test_unknown_operation_errors(tmp_path):
    out = await _call({"dir": _valid_dir(tmp_path)}, operation="foo")
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "Unknown pnid operation" in parsed["error"]
