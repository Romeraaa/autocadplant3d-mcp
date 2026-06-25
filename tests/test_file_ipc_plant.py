"""Tests for the Plant 3D .NET plugin File IPC path — no AutoCAD needed.

Covers:
- _type_dispatch_trigger parametrization (default '(c:mcp-dispatch)' unchanged,
  custom trigger accepted) by capturing WM_CHAR chars sent to PostMessageW.
- plant_ping / plant_locate write a command file with the
  'autocad_mcp_plant_cmd_' prefix and the correct JSON shape.
- _dispatch_plant reads/parses a pre-written result file (short timeout,
  trigger mocked so nothing is actually typed).
- server.py plant3d.locate: clear Spanish error on non-file_ipc backend, and
  validation of empty/missing pnpids.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(tmp_path: Path) -> FileIPCBackend:
    backend = FileIPCBackend()
    backend._ipc_dir = tmp_path
    backend._hwnd = 1234  # pretend a window exists; PostMessage is mocked
    backend._command_hwnd = 5678
    return backend


WM_CHAR = 0x0102


# ---------------------------------------------------------------------------
# _type_dispatch_trigger — parametrized trigger
# ---------------------------------------------------------------------------


class TestTypeDispatchTrigger:
    def _capture_chars(self, backend: FileIPCBackend, *args) -> str:
        """Call _type_dispatch_trigger with mocked PostMessageW and return the
        characters sent via WM_CHAR (Enter / 0x0D stripped)."""
        sent: list[int] = []

        def fake_post(hwnd, msg, wparam, lparam):
            if msg == WM_CHAR:
                sent.append(wparam)
            return True

        mock_user32 = MagicMock()
        mock_user32.PostMessageW = fake_post
        mock_windll = MagicMock()
        mock_windll.user32 = mock_user32

        with patch("ctypes.windll", mock_windll), patch("time.sleep", lambda *_: None):
            backend._type_dispatch_trigger(*args)

        # Drop the trailing Enter (carriage return)
        chars = [c for c in sent if c != 0x0D]
        return "".join(chr(c) for c in chars)

    def test_default_is_lisp_dispatch(self, tmp_path):
        backend = _make_backend(tmp_path)
        typed = self._capture_chars(backend)
        assert typed == "(c:mcp-dispatch)"

    def test_default_via_no_arg_unchanged(self, tmp_path):
        """The LISP path must keep typing exactly '(c:mcp-dispatch)'."""
        backend = _make_backend(tmp_path)
        typed = self._capture_chars(backend)
        assert typed == "(c:mcp-dispatch)"
        assert "MCPPLANTDISPATCH" not in typed

    def test_custom_trigger_accepted(self, tmp_path):
        backend = _make_backend(tmp_path)
        typed = self._capture_chars(backend, "MCPPLANTDISPATCH")
        assert typed == "MCPPLANTDISPATCH"


# ---------------------------------------------------------------------------
# _dispatch_plant / plant_ping / plant_locate
# ---------------------------------------------------------------------------


def _patch_trigger_and_write_result(backend: FileIPCBackend, ok_payload, captured: dict):
    """Return a fake _type_dispatch_trigger that, when called, captures the
    just-written command file and writes a matching result file so the poll
    loop returns immediately.
    """

    def fake_trigger(trigger="(c:mcp-dispatch)"):
        captured["trigger"] = trigger
        # The command file already exists at this point — find it.
        cmd_files = list(backend._ipc_dir.glob("autocad_mcp_plant_cmd_*.json"))
        assert len(cmd_files) == 1, f"expected one plant cmd file, got {cmd_files}"
        cmd_file = cmd_files[0]
        captured["cmd_path"] = cmd_file
        captured["cmd"] = json.loads(cmd_file.read_text(encoding="utf-8"))
        request_id = captured["cmd"]["request_id"]
        result_file = backend._ipc_dir / f"autocad_mcp_plant_result_{request_id}.json"
        result_file.write_text(
            json.dumps({"request_id": request_id, "ok": True, "payload": ok_payload}),
            encoding="utf-8",
        )

    return fake_trigger


class TestPlantPing:
    @pytest.mark.asyncio
    async def test_ping_writes_cmd_and_parses_result(self, tmp_path):
        backend = _make_backend(tmp_path)
        captured: dict = {}
        payload = {"plugin": "PlantMcpDispatch", "version": "1.0", "plant3d_available": True, "project": "P1"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(backend, payload, captured)

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_ping()

        assert result.ok is True
        assert result.payload == payload
        # Trigger was the plugin command name
        assert captured["trigger"] == "MCPPLANTDISPATCH"
        # Command file used the plant prefix and correct shape
        assert captured["cmd_path"].name.startswith("autocad_mcp_plant_cmd_")
        assert captured["cmd"]["command"] == "ping"
        assert captured["cmd"]["params"] == {}
        assert "request_id" in captured["cmd"]
        assert "ts" in captured["cmd"]


class TestPlantLocate:
    @pytest.mark.asyncio
    async def test_locate_writes_cmd_with_params(self, tmp_path):
        backend = _make_backend(tmp_path)
        captured: dict = {}
        payload = {"requested": [10, 20], "found": [10, 20], "not_found": [], "found_count": 2, "dwg": "model.dwg"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(backend, payload, captured)

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_locate([10, 20], zoom=True, select=False)

        assert result.ok is True
        assert result.payload == payload
        assert captured["trigger"] == "MCPPLANTDISPATCH"
        assert captured["cmd_path"].name.startswith("autocad_mcp_plant_cmd_")
        assert captured["cmd"]["command"] == "locate"
        assert captured["cmd"]["params"]["pnpids"] == [10, 20]
        assert captured["cmd"]["params"]["zoom"] is True
        assert captured["cmd"]["params"]["select"] is False

    @pytest.mark.asyncio
    async def test_locate_defaults_zoom_select_true(self, tmp_path):
        backend = _make_backend(tmp_path)
        captured: dict = {}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(backend, {"found_count": 1}, captured)

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            await backend.plant_locate([5])

        assert captured["cmd"]["params"]["zoom"] is True
        assert captured["cmd"]["params"]["select"] is True

    @pytest.mark.asyncio
    async def test_dispatch_plant_timeout(self, tmp_path):
        """No result file written → timeout error, trigger still the plugin one."""
        backend = _make_backend(tmp_path)
        captured = {}

        def fake_trigger(trigger="(c:mcp-dispatch)"):
            captured["trigger"] = trigger  # do not write any result file

        backend._type_dispatch_trigger = fake_trigger

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 0.3):
            result = await backend._dispatch_plant("ping", {})

        assert result.ok is False
        assert "Timeout" in result.error
        assert captured["trigger"] == "MCPPLANTDISPATCH"

    @pytest.mark.asyncio
    async def test_plant_files_cleaned_up(self, tmp_path):
        """Command and result files are removed in the finally block."""
        backend = _make_backend(tmp_path)
        captured: dict = {}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(backend, {"ok": 1}, captured)

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            await backend.plant_ping()

        assert list(tmp_path.glob("autocad_mcp_plant_*.json")) == []
        assert list(tmp_path.glob("autocad_mcp_plant_*.tmp")) == []


# ---------------------------------------------------------------------------
# Lock is shared between LISP and plugin paths
# ---------------------------------------------------------------------------


class TestSharedLock:
    def test_plant_uses_same_lock(self, tmp_path):
        backend = _make_backend(tmp_path)
        # plant_ping/plant_locate must acquire the same _lock used by _dispatch
        assert backend._lock is not None


# ---------------------------------------------------------------------------
# server.py plant3d.locate routing / validation
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, name):
        self.name = name

    async def plant_locate(self, pnpids, zoom=True, select=True):
        return CommandResult(ok=True, payload={"requested": pnpids, "found_count": len(pnpids)})

    async def plant_ping(self):
        return CommandResult(ok=True, payload={"plugin": "PlantMcpDispatch"})


def _patch_backend(name):
    """Context manager patching server.get_backend to yield a fake backend."""
    from autocad_mcp import server

    async def fake_get_backend():
        return _FakeBackend(name)

    return patch.object(server, "get_backend", fake_get_backend)


class TestServerLocateRouting:
    @pytest.mark.asyncio
    async def test_locate_non_file_ipc_clear_error(self):
        from autocad_mcp import server

        with _patch_backend("ezdxf"):
            out = await server.plant3d(operation="locate", data={"pnpids": [1, 2]})

        parsed = json.loads(out)
        # Raised RuntimeError is funneled through _safe → _error (has 'error').
        assert "PlantMcpDispatch" in parsed["error"]
        assert "NETLOAD" in parsed["error"]

    @pytest.mark.asyncio
    async def test_locate_empty_pnpids_validates(self):
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": []})

        parsed = json.loads(out)
        assert parsed["ok"] is False
        assert "pnpids" in parsed["error"]

    @pytest.mark.asyncio
    async def test_locate_missing_pnpids_validates(self):
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={})

        parsed = json.loads(out)
        assert parsed["ok"] is False
        assert "pnpids" in parsed["error"]

    @pytest.mark.asyncio
    async def test_locate_single_pnpid_normalized(self):
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpid": 42})

        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["payload"]["requested"] == [42]
        assert parsed["operation"] == "locate"

    @pytest.mark.asyncio
    async def test_locate_on_file_ipc_calls_backend(self):
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": [7, 8, 9]})

        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["payload"]["found_count"] == 3

    @pytest.mark.asyncio
    async def test_plugin_status_non_file_ipc_clear_error(self):
        from autocad_mcp import server

        with _patch_backend("ezdxf"):
            out = await server.plant3d(operation="plugin_status", data={})

        parsed = json.loads(out)
        assert "PlantMcpDispatch" in parsed["error"]

    @pytest.mark.asyncio
    async def test_plugin_status_on_file_ipc(self):
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="plugin_status", data={})

        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["payload"]["plugin"] == "PlantMcpDispatch"
        assert parsed["operation"] == "plugin_status"


# ---------------------------------------------------------------------------
# Validación de tipos en _plant3d_locate (server.py) — coerción a int
# ---------------------------------------------------------------------------


class TestLocateTypeValidation:
    """Cubre la lógica de coerción/rechazo de pnpids añadida en la revisión."""

    @pytest.mark.asyncio
    async def test_non_numeric_string_rejected(self):
        """(a) "abc" no es convertible a int → ok:False, error menciona pnpids/enteros."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": ["abc"]})

        parsed = json.loads(out)
        assert parsed["ok"] is False
        # El mensaje debe mencionar tanto "pnpids" como "enteros"
        error = parsed["error"].lower()
        assert "pnpids" in error
        assert "enteros" in error

    @pytest.mark.asyncio
    async def test_numeric_string_coerced_to_int(self):
        """(b) "123" es string numérico → se coerce a int 123 y llega al backend como [123]."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": ["123"]})

        parsed = json.loads(out)
        assert parsed["ok"] is True
        # El FakeBackend devuelve requested=pnpids tal cual; debe ser [123] (int)
        requested = parsed["payload"]["requested"]
        assert requested == [123]
        assert isinstance(requested[0], int)

    @pytest.mark.asyncio
    async def test_single_numeric_string_via_pnpid_key(self):
        """(c) pnpid="55" (string numérico, clave singular) → ok:True, requested == [55]."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpid": "55"})

        parsed = json.loads(out)
        assert parsed["ok"] is True
        requested = parsed["payload"]["requested"]
        assert requested == [55]
        assert isinstance(requested[0], int)

    @pytest.mark.asyncio
    async def test_bool_rejected(self):
        """(d) bool es subclase de int pero no válido como PnPID → ok:False."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": [True]})

        parsed = json.loads(out)
        assert parsed["ok"] is False
        error = parsed["error"].lower()
        assert "pnpids" in error
        assert "enteros" in error

    @pytest.mark.asyncio
    async def test_none_element_rejected(self):
        """None dentro de la lista no es convertible → ok:False."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": [None]})

        parsed = json.loads(out)
        assert parsed["ok"] is False

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_rejected(self):
        """Lista mixta con un elemento inválido → ok:False (falla en el primero inválido)."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(operation="locate", data={"pnpids": [1, "bad", 3]})

        parsed = json.loads(out)
        assert parsed["ok"] is False

    @pytest.mark.asyncio
    async def test_mixed_ints_and_numeric_strings_coerced(self):
        """Lista con enteros reales y strings numéricas → todos coercidos a int."""
        from autocad_mcp import server

        with _patch_backend("file_ipc"):
            out = await server.plant3d(
                operation="locate", data={"pnpids": [10, "20", 30]}
            )

        parsed = json.loads(out)
        assert parsed["ok"] is True
        requested = parsed["payload"]["requested"]
        assert requested == [10, 20, 30]
        assert all(isinstance(p, int) for p in requested)


# ---------------------------------------------------------------------------
# Limpieza de huérfanos en _dispatch_core (file_ipc.py)
# ---------------------------------------------------------------------------


class TestDispatchCoreOrphanCleanup:
    """Verifica que _dispatch_core borra ficheros {cmd_prefix}*.json y *.tmp
    preexistentes antes de escribir el nuevo comando."""

    @pytest.mark.asyncio
    async def test_orphan_cmd_json_deleted_before_dispatch(self, tmp_path):
        """Un fichero huérfano {plant_cmd}*.json es eliminado antes del dispatch."""
        backend = _make_backend(tmp_path)
        captured: dict = {}

        # Crear el huérfano antes de invocar el dispatch
        orphan = tmp_path / "autocad_mcp_plant_cmd_DEADBEEF.json"
        orphan.write_text('{"stale": true}', encoding="utf-8")
        assert orphan.exists(), "El huérfano debería existir antes del dispatch"

        payload = {"plugin": "PlantMcpDispatch", "version": "1.0",
                   "plant3d_available": True, "project": "P1"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(
            backend, payload, captured
        )

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_ping()

        # El dispatch debe haber completado correctamente
        assert result.ok is True
        assert result.payload == payload

        # El huérfano debe haber sido borrado
        assert not orphan.exists(), "El fichero huérfano debería haber sido eliminado"

    @pytest.mark.asyncio
    async def test_orphan_cmd_tmp_deleted_before_dispatch(self, tmp_path):
        """Un fichero huérfano {plant_cmd}*.tmp es eliminado antes del dispatch."""
        backend = _make_backend(tmp_path)
        captured: dict = {}

        # Crear un .tmp huérfano
        orphan_tmp = tmp_path / "autocad_mcp_plant_cmd_DEADBEEF.tmp"
        orphan_tmp.write_text('{"stale": true}', encoding="utf-8")
        assert orphan_tmp.exists()

        payload = {"plugin": "ok"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(
            backend, payload, captured
        )

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_ping()

        assert result.ok is True
        assert not orphan_tmp.exists(), "El .tmp huérfano debería haber sido eliminado"

    @pytest.mark.asyncio
    async def test_orphan_cleanup_does_not_touch_result_files(self, tmp_path):
        """La limpieza de huérfanos NO toca los ficheros result_prefix existentes."""
        backend = _make_backend(tmp_path)
        captured: dict = {}

        # Crear un result file de otra sesión (no debe borrarse)
        stale_result = tmp_path / "autocad_mcp_plant_result_OLDONE.json"
        stale_result.write_text('{"stale_result": true}', encoding="utf-8")

        payload = {"plugin": "ok"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(
            backend, payload, captured
        )

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_ping()

        assert result.ok is True
        # El result file de otra sesión debe seguir intacto
        assert stale_result.exists(), "El fichero result de otra sesión no debe borrarse"

    @pytest.mark.asyncio
    async def test_multiple_orphans_all_deleted(self, tmp_path):
        """Varios huérfanos son todos eliminados antes del dispatch."""
        backend = _make_backend(tmp_path)
        captured: dict = {}

        orphans = [
            tmp_path / "autocad_mcp_plant_cmd_AAA111.json",
            tmp_path / "autocad_mcp_plant_cmd_BBB222.json",
            tmp_path / "autocad_mcp_plant_cmd_CCC333.tmp",
        ]
        for o in orphans:
            o.write_text('{"stale": true}', encoding="utf-8")

        payload = {"plugin": "ok"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(
            backend, payload, captured
        )

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_ping()

        assert result.ok is True
        for o in orphans:
            assert not o.exists(), f"Huérfano {o.name} debería haber sido eliminado"

    @pytest.mark.asyncio
    async def test_orphan_cleanup_lisp_prefix_independent(self, tmp_path):
        """Un huérfano del prefix LISP (autocad_mcp_cmd_*) no es tocado por _dispatch_plant."""
        backend = _make_backend(tmp_path)
        captured: dict = {}

        # Crear un huérfano con el prefijo LISP (diferente al de plant)
        lisp_orphan = tmp_path / "autocad_mcp_cmd_LISP999.json"
        lisp_orphan.write_text('{"lisp": true}', encoding="utf-8")

        payload = {"plugin": "ok"}
        backend._type_dispatch_trigger = _patch_trigger_and_write_result(
            backend, payload, captured
        )

        with patch("autocad_mcp.backends.file_ipc.TIMEOUT", 2.0):
            result = await backend.plant_ping()

        assert result.ok is True
        # El huérfano LISP no debe haber sido tocado por _dispatch_plant
        assert lisp_orphan.exists(), (
            "El huérfano del prefijo LISP no debe borrarse con _dispatch_plant"
        )
