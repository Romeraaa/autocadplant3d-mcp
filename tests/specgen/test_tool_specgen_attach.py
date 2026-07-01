"""Tests for embedding the specgen ``build`` output in the chat as downloadable blobs.

Fast and deterministic: the pure helper (:func:`client.attach_files_result`) is exercised
with dummy files in ``tmp_path``, and the ``build`` branch of ``server.specgen`` is tested by
monkeypatching ``specgen_api.build`` to return a dict pointing at dummy files (so no real spec
is generated). Covers: attach_files=True embeds .pspc/.pspx/.xlsx as EmbeddedResource with a
decodable base64 blob; attach_files=False keeps the classic JSON-string behaviour; the size
guard moves oversized files to ``attachments_skipped``; and an omitted ``out`` + attach uses a
temp dir.
"""

from __future__ import annotations

import base64
import json

import pytest
from mcp.types import EmbeddedResource, TextContent

from autocad_mcp import server
from autocad_mcp.client import attach_files_result

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _dummy_files(tmp_path):
    pspc = tmp_path / "SPEC.pspc"
    pspx = tmp_path / "SPEC.pspx"
    xlsx = tmp_path / "REVISION_MATCHING.xlsx"
    pspc.write_bytes(b"pspc-bytes")
    pspx.write_bytes(b"pspx-bytes")
    xlsx.write_bytes(b"xlsx-bytes")
    return pspc, pspx, xlsx


# ---------------------------------------------------------------- helper in isolation
def test_helper_embeds_three_files(tmp_path):
    pspc, pspx, xlsx = _dummy_files(tmp_path)
    result = {"ok": True, "files": {}}
    out = attach_files_result(
        result,
        [(str(pspc), "application/octet-stream"),
         (str(pspx), "application/octet-stream"),
         (str(xlsx), XLSX_MIME)],
        max_bytes=1_000_000,
    )
    assert isinstance(out, list)
    assert isinstance(out[0], TextContent)
    embedded = [c for c in out if isinstance(c, EmbeddedResource)]
    assert len(embedded) == 3
    # each blob decodes back to the original bytes
    blobs = {c.resource.blob for c in embedded}
    decoded = {base64.b64decode(b) for b in blobs}
    assert {b"pspc-bytes", b"pspx-bytes", b"xlsx-bytes"} == decoded
    mimes = {c.resource.mimeType for c in embedded}
    assert XLSX_MIME in mimes
    assert "attachments_skipped" not in result


def test_helper_size_guard_skips_oversized(tmp_path):
    pspc, pspx, xlsx = _dummy_files(tmp_path)
    result = {"ok": True}
    # threshold below file size -> everything skipped -> plain JSON string
    out = attach_files_result(
        result,
        [(str(pspc), "application/octet-stream")],
        max_bytes=1,
    )
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert len(parsed["attachments_skipped"]) == 1
    assert parsed["attachments_skipped"][0]["name"] == "SPEC.pspc"
    assert "umbral" in parsed["attachments_skipped"][0]["reason"]


def test_helper_missing_file_reported(tmp_path):
    result = {"ok": True}
    out = attach_files_result(
        result,
        [(str(tmp_path / "nope.pspc"), "application/octet-stream")],
        max_bytes=1_000_000,
    )
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["attachments_skipped"][0]["reason"] == "no existe en disco"


def test_helper_partial_skip_still_embeds(tmp_path):
    pspc, _pspx, _xlsx = _dummy_files(tmp_path)
    result = {"ok": True}
    out = attach_files_result(
        result,
        [(str(pspc), "application/octet-stream"),
         (str(tmp_path / "missing.pspx"), "application/octet-stream")],
        max_bytes=1_000_000,
    )
    assert isinstance(out, list)
    assert len([c for c in out if isinstance(c, EmbeddedResource)]) == 1
    text = json.loads(out[0].text)
    assert len(text["attachments_skipped"]) == 1


# ---------------------------------------------------------------- server.build branch (mocked)
@pytest.fixture
def fake_build(monkeypatch, tmp_path):
    """Patch specgen_api.build to return a dict pointing at dummy files (no real spec)."""
    pspc, pspx, xlsx = _dummy_files(tmp_path)
    captured = {}

    def _build(*, piping_class, catalogs_dir, out_dir, spec_name, extend_h2, template_pspc):
        captured["out_dir"] = out_dir
        return {
            "ok": True,
            "spec_name": "SPEC",
            "files": {
                "pspc": str(pspc),
                "pspx": str(pspx),
                "review_xlsx": str(xlsx),
            },
            "components_built": 3,
            "coverage": {"total": 10},
            "verify": {"integrity_check": "ok"},
        }

    from autocad_mcp.specgen import api as specgen_api
    monkeypatch.setattr(specgen_api, "build", _build)
    return captured


async def _call_build(data):
    return await server.specgen(operation="build", data=data)


def _valid_paths(tmp_path):
    # piping_class must be an existing file, catalogs an existing dir (validated upstream)
    pc = tmp_path / "pc.xlsx"
    pc.write_bytes(b"x")
    cat = tmp_path / "cats"
    cat.mkdir()
    return str(pc), str(cat)


async def test_build_attach_true_returns_embedded(fake_build, tmp_path):
    pc, cat = _valid_paths(tmp_path)
    out = await _call_build({
        "piping_class": pc, "catalogs": cat, "out": str(tmp_path / "out"),
    })
    assert isinstance(out, list)
    embedded = [c for c in out if isinstance(c, EmbeddedResource)]
    assert len(embedded) == 3
    assert all(base64.b64decode(c.resource.blob) for c in embedded)


async def test_build_attach_false_returns_json(fake_build, tmp_path):
    pc, cat = _valid_paths(tmp_path)
    out = await _call_build({
        "piping_class": pc, "catalogs": cat, "out": str(tmp_path / "out"),
        "attach_files": False,
    })
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert "attachments_skipped" not in parsed


async def test_build_size_guard_skips(fake_build, tmp_path, monkeypatch):
    pc, cat = _valid_paths(tmp_path)
    monkeypatch.setenv("SPECGEN_MAX_ATTACH_BYTES", "1")
    out = await _call_build({
        "piping_class": pc, "catalogs": cat, "out": str(tmp_path / "out"),
    })
    # all three files exceed 1 byte -> nothing embedded -> plain JSON string
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert len(parsed["attachments_skipped"]) == 3


async def test_build_without_out_uses_temp_dir(fake_build, tmp_path):
    pc, cat = _valid_paths(tmp_path)
    out = await _call_build({"piping_class": pc, "catalogs": cat})
    assert isinstance(out, list)
    text = json.loads(out[0].text)
    assert "note" in text
    assert "temporal" in text["note"]
    # build was called with a temp dir (mkdtemp prefix)
    assert "specgen_" in fake_build["out_dir"]


async def test_build_without_out_no_attach_errors(fake_build, tmp_path):
    pc, cat = _valid_paths(tmp_path)
    out = await _call_build({"piping_class": pc, "catalogs": cat, "attach_files": False})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    assert "out" in parsed["error"]
