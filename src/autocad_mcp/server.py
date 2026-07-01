"""AutoCAD MCP Server v3.1 — 8 consolidated tools with operation dispatch.

Tools: drawing, entity, layer, block, annotation, pid, view, system
"""

from __future__ import annotations

import os

import structlog
from mcp.server.fastmcp import FastMCP

from autocad_mcp.client import (
    _error,
    _json,
    _safe,
    add_screenshot_if_available,
    get_backend,
)

# FastMCP validates return types via Pydantic. Tools that may return
# ImageContent (screenshot) alongside TextContent need a union return type.
ToolResult = str | list

log = structlog.get_logger()

mcp = FastMCP("autocad-mcp")


# ==========================================================================
# 1. drawing — File/drawing management
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Drawing Operations", "readOnlyHint": False})
@_safe("drawing")
async def drawing(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Drawing file management.

    Operations:
      create     — Create a new empty drawing. data: {name?}
      open       — Open an existing drawing. data: {path}
      info       — Get drawing extents, entity count, layers, blocks.
      save       — Save current drawing. data: {path?} (saves to path if given, else QSAVE)
      save_as_dxf — Export as DXF. data: {path}
      plot_pdf   — Plot to PDF. data: {path}
      purge      — Purge unused objects.
      get_variables — Get system variables. data: {names: [...]}
      undo       — Undo last operation.
      redo       — Redo last undone operation.
    """
    data = data or {}
    backend = await get_backend()

    if operation == "create":
        result = await backend.drawing_create(data.get("name"))
    elif operation == "info":
        result = await backend.drawing_info()
    elif operation == "save":
        result = await backend.drawing_save(data.get("path"))
    elif operation == "save_as_dxf":
        result = await backend.drawing_save_as_dxf(data["path"])
    elif operation == "plot_pdf":
        result = await backend.drawing_plot_pdf(data["path"])
    elif operation == "purge":
        result = await backend.drawing_purge()
    elif operation == "get_variables":
        result = await backend.drawing_get_variables(data.get("names"))
    elif operation == "open":
        result = await backend.drawing_open(data["path"])
    elif operation == "undo":
        result = await backend.undo()
    elif operation == "redo":
        result = await backend.redo()
    else:
        return _json({"error": f"Unknown drawing operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 2. entity — Entity CRUD + modification
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Entity Operations", "readOnlyHint": False})
@_safe("entity")
async def entity(
    operation: str,
    x1: float | None = None,
    y1: float | None = None,
    x2: float | None = None,
    y2: float | None = None,
    points: list[list[float]] | None = None,
    layer: str | None = None,
    entity_id: str | None = None,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Entity creation, querying, and modification.

    Create operations:
      create_line       — x1, y1, x2, y2, layer?
      create_circle     — data: {cx, cy, radius}, layer?
      create_polyline   — points: [[x,y],...], data: {closed?}, layer?
      create_rectangle  — x1, y1, x2, y2, layer?
      create_arc        — data: {cx, cy, radius, start_angle, end_angle}, layer?
      create_ellipse    — data: {cx, cy, major_x, major_y, ratio}, layer?
      create_mtext      — data: {x, y, width, text, height?}, layer?
      create_hatch      — entity_id, data: {pattern?}

    Read operations:
      list              — layer? → list entities
      count             — layer? → count entities
      get               — entity_id → entity details

    Modify operations:
      copy    — entity_id, data: {dx, dy}
      move    — entity_id, data: {dx, dy}
      rotate  — entity_id, data: {cx, cy, angle}
      scale   — entity_id, data: {cx, cy, factor}
      mirror  — entity_id, x1, y1, x2, y2
      offset  — entity_id, data: {distance}
      array   — entity_id, data: {rows, cols, row_dist, col_dist}
      fillet  — data: {id1, id2, radius}
      chamfer — data: {id1, id2, dist1, dist2}
      erase   — entity_id
    """
    data = data or {}
    backend = await get_backend()

    # --- Create ---
    if operation == "create_line":
        result = await backend.create_line(x1, y1, x2, y2, layer)
    elif operation == "create_circle":
        result = await backend.create_circle(data["cx"], data["cy"], data["radius"], layer)
    elif operation == "create_polyline":
        result = await backend.create_polyline(points or [], data.get("closed", False), layer)
    elif operation == "create_rectangle":
        result = await backend.create_rectangle(x1, y1, x2, y2, layer)
    elif operation == "create_arc":
        result = await backend.create_arc(data["cx"], data["cy"], data["radius"], data["start_angle"], data["end_angle"], layer)
    elif operation == "create_ellipse":
        result = await backend.create_ellipse(data["cx"], data["cy"], data["major_x"], data["major_y"], data["ratio"], layer)
    elif operation == "create_mtext":
        result = await backend.create_mtext(data["x"], data["y"], data["width"], data["text"], data.get("height", 2.5), layer)
    elif operation == "create_hatch":
        result = await backend.create_hatch(entity_id, data.get("pattern", "ANSI31"))
    # --- Read ---
    elif operation == "list":
        result = await backend.entity_list(layer)
    elif operation == "count":
        result = await backend.entity_count(layer)
    elif operation == "get":
        result = await backend.entity_get(entity_id)
    # --- Modify ---
    elif operation == "copy":
        result = await backend.entity_copy(entity_id, data["dx"], data["dy"])
    elif operation == "move":
        result = await backend.entity_move(entity_id, data["dx"], data["dy"])
    elif operation == "rotate":
        result = await backend.entity_rotate(entity_id, data["cx"], data["cy"], data["angle"])
    elif operation == "scale":
        result = await backend.entity_scale(entity_id, data["cx"], data["cy"], data["factor"])
    elif operation == "mirror":
        result = await backend.entity_mirror(entity_id, x1, y1, x2, y2)
    elif operation == "offset":
        result = await backend.entity_offset(entity_id, data["distance"])
    elif operation == "array":
        result = await backend.entity_array(entity_id, data["rows"], data["cols"], data["row_dist"], data["col_dist"])
    elif operation == "fillet":
        result = await backend.entity_fillet(data["id1"], data["id2"], data["radius"])
    elif operation == "chamfer":
        result = await backend.entity_chamfer(data["id1"], data["id2"], data["dist1"], data["dist2"])
    elif operation == "erase":
        result = await backend.entity_erase(entity_id)
    else:
        return _json({"error": f"Unknown entity operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 3. layer — Layer management
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Layer Operations", "readOnlyHint": False})
@_safe("layer")
async def layer(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Layer creation and management.

    Operations:
      list            — List all layers with properties.
      create          — data: {name, color?, linetype?}
      set_current     — data: {name}
      set_properties  — data: {name, color?, linetype?, lineweight?}
      freeze          — data: {name}
      thaw            — data: {name}
      lock            — data: {name}
      unlock          — data: {name}
    """
    data = data or {}
    backend = await get_backend()

    if operation == "list":
        result = await backend.layer_list()
    elif operation == "create":
        result = await backend.layer_create(data["name"], data.get("color", "white"), data.get("linetype", "CONTINUOUS"))
    elif operation == "set_current":
        result = await backend.layer_set_current(data["name"])
    elif operation == "set_properties":
        result = await backend.layer_set_properties(data["name"], data.get("color"), data.get("linetype"), data.get("lineweight"))
    elif operation == "freeze":
        result = await backend.layer_freeze(data["name"])
    elif operation == "thaw":
        result = await backend.layer_thaw(data["name"])
    elif operation == "lock":
        result = await backend.layer_lock(data["name"])
    elif operation == "unlock":
        result = await backend.layer_unlock(data["name"])
    else:
        return _json({"error": f"Unknown layer operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 4. block — Block operations
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Block Operations", "readOnlyHint": False})
@_safe("block")
async def block(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Block definition, insertion, and attribute management.

    Operations:
      list                 — List all block definitions.
      insert               — data: {name, x, y, scale?, rotation?, block_id?}
      insert_with_attributes — data: {name, x, y, scale?, rotation?, attributes: {tag: value}}
      get_attributes       — data: {entity_id}
      update_attribute     — data: {entity_id, tag, value}
      define               — data: {name, entities: [{type, ...}]}
    """
    data = data or {}
    backend = await get_backend()

    if operation == "list":
        result = await backend.block_list()
    elif operation == "insert":
        result = await backend.block_insert(
            data["name"], data["x"], data["y"],
            data.get("scale", 1.0), data.get("rotation", 0.0), data.get("block_id"),
        )
    elif operation == "insert_with_attributes":
        result = await backend.block_insert_with_attributes(
            data["name"], data["x"], data["y"],
            data.get("scale", 1.0), data.get("rotation", 0.0), data.get("attributes"),
        )
    elif operation == "get_attributes":
        result = await backend.block_get_attributes(data["entity_id"])
    elif operation == "update_attribute":
        result = await backend.block_update_attribute(data["entity_id"], data["tag"], data["value"])
    elif operation == "define":
        result = await backend.block_define(data["name"], data.get("entities", []))
    else:
        return _json({"error": f"Unknown block operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 5. annotation — Text, dimensions, leaders
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD Annotation Operations", "readOnlyHint": False})
@_safe("annotation")
async def annotation(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Annotation: text, dimensions, and leaders.

    Operations:
      create_text             — data: {x, y, text, height?, rotation?, layer?}
      create_dimension_linear — data: {x1, y1, x2, y2, dim_x, dim_y}
      create_dimension_aligned — data: {x1, y1, x2, y2, offset}
      create_dimension_angular — data: {cx, cy, x1, y1, x2, y2}
      create_dimension_radius — data: {cx, cy, radius, angle}
      create_leader           — data: {points: [[x,y],...], text}
    """
    data = data or {}
    backend = await get_backend()

    if operation == "create_text":
        result = await backend.create_text(
            data["x"], data["y"], data["text"],
            data.get("height", 2.5), data.get("rotation", 0.0), data.get("layer"),
        )
    elif operation == "create_dimension_linear":
        result = await backend.create_dimension_linear(
            data["x1"], data["y1"], data["x2"], data["y2"], data["dim_x"], data["dim_y"],
        )
    elif operation == "create_dimension_aligned":
        result = await backend.create_dimension_aligned(
            data["x1"], data["y1"], data["x2"], data["y2"], data["offset"],
        )
    elif operation == "create_dimension_angular":
        result = await backend.create_dimension_angular(
            data["cx"], data["cy"], data["x1"], data["y1"], data["x2"], data["y2"],
        )
    elif operation == "create_dimension_radius":
        result = await backend.create_dimension_radius(
            data["cx"], data["cy"], data["radius"], data["angle"],
        )
    elif operation == "create_leader":
        result = await backend.create_leader(data["points"], data["text"])
    else:
        return _json({"error": f"Unknown annotation operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 6. pid — P&ID operations (CTO library)
# ==========================================================================


@mcp.tool(annotations={"title": "P&ID Operations (CTO Library)", "readOnlyHint": False})
@_safe("pid")
async def pid(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """P&ID drawing with CTO symbol library.

    Operations:
      setup_layers     — Create standard P&ID layers.
      insert_symbol    — data: {category, symbol, x, y, scale?, rotation?}
      list_symbols     — data: {category}
      draw_process_line — data: {x1, y1, x2, y2}
      connect_equipment — data: {x1, y1, x2, y2}
      add_flow_arrow   — data: {x, y, rotation?}
      add_equipment_tag — data: {x, y, tag, description?}
      add_line_number  — data: {x, y, line_num, spec}
      insert_valve     — data: {x, y, valve_type, rotation?, attributes?}
      insert_instrument — data: {x, y, instrument_type, rotation?, tag_id?, range_value?}
      insert_pump      — data: {x, y, pump_type, rotation?, attributes?}
      insert_tank      — data: {x, y, tank_type, scale?, attributes?}
    """
    data = data or {}
    backend = await get_backend()

    if operation == "setup_layers":
        result = await backend.pid_setup_layers()
    elif operation == "insert_symbol":
        result = await backend.pid_insert_symbol(
            data["category"], data["symbol"], data["x"], data["y"],
            data.get("scale", 1.0), data.get("rotation", 0.0),
        )
    elif operation == "list_symbols":
        result = await backend.pid_list_symbols(data["category"])
    elif operation == "draw_process_line":
        result = await backend.pid_draw_process_line(data["x1"], data["y1"], data["x2"], data["y2"])
    elif operation == "connect_equipment":
        result = await backend.pid_connect_equipment(data["x1"], data["y1"], data["x2"], data["y2"])
    elif operation == "add_flow_arrow":
        result = await backend.pid_add_flow_arrow(data["x"], data["y"], data.get("rotation", 0.0))
    elif operation == "add_equipment_tag":
        result = await backend.pid_add_equipment_tag(data["x"], data["y"], data["tag"], data.get("description", ""))
    elif operation == "add_line_number":
        result = await backend.pid_add_line_number(data["x"], data["y"], data["line_num"], data["spec"])
    elif operation == "insert_valve":
        result = await backend.pid_insert_valve(
            data["x"], data["y"], data["valve_type"],
            data.get("rotation", 0.0), data.get("attributes"),
        )
    elif operation == "insert_instrument":
        result = await backend.pid_insert_instrument(
            data["x"], data["y"], data["instrument_type"],
            data.get("rotation", 0.0), data.get("tag_id", ""), data.get("range_value", ""),
        )
    elif operation == "insert_pump":
        result = await backend.pid_insert_pump(
            data["x"], data["y"], data["pump_type"],
            data.get("rotation", 0.0), data.get("attributes"),
        )
    elif operation == "insert_tank":
        result = await backend.pid_insert_tank(
            data["x"], data["y"], data["tank_type"],
            data.get("scale", 1.0), data.get("attributes"),
        )
    else:
        return _json({"error": f"Unknown pid operation: {operation}"})

    return await add_screenshot_if_available(result, include_screenshot)


# ==========================================================================
# 7. view — Viewport and screenshot
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD View Operations", "readOnlyHint": True})
@_safe("view")
async def view(
    operation: str,
    x1: float | None = None,
    y1: float | None = None,
    x2: float | None = None,
    y2: float | None = None,
) -> ToolResult:
    """Viewport control and screenshot capture.

    Operations:
      zoom_extents   — Zoom to show all entities.
      zoom_window    — Zoom to window: x1, y1, x2, y2
      get_screenshot — Capture current view as PNG image.
    """
    backend = await get_backend()

    if operation == "zoom_extents":
        result = await backend.zoom_extents()
        return _json(result.to_dict())
    elif operation == "zoom_window":
        result = await backend.zoom_window(x1, y1, x2, y2)
        return _json(result.to_dict())
    elif operation == "get_screenshot":
        result = await backend.get_screenshot()
        if result.ok and result.payload:
            from mcp.types import ImageContent, TextContent

            return [
                TextContent(type="text", text=_json({"ok": True, "screenshot": "attached"})),
                ImageContent(type="image", data=result.payload, mimeType="image/png"),
            ]
        return _json(result.to_dict())
    else:
        return _json({"error": f"Unknown view operation: {operation}"})


# ==========================================================================
# 8. system — Server management
# ==========================================================================


@mcp.tool(annotations={"title": "AutoCAD MCP System", "readOnlyHint": True})
@_safe("system")
async def system(
    operation: str,
    data: dict | None = None,
    include_screenshot: bool = False,
) -> ToolResult:
    """Server status and management.

    Operations:
      status        — Backend info, capabilities, health check.
      health        — Quick health check (ping backend).
      get_backend   — Return current backend name and capabilities.
      runtime       — Return process/runtime details for spawn diagnostics.
      init          — Re-initialize the backend.
      execute_lisp  — Execute arbitrary AutoLISP code (File IPC only). data: {code}
    """
    data = data or {}

    if operation == "status" or operation == "get_backend":
        backend = await get_backend()
        result = await backend.status()
        return await add_screenshot_if_available(result, include_screenshot)
    elif operation == "health":
        try:
            backend = await get_backend()
            result = await backend.status()
            return _json({"ok": result.ok, "backend": backend.name})
        except Exception as e:
            return _json({"ok": False, "error": str(e)})
    elif operation == "runtime":
        import os
        import sys

        return _json(
            {
                "ok": True,
                "platform": sys.platform,
                "python": sys.executable,
                "cwd": os.getcwd(),
                "backend_env": os.environ.get("AUTOCAD_MCP_BACKEND", "auto"),
                "wsl_interop": bool(os.environ.get("WSL_INTEROP")),
            }
        )
    elif operation == "init":
        # Force re-initialization
        from autocad_mcp import client
        client._backend = None
        backend = await get_backend()
        result = await backend.status()
        return _json(result.to_dict())
    elif operation == "execute_lisp":
        backend = await get_backend()
        if not data.get("code"):
            return _json({"error": "data.code is required"})
        result = await backend.execute_lisp(data["code"])
        return await add_screenshot_if_available(result, include_screenshot)
    else:
        return _json({"error": f"Unknown system operation: {operation}"})


# ==========================================================================
# 9. plant3d — Read-only queries over Plant 3D project databases
# ==========================================================================


@mcp.tool(annotations={"title": "Plant 3D Project Query (read-only)", "readOnlyHint": True})
@_safe("plant3d")
async def plant3d(
    operation: str,
    data: dict | None = None,
) -> ToolResult:
    """Consultas de solo lectura sobre los datos de un proyecto AutoCAD Plant 3D.

    Lee directamente las bases SQLite (.dcf) del proyecto — no requiere el
    plugin .NET. Nunca modifica el proyecto.

    Por defecto consulta el **proyecto que el usuario tiene abierto en AutoCAD**:
    si no se indica `project`, detecta el dibujo activo (DWGPREFIX) y sube hasta
    su `Project.xml`. Esto requiere AutoCAD abierto con un dibujo del proyecto.
    Alternativamente, se puede indicar `project` (ruta a la carpeta o, si
    AUTOCAD_MCP_PLANT3D_ROOT está configurado, el nombre).

    Operations:
      detect_project — Identifica el proyecto Plant 3D actualmente abierto.
                       data: {project?}
      line_summary   — Resumen de líneas de tubería: por cada LineNumberTag,
                       nº de componentes, spools, servicios, specs y diámetros.
                       data: {project?}
      find_untagged  — Componentes de tubería sin LineNumberTag (NULL, vacío
                       o '?'), con desglose por clase y por spec.
                       data: {project?}
      validate_specs — Valida specs de tubería: spec real != Required Spec,
                       líneas con specs mezcladas, specs vacías, specs sin
                       fichero .pspc, y Schedule/Material fuera del catálogo.
                       data: {project?, ignore_specs?, limit?}
      list_specs     — Lista las specs de tubería del proyecto cruzando las
                       USADAS en el modelo (EngineeringItems.Spec de Piping.dcf,
                       con recuento de componentes) con las DISPONIBLES en
                       catálogo (ficheros .pspc de 'Spec Sheets'). Cada spec con
                       {spec, used (nº componentes), has_pspc}. Degrada con
                       gracia si falta 'Spec Sheets' o EngineeringItems.
                       data: {project?}
                       Devuelve {ok, project, path, count, specs, notes}.
      spec_contents  — Componentes PERMITIDOS por una spec: abre su .pspc en
                       'Spec Sheets' (tabla EngineeringItems) y lista cada
                       componente {class, description, size, schedule, material,
                       end_type, pressure_class, item_code}. Solo lectura. Si la
                       spec no tiene .pspc → ok:False con mensaje en español.
                       data: {spec, project?, limit?=100 (0=sin tope)}
                       Devuelve {ok, project, spec, path_pspc, count,
                       components, notes}.
      list_lines     — LINE LIST: una fila por línea (LineNumberTag válido) con
                       nº de componentes, servicio/spec/tamaño nominal de la
                       cabecera, specs reales (y spec_mixed), tamaños por unidad,
                       aislamiento y DWGs del modelo 3D.
                       data: {project?, ignore_specs?, limit?}
      list_components— Inventario de componentes de tubería con filtros
                       opcionales: por clase (pipe/valve/fitting/flange/
                       instrument/support o PartCategory literal), por línea,
                       por spec y por tamaño (exige unidad: {value, unit}).
                       Cada componente con pnpid, clase, tag, descripción, spec,
                       tamaño y línea; incluye desglose por clase.
                       data: {project?, classes?, line?, spec?, size?, limit?}
      list_valves    — Inventario de válvulas: preset de list_components con la
                       clase fijada a 'valve' (cualquier 'classes' se ignora).
                       Admite los demás filtros (línea, spec, tamaño) y limit.
                       data: {project?, line?, spec?, size?, limit?}
      list_instruments — Inventario de instrumentos: preset de list_components
                       con la clase fijada a 'instrument' (cualquier 'classes' se
                       ignora). Admite los demás filtros (línea, spec, tamaño) y limit.
                       data: {project?, line?, spec?, size?, limit?}
      bom            — Bill of Materials: agrega los componentes por
                       (clase, spec, tamaño, descripción) con su cantidad
                       (recuento de componentes, no longitudes). Admite los
                       mismos filtros de alcance que list_components; aquí limit
                       acota el nº de líneas de BOM devueltas.
                       data: {project?, classes?, line?, spec?, size?, limit?}
      pipe_length    — Suma longitudes reales de tramos de tubería (tabla Pipe,
                       columna Length, solo PartCategory='Pipe'). Agrupa por
                       group_by (line|spec|size; default line) y admite filtros
                       por línea, spec y tamaño (diámetro; exige unidad). Reporta
                       los tramos sin línea aparte (untagged) y la unidad leída
                       de LengthUnit (no asumida). Aquí limit acota el nº de
                       grupos devueltos.
                       data: {project?, group_by?, line?, spec?, size?, limit?}
      weld_list      — Recuento y desglose de soldaduras (tablas Buttweld/
                       Socketweld/TapWeld; el subtipo type se deriva de la
                       tabla). Agrupa por group_by (line|size|spec|shop_field|
                       type; default line) y admite filtros por línea, spec,
                       tamaño (diámetro; exige unidad), shop_field (shop|field)
                       y weld_type (butt|socket|tap). Devuelve siempre los
                       desgloses globales by_type y by_shop_field, y reporta
                       aparte las soldaduras sin línea (untagged). No usa
                       WeldNumber (NULL; numeración isométrica): cuenta y
                       desglosa, no numera. Aquí limit acota el nº de grupos.
                       data: {project?, group_by?, line?, spec?, size?,
                       shop_field?, weld_type?, limit?}
      bolt_gasket_list — Recuento y desglose de pernos y juntas (material de
                       montaje de bridas; tablas BoltSet/Gasket, NO Fasteners).
                       Métricas multi-valor por bucket: item_count, bolt_sets,
                       individual_bolts (Σ NumberInSet) y gaskets. Agrupa por
                       group_by (line|size|spec|material|item_type|shop_field|
                       bolt_size; default line) y admite filtros por línea,
                       spec, tamaño (diámetro de brida; exige unidad),
                       shop_field (shop|field) e item_type (bolt|gasket).
                       Devuelve siempre los desgloses globales by_item_type y
                       by_shop_field, y reporta aparte los items sin línea
                       (untagged). Aquí limit acota el nº de grupos.
                       data: {project?, group_by?, line?, spec?, size?,
                       shop_field?, item_type?, limit?}
      list_equipment — Lista los equipos del proyecto (tabla Equipment de
                       Piping.dcf: bombas, equipo misceláneo, intercambiadores,
                       etc.). La clase real se toma de PnPBase.PnPClassName y las
                       boquillas (nozzles) se asocian vía AssetOwnership
                       (Owner=equipo, Owned=nozzle). Cada equipo con pnpid,
                       class, tag, type, number, area, nozzle_count y nozzles.
                       Solo lectura; degrada con gracia si falta una tabla.
                       data: {project?}
                       Devuelve {ok, project, count, by_class, equipment, notes}.
      get_component  — Volcado COMPLETO de propiedades de UN objeto Plant 3D por
                       pnpid (entero) o por tag (PnPTagRegistry; si resuelve a
                       varios, devuelve el primero y avisa). La clase se lee de
                       PnPBase.PnPClassName; vuelca todas las columnas de su tabla
                       de clase + EngineeringItems (omite GUID/timestamp). Incluye
                       DWG(s) y handle(s) vía PnPDataLinks⋈PnPDrawings (para
                       localizar luego con 'locate'). Solo lectura.
                       data: {pnpid | tag, project?}
                       Devuelve {ok, project, pnpid, class, properties, dwgs,
                       notes}; pnpid inexistente → ok:False.
      find_missing_properties — Lista los componentes a los que les faltan
                       propiedades obligatorias, según un perfil por clase
                       configurable. Se apoya en list_components (solo lectura,
                       sin SQL propio): para cada componente comprueba los
                       campos requeridos de SU clase canónica (pipe/valve/
                       fitting/flange/instrument; el resto usa el perfil por
                       defecto). Un campo cuenta como faltante si está vacío
                       (None/""/espacios); 'tag' usa el criterio de tag en
                       blanco y 'size' considera faltante también el valor "?".
                       Perfil por defecto: pipe/fitting/flange=[spec,size,line],
                       valve=[spec,size,line,tag], instrument=[tag,line].
                       data['required'] sobrescribe el perfil: una lista de
                       campos (aplica a TODAS las clases) o un dict {clase:
                       [campos]} (sustituye solo las clases citadas). Campos
                       válidos: spec,size,line,tag,description; uno desconocido
                       se ignora con nota. Filtros opcionales reenviados:
                       classes, line, spec, dwg. limit acota los componentes con
                       faltantes (default 50, 0 = sin tope).
                       data: {project?, required?, classes?, line?, spec?, dwg?,
                       limit?}
                       Devuelve {ok, project, path, profile, filters, count,
                       omitted, by_class, components:[{pnpid, class, tag, line,
                       missing}], notes}.
      export         — Vuelca cualquier listado a un fichero CSV o XLSX. Solo
                       lectura sobre los .dcf: el único fichero que se ESCRIBE
                       es el de salida. kind (obligatorio) elige el listado:
                       lines | components | valves | instruments | equipment |
                       bom | pipe_length | weld_list | bolt_gasket_list | specs
                       | untagged. path (obligatorio) fija la salida; el formato
                       se decide por extensión: .csv (utf-8-sig, para que Excel
                       respete acentos) o .xlsx (openpyxl; una hoja, primera
                       fila = cabeceras). Otra extensión → error. Crea las
                       carpetas padre si faltan. El resto de data se reenvía como
                       filtros al listado subyacente y limit se fuerza a 0 (sin
                       tope) para no truncar la exportación. Columnas = unión
                       ordenada y estable de las claves de las filas; un valor
                       anidado se serializa a texto compacto. Si falta openpyxl
                       al exportar a XLSX → ok:False con mensaje (no afecta CSV).
                       data: {kind, path, project?, ...filtros del kind}
                       Devuelve {ok, project, path, kind, format, rows, columns,
                       notes} (solo metadatos, nunca los datos).
      list_projects  — Lista proyectos bajo una raíz. data: {root?}
                       (usa AUTOCAD_MCP_PLANT3D_ROOT si no se indica root)
      list_drawings  — Lista y clasifica los dibujos del proyecto (PnPDrawings
                       de Piping.dcf y, si existe, ProcessPower.dcf). Tipos:
                       3d_model | spec_sheet | folder | isometric | ortho | pid.
                       Solo lectura. data: {project?}
                       Devuelve {ok, project, path, count, by_type, drawings}.
      locate         — Localiza objetos Plant 3D en el DIBUJO por PnPID y los
                       resalta/encuadra. A diferencia de las consultas SQLite
                       (que solo leen los .dcf), esta operación SÍ localiza el
                       objeto en el modelo 3D. Va por el plugin .NET, NO por
                       SQLite: requiere AutoCAD 2026 abierto con el plugin
                       PlantMcpDispatch cargado (NETLOAD) y el DWG de modelo
                       correspondiente abierto.
                       Los objetos se indican por PnPID, o se resuelven a PnPIDs
                       vía SQLite a partir de un 'tag' (PnPTagRegistry) o de una
                       'line' (número de línea; PipeRunComponent). Como el plugin
                       filtra por el DWG activo, de una línea/tag multi-dibujo
                       solo se localizan los objetos del dibujo abierto.
                       Con isolate?=True además AÍSLA los objetos localizados,
                       ocultando el resto del modelo (devuelve isolated en el
                       payload); revertir con la operación 'unisolate'.
                       data: {pnpids: [int] (o pnpid único) | tag | line,
                       zoom?=True, select?=True, isolate?=False, project?}
      unisolate      — Revierte el aislado de 'locate' (vuelve a MOSTRAR todos
                       los objetos del dibujo). Va por el plugin .NET, NO por
                       SQLite: requiere AutoCAD 2026 abierto con el plugin
                       PlantMcpDispatch cargado (NETLOAD).
                       Devuelve {dwg, ok, notes}.
      plugin_status  — Comprueba que el plugin .NET responde (ping). Devuelve
                       {plugin, version, plant3d_available, project}. Requiere
                       AutoCAD abierto con el plugin cargado (NETLOAD).
      pnid_probe     — DIAGNÓSTICO del plugin P&ID: inspecciona el dibujo
                       activo (sin tocar los .dcf) y devuelve un resumen de las
                       partes y líneas P&ID detectadas. Va por el plugin .NET,
                       NO por SQLite: requiere AutoCAD 2026 abierto con el plugin
                       PlantMcpDispatch cargado (NETLOAD).
                       data: {limit?=50}
                       Devuelve {pnid_part_found, dwg, row_count, by_class,
                       sample_rows, line_count, sample_lines, notes}.
    """
    data = data or {}
    from autocad_mcp import plant3d_query

    # --- Plugin .NET (File IPC, NO SQLite): locate / plugin_status ---
    if operation == "locate":
        return await _plant3d_locate(data)
    elif operation == "unisolate":
        return await _plant3d_unisolate(data)
    elif operation == "plugin_status":
        return await _plant3d_plugin_status()
    elif operation == "pnid_probe":
        return await _plant3d_pnid_probe(data)

    if operation == "list_projects":
        result = plant3d_query.list_projects(data.get("root"))
    elif operation == "detect_project":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.project_info(project)
    elif operation == "line_summary":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.line_summary(project)
    elif operation == "find_untagged":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.find_untagged(project)
    elif operation == "validate_specs":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.validate_specs(project, data)
    elif operation == "list_specs":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.list_specs(project, data)
    elif operation == "spec_contents":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.spec_contents(project, data)
    elif operation == "list_lines":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.list_lines(project, data)
    elif operation == "list_drawings":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.list_drawings(project)
    elif operation == "list_components":
        project = data.get("project") or await _detect_open_project()
        # "DWG abierto en AutoCAD": active_dwg:true o dwg:"@active" leen DWGNAME
        # del dibujo activo y lo traducen al basename que casa con PnPDrawings.
        if data.get("active_dwg") is True or data.get("dwg") == "@active":
            data["dwg"] = await _detect_active_dwg_name()
        result = plant3d_query.list_components(project, data)
    elif operation == "list_valves":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.list_valves(project, data)
    elif operation == "list_instruments":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.list_instruments(project, data)
    elif operation == "bom":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.bom(project, data)
    elif operation == "pipe_length":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.pipe_length(project, data)
    elif operation == "weld_list":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.weld_list(project, data)
    elif operation == "bolt_gasket_list":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.bolt_gasket_list(project, data)
    elif operation == "list_equipment":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.list_equipment(project, data)
    elif operation == "get_component":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.get_component(project, data)
    elif operation == "find_missing_properties":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.find_missing_properties(project, data)
    elif operation == "export":
        project = data.get("project") or await _detect_open_project()
        result = plant3d_query.export(project, data)
    else:
        return _json({"error": f"Unknown plant3d operation: {operation}"})

    return _json(result)


async def _detect_open_project() -> str:
    """Resolve the Plant 3D project of the drawing currently open in AutoCAD.

    Reads the active drawing folder (DWGPREFIX) from the running session via
    the File IPC backend, then walks up to the project's Project.xml.
    """
    from autocad_mcp import plant3d_query

    backend = await get_backend()
    if backend.name != "file_ipc":
        raise RuntimeError(
            f"No hay AutoCAD abierto (backend actual: {backend.name}). "
            "Abre el proyecto Plant 3D en AutoCAD, o indica 'project' con la "
            "ruta o el nombre del proyecto."
        )

    res = await backend.drawing_get_variables(["DWGPREFIX"])
    if not res.ok:
        raise RuntimeError(f"No se pudo leer la ruta del dibujo activo: {res.error}")
    prefix = (res.payload or {}).get("DWGPREFIX")
    if not prefix:
        raise RuntimeError(
            "El dibujo activo no tiene ruta en disco (¿sin guardar?). "
            "Abre o guarda un dibujo del proyecto."
        )
    return str(plant3d_query.find_project_root(prefix))


async def _detect_active_dwg_name() -> str:
    """Return the basename of the drawing currently open in AutoCAD.

    Reads the ``DWGNAME`` system variable (e.g. ``"23099-PIP-MOD-0001_R9.dwg"``)
    via the File IPC backend; this basename matches ``PnPDrawings."Dwg Name"``
    so it can drive the ``dwg`` filter of ``list_components``. Requires AutoCAD
    open (file_ipc backend); on ezdxf/headless it raises a Spanish error.
    """
    backend = await get_backend()
    if backend.name != "file_ipc":
        raise RuntimeError(
            f"No hay AutoCAD abierto (backend actual: {backend.name}). "
            "Para filtrar por el DWG activo abre el dibujo en AutoCAD, o indica "
            "'dwg' con el nombre del archivo (p.ej. '23099-PIP-MOD-0001_R9.dwg')."
        )

    res = await backend.drawing_get_variables(["DWGNAME"])
    if not res.ok:
        raise RuntimeError(
            f"No se pudo leer el nombre del dibujo activo: {res.error}"
        )
    dwgname = (res.payload or {}).get("DWGNAME")
    if not dwgname:
        raise RuntimeError(
            "El dibujo activo no tiene nombre (¿sin guardar?). "
            "Guarda el dibujo o indica 'dwg' explícitamente."
        )
    return os.path.basename(str(dwgname))


_PLUGIN_REQUIRED_MSG = (
    "plant3d.locate requiere AutoCAD 2026 abierto con el plugin "
    "PlantMcpDispatch cargado (NETLOAD)."
)


async def _require_plant_plugin_backend():
    """Return the active backend, requiring file_ipc for plugin operations.

    plant3d.locate / plugin_status go through the .NET plugin over File IPC
    (not SQLite), so they need AutoCAD open with the plugin loaded. On any
    other backend (e.g. ezdxf/headless) raise a clear Spanish error.
    """
    backend = await get_backend()
    if backend.name != "file_ipc":
        raise RuntimeError(_PLUGIN_REQUIRED_MSG)
    return backend


async def _plant3d_locate(data: dict) -> ToolResult:
    """Locate Plant 3D objects in the drawing via the .NET plugin.

    Targets can be given directly as ``pnpids``/``pnpid`` or resolved from a
    ``tag`` (via ``PnPTagRegistry``) or a ``line`` number (via
    ``PipeRunComponent.LineNumberTag``), both looked up read-only in Piping.dcf.
    """
    backend = await _require_plant_plugin_backend()

    from autocad_mcp import plant3d_query

    # Normalize pnpids: accept a single 'pnpid' or a 'pnpids' list.
    pnpids = data.get("pnpids")
    if pnpids is None and data.get("pnpid") is not None:
        pnpids = [data["pnpid"]]
    if isinstance(pnpids, int):
        pnpids = [pnpids]

    # Si no hay pnpids explícitos, resuélvelos por tag o por línea (SQLite).
    # 'resolved_from' es informativo para la salida.
    resolved_from: dict | None = None
    project = data.get("project")
    if not pnpids and data.get("tag"):
        tag = str(data["tag"])
        project = project or await _detect_open_project()
        pnpids = plant3d_query.pnpids_for_tag(project, tag)
        if not pnpids:
            return _json({
                "ok": False,
                "error": (
                    f"No se encontró ningún objeto con el tag '{tag}' en el "
                    "proyecto."
                ),
            })
        resolved_from = {"by": "tag", "value": tag, "pnpids_resueltos": len(pnpids)}
    elif not pnpids and data.get("line"):
        line = str(data["line"])
        project = project or await _detect_open_project()
        pnpids = plant3d_query.pnpids_for_line(project, line)
        if not pnpids:
            return _json({
                "ok": False,
                "error": (
                    f"No se encontró ninguna línea '{line}' (sin componentes) "
                    "en el proyecto."
                ),
            })
        resolved_from = {"by": "line", "value": line, "pnpids_resueltos": len(pnpids)}

    if not pnpids:
        return _json({
            "ok": False,
            "error": (
                "plant3d.locate requiere 'pnpids' (lista de enteros) no vacía, "
                "o bien 'tag' o 'line' para resolverlos."
            ),
        })

    # Coerce/validate every element to int (the C# plugin uses int.TryParse,
    # so numeric strings are acceptable). Reject non-integer values early
    # instead of letting the plugin silently drop them.
    coerced: list[int] = []
    for p in pnpids:
        if isinstance(p, bool):  # bool is a subclass of int — not a valid PnPID
            return _json({
                "ok": False,
                "error": "plant3d.locate requiere que todos los 'pnpids' sean enteros.",
            })
        try:
            coerced.append(int(p))
        except (TypeError, ValueError):
            return _json({
                "ok": False,
                "error": "plant3d.locate requiere que todos los 'pnpids' sean enteros.",
            })
    pnpids = coerced

    zoom = data.get("zoom", True)
    select = data.get("select", True)
    isolate = bool(data.get("isolate", False))

    # Resolve each PnPID to its drawing handle(s) from the SQLite project DB,
    # so the plugin can grab objects by handle instead of relying on the
    # Plant 3D API. A pnpid with no row in PnPDataLinks simply yields no target.
    project = project or await _detect_open_project()
    targets = plant3d_query.resolve_handles(project, pnpids)

    result = await backend.plant_locate(pnpids, targets, zoom, select, isolate)
    out = result.to_dict()
    out["operation"] = "locate"
    if resolved_from is not None:
        out["resuelto_por"] = resolved_from
    return _json(out)


async def _plant3d_unisolate(data: dict) -> ToolResult:
    """Revert object isolation in the drawing via the .NET plugin (show all)."""
    backend = await _require_plant_plugin_backend()
    result = await backend.plant_unisolate()
    out = result.to_dict()
    out["operation"] = "unisolate"
    return _json(out)


async def _plant3d_plugin_status() -> ToolResult:
    """Ping the Plant 3D .NET plugin to verify it is loaded and responsive."""
    backend = await _require_plant_plugin_backend()
    result = await backend.plant_ping()
    out = result.to_dict()
    out["operation"] = "plugin_status"
    return _json(out)


async def _plant3d_pnid_probe(data: dict) -> ToolResult:
    """Run the P&ID diagnostic probe on the open drawing via the .NET plugin."""
    backend = await _require_plant_plugin_backend()
    limit = data.get("limit", 50)
    result = await backend.plant_pnid_probe(limit)
    out = result.to_dict()
    out["operation"] = "pnid_probe"
    return _json(out)


# ==========================================================================
# 10. specgen — Plant 3D spec authoring from a piping-class Excel
# ==========================================================================


@mcp.tool(annotations={"title": "Plant 3D Spec Authoring", "readOnlyHint": False})
@_safe("specgen")
async def specgen(
    operation: str,
    data: dict | None = None,
) -> ToolResult:
    """Genera una spec de AutoCAD Plant 3D (.pspc/.pspx) a partir de un piping class Excel.

    Empareja cada fila del piping class contra un directorio de catálogos (.pcat, SQLite,
    abiertos en solo lectura) con un modelo de confianza (ALTA/MEDIA/SUSTITUCION/BAJA) y
    materializa las piezas elegidas. Opcionalmente amplía los catálogos con las variantes
    de servicio de hidrógeno (-H2) deducidas del propio Excel. Nunca modifica los catálogos
    de entrada: la ampliación siempre trabaja sobre COPIAS dentro de 'out'.

    Operations:
      analyze — SOLO ANÁLISIS (no construye spec). Parsea el Excel, empareja contra los
                catálogos y devuelve la cobertura por nivel de confianza, los recuentos por
                familia (hoja) y la LISTA DE HUECOS (piezas sin match). Es lo que usa un
                ingeniero para "ver qué tal casa este piping class". Si se indica 'out',
                escribe además REVISION_MATCHING.xlsx y devuelve su ruta (único fichero que
                escribe). Con extend_h2=True empareja contra los catálogos -H2 ampliados
                (exige 'out'; escribe las copias en out/catalogs).
                data: {piping_class, catalogs, out?, extend_h2?}
                Devuelve {ok, coverage, by_family, gaps, review_xlsx, extend_h2?}.
      build   — PIPELINE COMPLETO: parsea, (amplía -H2), empareja, escribe
                REVISION_MATCHING.xlsx, construye <spec_name>.pspc + .pspx y ejecuta la
                verificación interna (integrity_check + consistencia de grafo + .pspx ZIP/XML
                válido). Devuelve las rutas de los ficheros generados, la cobertura y el
                resumen de verificación. 'ok' solo es True si la spec supera todas las
                comprobaciones de integridad.
                data: {piping_class, catalogs, out, spec_name?, extend_h2?, template_pspc?}
                (spec_name por defecto = nombre del piping class; template_pspc aporta la
                branch table vía su .pspx hermano.)
                Devuelve {ok, spec_name, files, components_built, coverage, verify,
                extend_h2?}.
      extend_catalog — Crea únicamente las variantes -H2 en copias de los catálogos bajo
                out/catalogs (no construye spec ni empareja para BOM). Devuelve las rutas,
                el nº de familias/filas creadas y los L-codes base cubiertos.
                data: {piping_class, catalogs, out}
                Devuelve {ok, out_dir, catalogs, families_created, rows_created,
                lcodes_covered, per_catalog, warnings}.
    """
    data = data or {}
    from autocad_mcp.specgen import api as specgen_api

    piping_class = data.get("piping_class")
    catalogs = data.get("catalogs")
    out = data.get("out")

    # --- Common parameter validation (Spanish, returned as JSON, never raised) ---
    if not piping_class:
        return _json({"error": "Falta el parámetro obligatorio 'piping_class'."})
    if not catalogs:
        return _json({"error": "Falta el parámetro obligatorio 'catalogs'."})
    if not os.path.isfile(piping_class):
        return _json({"error": f"No existe el piping class: {piping_class}"})
    if not os.path.isdir(catalogs):
        return _json({"error": f"No existe la carpeta de catálogos: {catalogs}"})

    if operation == "analyze":
        result = specgen_api.analyze(
            piping_class=piping_class,
            catalogs_dir=catalogs,
            out_dir=out,
            extend_h2=bool(data.get("extend_h2")),
        )
        return _json(result)

    if operation == "build":
        if not out:
            return _json({"error": "Falta el parámetro obligatorio 'out' (carpeta de salida)."})
        result = specgen_api.build(
            piping_class=piping_class,
            catalogs_dir=catalogs,
            out_dir=out,
            spec_name=data.get("spec_name"),
            extend_h2=bool(data.get("extend_h2")),
            template_pspc=data.get("template_pspc"),
        )
        return _json(result)

    if operation == "extend_catalog":
        if not out:
            return _json({"error": "Falta el parámetro obligatorio 'out' (carpeta de salida)."})
        result = specgen_api.extend_catalog(
            piping_class=piping_class,
            catalogs_dir=catalogs,
            out_dir=out,
        )
        return _json(result)

    return _json({"error": f"Unknown specgen operation: {operation}"})


# ==========================================================================
# Main entry point
# ==========================================================================


def main():
    """Run the MCP server on stdio transport."""
    import logging
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    log.info("autocad_mcp_starting", version="3.1.0")
    mcp.run(transport="stdio")
