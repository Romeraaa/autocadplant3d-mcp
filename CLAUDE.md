# AutoCAD MCP — CLAUDE.md

## Proyecto

Servidor MCP (Model Context Protocol) v3.1 que permite a Claude controlar AutoCAD 2026 mediante
File IPC sin robar el foco de ventana. Desarrollado para IDEA IT (Ingeniería y Diseño Estructural
Avanzado, S.L.).

Comunica siempre en **español** con el usuario.

---

## Modo de trabajo: Orquestador por defecto

En este proyecto operas SIEMPRE como **orquestador**, sin necesidad de invocar `/orquestar`. Coordinas y delegas; **no escribes ficheros tú mismo**.

- **Tú (sesión principal) NO escribes** código, documentación, tests ni ningún fichero. Un hook (`.claude/hooks/block_orchestrator_writes.ps1`) lo bloquea físicamente. Delega toda escritura en el subagente adecuado mediante la herramienta Task/Agent.
- **Sí actúas directamente** para leer, investigar, analizar, planificar y responder. Para preguntas o exploración NO lances agentes: hazlo tú. Delega solo cuando haya que **crear o modificar** algo.

### Subagentes
| Agente | Para qué |
|---|---|
| `mcp-python-dev` | Código Python del servidor MCP (`server.py`, `plant3d_query.py`, `backends/`, `config.py`) |
| `sqlite-analyst` | Investigar esquemas `.dcf` y diseñar/validar SQL (solo lectura) |
| `lisp-dev` | AutoLISP en `lisp-code/` |
| `dotnet-plugin-dev` | Plugin C# de Plant 3D |
| `test-runner` | Tests `pytest` |
| `docs-writer` | Documentación y memoria (en español) |
| `code-reviewer` | Revisión read-only del diff |

### Protocolo
1. Descompón la petición y reúne contexto (leyendo tú mismo).
2. Delega con briefing completo + criterio de "hecho" (los agentes no ven la conversación).
3. Encadena: flujo típico de Plant 3D → `sqlite-analyst` → `mcp-python-dev` → `test-runner` → `code-reviewer`.
4. Cierre: todo cambio de código pasa por `code-reviewer`; si toca lógica, también por `test-runner`. En hitos, `docs-writer` actualiza docs/memoria.
5. Sintetiza y reporta conciso; no vuelques los informes crudos.

Inyecta en cada briefing las REGLAS CRÍTICAS de la sección siguiente. Los subagentes y la skill se cargan al iniciar sesión (si se crean a media sesión, requieren reiniciar Claude Code). Escape manual (solo mantenimiento del harness): crear el fichero `.claude/ALLOW_ORCHESTRATOR_WRITES`.

---

## Arquitectura

```
Claude (MCP client)
    ↓ tool call
Python server  →  escribe  →  C:/temp/autocad_mcp_cmd_{id}.json
    ↓
Python  →  envía WM_CHAR "(c:mcp-dispatch)" + Enter  →  AutoCAD 2026
                                                              ↓
                                          mcp_dispatch.lsp  lee el JSON
                                          ejecuta la operación AutoCAD
                                          escribe resultado
                                              ↓
                                     C:/temp/autocad_mcp_result_{id}.json
    ↓
Python  ←  poll 100ms  ←  lee resultado
    ↓
Claude recibe respuesta MCP
```

**Lock único:** `asyncio.Lock()` en `file_ipc.py` — solo un comando en vuelo a la vez.

**Backend activo:** `file_ipc` cuando AutoCAD está abierto con un .dwg. Fallback a `ezdxf` (headless).

---

## REGLAS CRÍTICAS — NUNCA IGNORAR

### 1. NUNCA usar `execute_lisp`
```python
# ❌ PROHIBIDO — abre el diálogo APPLOAD en AutoCAD 2026 y bloquea el hilo
system(operation="execute_lisp", data={"code": "..."})

# ✅ CORRECTO — usar las operaciones MCP nativas
layer(operation="create", data={"name": "MUROS", "color": "1"})
entity(operation="create_line", x1=0, y1=0, x2=10, y2=0)
```

### 2. Color de capa como STRING, no entero
```python
# ❌ INCORRECTO — la función mcp-json-get-string no parsea enteros
layer(operation="create", data={"name": "MUROS", "color": 1})

# ✅ CORRECTO
layer(operation="create", data={"name": "MUROS", "color": "1"})

# Códigos ACI: "1"=rojo, "2"=amarillo, "3"=verde, "4"=cian, "5"=azul, "6"=magenta, "7"=blanco
```

### 3. Comando LAYER siempre con guión
```lisp
; ❌ INCORRECTO — abre el diálogo de capas y bloquea
(command "LAYER" ...)

; ✅ CORRECTO — versión línea de comandos
(command "_.-LAYER" "_NEW" nombre "_COLOR" color nombre "_LTYPE" linetype nombre "")
```

---

## Los 8 Tools MCP

### `drawing` — Gestión del dibujo
`create` · `open` · `info` · `save` · `save_as_dxf` · `plot_pdf` · `purge` · `get_variables` · `undo` · `redo`

### `entity` — Entidades
Crear: `create_line` · `create_circle` · `create_polyline` · `create_rectangle` · `create_arc` · `create_ellipse` · `create_mtext` · `create_hatch`
Leer: `list` · `count` · `get`
Modificar: `copy` · `move` · `rotate` · `scale` · `mirror` · `offset` · `array` · `fillet` · `chamfer` · `erase`

### `layer` — Capas
`list` · `create` · `set_current` · `set_properties` · `freeze` · `thaw` · `lock` · `unlock`

### `block` — Bloques
`list` · `insert` · `insert_with_attributes` · `get_attributes` · `update_attribute` · `define`

### `annotation` — Cotas y textos
`create_text` · `create_dimension_linear` · `create_dimension_aligned` · `create_dimension_angular` · `create_dimension_radius` · `create_leader`

### `pid` — Diagramas P&ID (librería CTO)
`setup_layers` · `insert_symbol` · `list_symbols` · `draw_process_line` · `connect_equipment` · `add_flow_arrow` · `add_equipment_tag` · `add_line_number` · `insert_valve` · `insert_instrument` · `insert_pump` · `insert_tank`

### `view` — Viewport
`zoom_extents` · `zoom_window` · `get_screenshot`

### `system` — Estado del servidor
`status` · `health` · `get_backend` · `runtime` · `init`
⚠️ `execute_lisp` está documentado pero NO debe usarse (ver regla 1).

### `plant3d` — Consulta de proyectos Plant 3D (solo lectura)
`detect_project` · `line_summary` · `list_projects` · `find_untagged` · `validate_specs`
Lee directamente las bases SQLite (`.dcf`) del proyecto — **no requiere el plugin .NET**
y nunca modifica el proyecto (apertura `mode=ro`).
- `find_untagged` — lista los componentes de tubería SIN número de línea válido
  (`LineNumberTag` NULL, vacío o `?`), con desglose por clase (`PartCategory`) y por spec.
  Identifica cada componente por `PnPID` + propiedades; **no lo localiza en el dibujo**
  (no hay handle/GUID en el SQLite — eso requeriría el plugin .NET).
- `validate_specs` — valida coherencia de especificaciones cruzando `Piping.dcf` con los
  catálogos `Spec Sheets\*.pspc` (también SQLite). Cuatro comprobaciones: (1) Spec ≠ Required
  Spec de la línea; (2) specs mezcladas dentro de un mismo `LineNumberTag`; (3) spec vacía/NULL;
  (4) spec fantasma (usada en el proyecto pero sin fichero `.pspc` en el catálogo) y
  material/schedule fuera de catálogo. Degrada con gracia si no existe la carpeta `Spec Sheets`
  o un `.pspc` es ilegible. Parámetros: `data["ignore_specs"]` (lista de specs auxiliares a
  excluir) y `data["limit"]` (acota la salida). Identifica componentes por `PnPID` + propiedades.
**Por defecto consulta el proyecto que el usuario tiene abierto en AutoCAD:** si no se pasa
`project`, lee `DWGPREFIX` del dibujo activo (vía backend File IPC) y sube hasta el `Project.xml`.
También admite `project` explícito (ruta a la carpeta o, con `AUTOCAD_MCP_PLANT3D_ROOT`, el nombre).

---

## Archivos clave

| Archivo | Rol |
|---------|-----|
| `src/autocad_mcp/server.py` | 9 tools MCP con dispatch de operaciones |
| `src/autocad_mcp/backends/file_ipc.py` | Backend IPC con AutoCAD |
| `src/autocad_mcp/plant3d_query.py` | Consultas de solo lectura sobre los `.dcf` (SQLite) de Plant 3D |
| `src/autocad_mcp/config.py` | Variables de entorno y detección de backend |
| `lisp-code/mcp_dispatch.lsp` | Dispatcher LISP (debe cargarse en AutoCAD) |
| `lisp-code/attribute_tools.lsp` | Herramientas de atributos (debe cargarse) |
| `.mcp.json` | Config del servidor MCP para Claude Code |

---

## Setup de desarrollo

```powershell
# Activar entorno virtual
.\.venv\Scripts\Activate.ps1

# Instalar dependencias
pip install -e ".[dev]"

# Variables de entorno relevantes
$env:AUTOCAD_MCP_BACKEND = "auto"      # auto | file_ipc | ezdxf
$env:AUTOCAD_MCP_IPC_DIR = "C:/temp"
$env:AUTOCAD_MCP_IPC_TIMEOUT = "15"
```

**AutoCAD 2026 debe tener cargados los dos LISP antes de usar los tools:**
1. Cargar `lisp-code/mcp_dispatch.lsp`
2. Cargar `lisp-code/attribute_tools.lsp`

---

## Trabajo pendiente

### Consulta de datos Plant 3D (vía SQLite) — EN CURSO
**Hallazgo clave:** los `.dcf` de un proyecto Plant 3D (Piping.dcf, ProcessPower.dcf...) son
bases **SQLite**. La parte de CONSULTA/lectura de las herramientas Plant 3D ya **no depende**
del plugin .NET ni de AutoCAD abierto: se lee el SQLite con el módulo `sqlite3` de Python.

- Tablas clave en `Piping.dcf`: `PipeRunComponent` (LineNumberTag, Service, Required Spec,
  SpoolNumber...), `EngineeringItems` (Spec, NominalDiameter, Material, Schedule...), unidas por `PnPID`.
- Proyectos de prueba en `\\172.16.0.220\Comun\06-INFORMÁTICA\3_UTILIDADES\MCP-Plant3D\Proyectos`.
- Implementado: `plant3d.detect_project` · `plant3d.line_summary` · `plant3d.list_projects` ·
  `plant3d.find_untagged` (componentes sin `LineNumberTag` válido; implementada y testeada 2026-06-20) ·
  `plant3d.validate_specs` (validación de coherencia de especificaciones; implementada, testeada y
  commiteada 2026-06-22, commit `f4ecdab`). **Hallazgo:** los catálogos de specs viven en
  `Spec Sheets\*.pspc`, que también son SQLite — accesibles sin plugin .NET.
  Detección del proyecto abierto: lee `DWGPREFIX` del dibujo activo y sube hasta `Project.xml`.
- Las dos herramientas de solo lectura del trío original ya están implementadas vía SQLite.
  **Fase actual: SOLO CONSULTA (decisión 2026-06-22).** La escritura y el plugin .NET quedan aplazados; ver sección siguiente.

### Plugin .NET para Plant 3D — APLAZADO (fuera del alcance actual)

> **Decisión 2026-06-22:** el proyecto se centra ÚNICAMENTE en herramientas de consulta (solo
> lectura). El plugin .NET **no se desarrolla por ahora**; se retomará si/cuando se aborde
> escritura en la sesión viva de AutoCAD.

Plugin C# (`plant3d-plugin/PlantMcpDispatch.dll`) con APIs de Plant 3D (`Autodesk.ProcessPower.*`).
Solo necesario para operaciones que **escriban** en la sesión viva (p.ej. asignar capas de verdad)
o que necesiten datos no disponibles en el SQLite (handles para localizar objetos en el dibujo,
datos accesibles únicamente vía `DataLinksManager`). AutoLISP no puede acceder a estas APIs.

**Estado conservado para cuando se retome:**
- Arquitectura decidida, entorno verificado (net8.0-windows, VS Code + .NET SDK 9).
- Bloqueado por: DWG de prueba de Plant 3D (pendiente de recibir de la organización).
- DLLs disponibles en `C:\Program Files\Autodesk\AutoCAD 2026\PLNT3D\`.

Las 3 herramientas originalmente previstas:
- `plant3d-find-untagged` — ✅ IMPLEMENTADA vía SQLite (2026-06-20, `plant3d.find_untagged`)
- `plant3d-validate-specs` — ✅ IMPLEMENTADA vía SQLite (2026-06-22, `plant3d.validate_specs`; catálogos `Spec Sheets\*.pspc` también SQLite)
- `plant3d-assign-layers-by-property` — escritura → requiere plugin .NET → **APLAZADA / fuera del alcance actual**

### Herramientas estructurales (HERRAMIENTAS_PROPUESTAS.md)
10 herramientas propuestas para flujos de ingeniería estructural. Prioridad actual:
⭐⭐⭐ Plantillas de capas · Cajetín · Cuadro de superficies
