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
| Agente | Para qué | Modelo |
|---|---|---|
| `mcp-python-dev` | Código Python del servidor MCP (`server.py`, `plant3d_query.py`, `backends/`, `config.py`) | opus |
| `sqlite-analyst` | Investigar esquemas `.dcf` y diseñar/validar SQL (solo lectura) | sonnet |
| `lisp-dev` | AutoLISP en `lisp-code/` | opus |
| `dotnet-plugin-dev` | Plugin C# de Plant 3D | opus |
| `test-runner` | Tests `pytest` | sonnet |
| `docs-writer` | Documentación y memoria (en español) | sonnet |
| `code-reviewer` | Revisión read-only del diff | sonnet |

### Protocolo
1. Descompón la petición y reúne contexto (leyendo tú mismo).
2. Delega con briefing **mínimo suficiente** + criterio de "hecho" (los agentes no ven la conversación).
3. Encadena solo lo necesario (ver "Calibrar el esfuerzo").
4. Cierre: el código que toca lógica pasa por `test-runner` y, antes de reportar, por `code-reviewer`. En hitos, `docs-writer` actualiza docs/memoria.
5. Sintetiza y reporta conciso; no vuelques los informes crudos.

### Calibrar el esfuerzo (eficiencia de tokens)
El coste está dominado por el número de subagentes y por cuánto contexto recarga cada uno. Ajusta la cadena al tamaño real de la tarea:
- **Cambio trivial** (1 fichero, mecánico: un comentario, un literal, un test aislado, renombrar): **un solo agente**, sin cadena. Sáltate `code-reviewer` si no toca lógica.
- **Cambio normal** (una operación, un fix con su test): el agente que escribe (`mcp-python-dev`, etc.) puede **escribir código y sus tests en la misma pasada**; luego `code-reviewer`. No metas `test-runner` aparte salvo que quieras verificación independiente o haya muchos tests.
- **Feature multi-parte / con esquema SQLite nuevo**: cadena completa `sqlite-analyst` → `mcp-python-dev` → `test-runner` → `code-reviewer`.

**Disciplina de contexto (lo que más ahorra).** El coste está dominado por la *caché de contexto re-leída en cada turno*, no por lo que se escribe (medido: ~86% del gasto es cache read/write). Por tanto:
- **Corta la sesión en cada hito.** Al cerrar un hito (commit + memoria actualizada), usa `/clear` o `/compact`. El coste de una sesión crece con (tamaño de contexto × nº de turnos); las sesiones largas que arrastran contexto enorme son el mayor sumidero.
- **No cargues ficheros grandes en la sesión principal.** Leer `server.py` entero en la principal lo mete en caché y se re-lee en CADA turno posterior. Léelo por rango `file:línea` o delega la lectura en un subagente (muere y se lleva ese contexto).

### Briefings baratos (inyéctalo a cada subagente)
- **No re-leas ficheros ya citados** en el briefing; si das tú el fragmento relevante, que no vuelva a abrir el fichero entero.
- Lee por **rango `file:línea`**, no ficheros completos (`server.py` es grande).
- Devuelve resultado **conciso/estructurado** (qué tocaste, fichero:línea, qué falta), **sin narración**.
- Inyecta las **REGLAS CRÍTICAS** de la sección siguiente.

Los subagentes y la skill se cargan al iniciar sesión (si se crean a media sesión, requieren reiniciar Claude Code). Escape manual (solo mantenimiento del harness): crear el fichero `.claude/ALLOW_ORCHESTRATOR_WRITES`.

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

## Los 10 Tools MCP

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

### `plant3d` — Consulta de proyectos Plant 3D
`detect_project` · `line_summary` · `list_projects` · `find_untagged` · `validate_specs` · `list_lines` · `list_components` · `list_valves` · `list_instruments` · `bom` · `pipe_length` · `weld_list` · `bolt_gasket_list` · `locate` · `plugin_status`

- **La mayoría** lee directamente las bases SQLite (`.dcf`) del proyecto — sin plugin .NET, apertura `mode=ro`, nunca modifica el proyecto. Ninguna localiza el objeto en el dibujo (no hay handle/GUID en el SQLite).
- **Excepción — `locate` y `plugin_status`:** van por el **plugin .NET `PlantMcpDispatch` vía File IPC** (canal propio, trigger `MCPPLANTDISPATCH`). Requieren AutoCAD 2026 abierto con el plugin cargado (NETLOAD); sobre `ezdxf`/headless devuelven error en español. `locate` es la **única que sí localiza objetos en el dibujo**.
- **Por defecto** opera sobre el proyecto abierto en AutoCAD: sin `project`, lee `DWGPREFIX` del dibujo activo y sube hasta `Project.xml`. Admite `project` explícito.

📖 **Detalle por operación (parámetros, salida, esquema) y arquitectura del plugin .NET → `docs/plant3d-tools.md`.** Léelo cuando necesites el comportamiento exacto de una operación.

### `specgen` — Generación de specs/catálogos Plant 3D
Genera ficheros de especificación Plant 3D (`.pspc`/`.pspx`) a partir de una piping class en Excel.
- `analyze` — solo lectura: cobertura por nivel de confianza + huecos sin cubrir.
- `build` — genera `.pspc`/`.pspx`, informe `REVISION_MATCHING.xlsx` y, opcionalmente, catálogos H2 ampliados (`--extend-h2`).
- `extend_catalog` — amplía un catálogo `.pcat` con variantes `-H2` clonando familias base.

Paquete en `src/autocad_mcp/specgen/`; capa `api.py` compartida con la CLI (`python -m autocad_mcp.specgen`). Depende de un catálogo `.pcat` con las piezas; no requiere API .NET ni AutoCAD abierto.

---

## Archivos clave

| Archivo | Rol |
|---------|-----|
| `src/autocad_mcp/server.py` | 10 tools MCP con dispatch de operaciones |
| `src/autocad_mcp/backends/file_ipc.py` | Backend IPC con AutoCAD (canal LISP + canal plugin .NET) |
| `src/autocad_mcp/plant3d_query.py` | Consultas de solo lectura sobre los `.dcf` (SQLite) de Plant 3D |
| `src/autocad_mcp/config.py` | Variables de entorno y detección de backend |
| `lisp-code/mcp_dispatch.lsp` | Dispatcher LISP (debe cargarse en AutoCAD) |
| `lisp-code/attribute_tools.lsp` | Herramientas de atributos (debe cargarse) |
| `src/autocad_mcp/specgen/` | Paquete specgen: generación de specs/catálogos Plant 3D desde piping class Excel |
| `plant3d-plugin/` | Plugin C# `PlantMcpDispatch` (locate/plugin_status); ver `docs/plant3d-tools.md` |
| `docs/plant3d-tools.md` | Referencia detallada del tool `plant3d` y del plugin .NET |
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

> **Hallazgo base:** los `.dcf` y `.pspc` de Plant 3D son bases **SQLite** — la CONSULTA no depende
> del plugin .NET ni de AutoCAD abierto (módulo `sqlite3`). El historial detallado de cada operación
> (fechas, conteos de tests, commits, cifras validadas) vive en la **memoria del proyecto**; el detalle
> técnico de cada operación, en **`docs/plant3d-tools.md`**.

- **Consulta SQLite:** todas las operaciones implementadas (`detect_project` … `bolt_gasket_list`).
- **Plugin .NET — RETOMADO PARCIALMENTE (2026-06-25, commit `b1897a5`):** `locate` y `plugin_status`
  implementados y testeados a nivel unitario (suite 1092 verde). **`locate` PENDIENTE DE VALIDACION
  en AutoCAD vivo** — firmas de API descubiertas con el `probe`, bloqueado por DWG de prueba
  multi-modelo (pendiente de la organización).
- **Escritura en Plant 3D** (p.ej. `assign-layers-by-property`): requiere plugin .NET → **PENDIENTE / no abordada aún**.
- **Herramientas estructurales** (`HERRAMIENTAS_PROPUESTAS.md`): 10 propuestas; prioridad actual
  ⭐⭐⭐ Plantillas de capas · Cajetín · Cuadro de superficies.
