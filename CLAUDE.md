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
`detect_project` · `line_summary` · `list_projects` · `find_untagged` · `validate_specs` · `list_lines` · `list_components` · `list_valves` · `list_instruments` · `bom` · `pipe_length` · `weld_list` · `bolt_gasket_list`
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
- `list_lines` — genera la LINE LIST del proyecto: una fila por número de línea válido
  (`LineNumberTag` no NULL/vacío/`?`). Estrategia híbrida: propiedades de línea (Service,
  NominalSpec, NominalSize, aislamiento) desde la tabla cabecera `P3dLineGroup` (casada por Tag
  normalizado TRIM+UPPER); specs reales y diámetros agregados desde `EngineeringItems`; DWG del
  modelo 3D donde vive la línea desde `P3dDrawingLineGroupRelationship` → `PnPDrawings`. Los
  tamaños se mantienen separados por unidad (in/mm) sin colapsar a rango. Robusto frente a
  variaciones de esquema (usa `PRAGMA table_info`; degrada con gracia si faltan columnas
  opcionales o tablas de relación). Parámetros: `data["ignore_specs"]` y `data["limit"]` (default
  50, 0 = sin tope). **No disponible vía SQLite:** P&ID de origen y localización física del objeto
  en el dibujo (requieren plugin .NET).
- `list_components` — lista genérica de componentes de tubería (`PipeRunComponent` ⨝
  `EngineeringItems` por `PnPID`) con filtros opcionales: `classes` (mapeo canónico: `pipe`,
  `valve`, `fitting`, `flange`, `instrument`, `support`; valor no canónico = passthrough literal
  de `PartCategory`; omitido = todas), `line` (por `LineNumberTag`, normalizado TRIM+UPPER),
  `spec` (por `EngineeringItems.Spec`) y `size` (`{"value", "unit"}` — exige unidad). Parámetro
  `limit` (default 50, 0 = sin tope; reporta `omitted`, sin truncado silencioso). Salida:
  `{ok, project, path, limit, filters, count, omitted, by_class, components, notes}`. Cada
  componente: `pnpid`, `class`, `tag` (saneado; NULL/'?'/'?-?' → None), `description`, `spec`,
  `size`, `line`. **No localiza el objeto en el dibujo** (sin handle/GUID en SQLite).
- `list_valves` — preset de solo lectura de `list_components` con la clase fijada a válvula
  (`classes=["valve"]`). Cualquier `classes` que pase el usuario se ignora; se conservan los
  filtros restantes: `line`, `spec`, `size` (`{"value", "unit"}`), `limit` (default 50, 0 = sin
  tope). Salida idéntica a `list_components`: `{ok, project, path, limit, filters, count,
  omitted, by_class, components, notes}`. **No localiza el objeto en el dibujo** (sin
  handle/GUID en SQLite).
- `list_instruments` — preset de solo lectura de `list_components` con la clase fijada a
  instrumento (`classes=["instrument"]`, mapea a `PartCategory="Instruments"`). Cualquier
  `classes` que pase el usuario se ignora; se conservan los filtros restantes: `line`, `spec`,
  `size` (`{"value", "unit"}`), `limit` (default 50, 0 = sin tope). Salida idéntica a
  `list_components`. **No localiza el objeto en el dibujo** (sin handle/GUID en SQLite).
- `bom` — genera el Bill of Materials del proyecto. Solo lectura; NO emite SQL propio: agrega
  internamente la salida de `list_components` (`PipeRunComponent` ⨝ `EngineeringItems`).
  Agrupa los componentes por la tupla **(clase, spec, tamaño, descripción)** — cada combinación
  distinta es una línea de BOM con su `quantity` (recuento de componentes; no mide longitudes).
  Admite los mismos filtros de alcance que `list_components`: `classes`, `line`, `spec`,
  `size` (`{"value", "unit"}`), y `limit` (default 50, 0 = sin tope) que acota el número de
  LÍNEAS de BOM, no de componentes individuales. Clase None/vacía se etiqueta `"(sin clase)"`;
  spec/size/description None se conservan como None. Salida: `{ok, project, path, limit,
  filters, total_components, line_count, omitted, by_class, bom, notes}`; cada línea de BOM:
  `{class, spec, size, description, quantity}`. No aplica la limitación de localización en el
  dibujo (un BOM no localiza objetos).
- `pipe_length` — genera el sumatorio de longitudes reales de tubería del proyecto. Solo
  lectura; consulta directamente la tabla **`Pipe`** de `Piping.dcf` (columna `Length`), casada
  por `PnPID` con `EngineeringItems` y `PipeRunComponent`. Solo aplica a
  `PartCategory='Pipe'` — no acumula dimensiones físicas de válvulas, fittings ni
  instrumentos. La unidad de longitud se lee de `EngineeringItems.LengthUnit` (típicamente
  `'mm'`), nunca se asume; es ortogonal a `NominalUnit` (diámetro). No mezcla longitudes de
  distinta unidad. Parámetro `group_by` (`"line"` por defecto | `"spec"` | `"size"`) y
  filtros de alcance idénticos a `list_components`: `line`, `spec`, `size` (`{"value","unit"}`
  — exige unidad), `limit` (default 50, 0 = sin tope; acota número de GRUPOS, reporta
  `omitted` sin truncado silencioso). Tramos sin línea válida (`LineNumberTag`
  NULL/`''`/`'?'`) se reportan SIEMPRE en el campo `untagged` `{pipe_count, length}` y
  además como grupo `"(SIN LÍNEA)"` cuando `group_by="line"`. Salida: `{ok, project, path,
  limit, group_by, filters, length_unit, total_pipe_count, total_length, untagged,
  group_count, omitted, groups, notes}`; cada grupo: `{group, pipe_count, length,
  length_unit}`. Longitudes redondeadas a 2 decimales. Robusto a variaciones de esquema
  (PRAGMA): degrada con gracia (ok:True, groups vacíos, totales 0, nota) si falta la tabla
  `Pipe`, la columna `Length` o `LengthUnit`. **No localiza el objeto en el dibujo** (sin
  handle/GUID en SQLite — requeriría plugin .NET).
- `weld_list` — recuento y desglose de soldaduras del proyecto. Solo lectura; consulta las
  tres tablas dedicadas de `Piping.dcf`: **`Buttweld`**, **`Socketweld`** y **`TapWeld`**
  (el subtipo — butt/socket/tap — deriva de la tabla de origen). Cada soldadura se casa por
  `PnPID` con `EngineeringItems` (diámetro/spec) y con `PipeRunComponent`; la línea se
  resuelve vía `P3dLineGroupPartRelationship` → `P3dLineGroup.Tag` (~97% de cobertura).
  El campo `Shop_Field` está poblado (SHOP/FIELD) → desglose taller vs. campo siempre
  presente. `WeldNumber` es 100% NULL (numeración isométrica aún no asignada) → la
  herramienta CUENTA y DESGLOSA, NO numera. Parámetros (`data`): `group_by` (`"line"` por
  defecto | `"size"` | `"spec"` | `"shop_field"` | `"type"`); filtros `line`, `spec`,
  `size` (`{"value","unit"}` — exige unidad), `shop_field` (`"shop"` | `"field"`),
  `weld_type` (`"butt"` | `"socket"` | `"tap"`); `limit` (default 50, 0 = sin tope; acota
  el número de GRUPOS, reporta `omitted`). Salida: `{ok, project, path, limit, group_by,
  filters, total_welds, by_type[], by_shop_field[], untagged{weld_count}, group_count,
  omitted, groups[], notes}`. `by_type` y `by_shop_field` son desgloses globales siempre
  presentes (orden descendente). Soldaduras sin línea válida se reportan en `untagged` y,
  cuando `group_by="line"`, además como grupo `"(SIN LÍNEA)"`. Robusto a variaciones de
  esquema (PRAGMA): degrada con gracia (ok:True, total 0, listas vacías, nota) si faltan
  las tres tablas, la columna `Shop_Field` o las tablas/columnas de relación de línea.
  **No localiza el objeto en el dibujo** (sin handle/GUID en SQLite — requeriría plugin .NET).
- `bolt_gasket_list` — lista y recuento de pernos y juntas (material de montaje de bridas) del
  proyecto. Solo lectura; consulta DOS tablas dedicadas de `Piping.dcf`: **`BoltSet`** (conjuntos
  de pernos; columnas `BoltSize`, `NumberInSet`, `BoltCompatibleStd`, `Shop_Field`) y **`Gasket`**
  (juntas). IGNORA `Fasteners` (superconjunto genérico no fiable). Cada elemento se casa por
  `PnPID` con `EngineeringItems` (Spec, NominalDiameter/NominalUnit — diámetro de brida en in/mm
  sin colapsar, Material); la línea se resuelve vía `P3dLineGroupPartRelationship` →
  `P3dLineGroup.Tag` (1:1; ~80% de cobertura; el ~20% sin línea es legítimo — bridas en cabeza de
  ramal sin asignación de línea). Doble métrica de cantidad: `item_count` (filas: sets + juntas),
  `bolt_sets`, `individual_bolts` (Σ `NumberInSet`, parseado de texto y expuesto como int;
  `NumberInSet` no numérico → contribuye 0 con nota), `gaskets` (cada junta = 1). Parámetros
  (`data`): `group_by` (`"line"` por defecto | `"size"` | `"spec"` | `"material"` |
  `"item_type"` | `"shop_field"` | `"bolt_size"`); filtros `item_type` (`"bolt"` | `"gasket"`),
  `line`, `spec`, `size` (`{"value","unit"}` — exige unidad), `shop_field` (`"shop"` | `"field"`);
  `limit` (default 50, 0 = sin tope; acota el número de GRUPOS, reporta `omitted`). Salida:
  `{ok, project, path, limit, group_by, filters, totals{item_count, bolt_sets, individual_bolts,
  gaskets}, by_item_type[], by_shop_field[], untagged{...}, group_count, omitted, groups[],
  notes}`. `by_item_type` y `by_shop_field` son desgloses globales siempre presentes. Items sin
  línea válida se reportan en `untagged` y, cuando `group_by="line"`, también como grupo
  `"(SIN LÍNEA)"`. Robusto a variaciones de esquema (PRAGMA): degrada con gracia (ok:True,
  totales 0, listas vacías, nota) si faltan las tablas, columnas opcionales (`NumberInSet` /
  `BoltSize` / `Shop_Field`) o las tablas/columnas de relación de línea. **No localiza el objeto
  en el dibujo** (sin handle/GUID en SQLite — requeriría plugin .NET).
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
  **Hallazgo adicional:** `P3dLineGroup` — tabla cabecera con una fila por número de línea; recoge
  Service, NominalSpec, NominalSize, aislamiento y otros atributos de la línea. Su esquema VARÍA
  por proyecto (se usa `PRAGMA table_info` para seleccionar solo columnas presentes).
- Proyectos de prueba en `\\172.16.0.220\Comun\06-INFORMÁTICA\3_UTILIDADES\MCP-Plant3D\Proyectos`.
- Implementado: `plant3d.detect_project` · `plant3d.line_summary` · `plant3d.list_projects` ·
  `plant3d.find_untagged` (componentes sin `LineNumberTag` válido; implementada y testeada 2026-06-20) ·
  `plant3d.validate_specs` (validación de coherencia de especificaciones; implementada, testeada y
  commiteada 2026-06-22, commit `f4ecdab`). **Hallazgo:** los catálogos de specs viven en
  `Spec Sheets\*.pspc`, que también son SQLite — accesibles sin plugin .NET. ·
  `plant3d.list_lines` (LINE LIST completa; estrategia híbrida con `P3dLineGroup` + `EngineeringItems`
  + relación de DWG; implementada, testeada — ~98 tests nuevos, suite total 306 verdes — commiteada
  2026-06-22, commit `6c40dee`; validado: AIR LIQUIDE HUELVA = 114 líneas). ·
  `plant3d.list_components` (lista genérica de componentes con filtros por clase/línea/spec/size;
  implementada y testeada 2026-06-22, 96 tests nuevos, suite total 402 verdes; validado:
  AIR LIQUIDE HUELVA = 4666 componentes, `classes=["valve"]` → 357; commit `31ab90c`). ·
  `plant3d.list_valves` (preset de `list_components` con `classes=["valve"]` fijado; implementada
  y testeada 2026-06-23, 52 tests nuevos, suite total 454 verdes; commit pendiente). ·
  `plant3d.list_instruments` (preset de `list_components` con `classes=["instrument"]` fijado;
  implementada y testeada 2026-06-23, 52 tests nuevos, suite total 506 verdes; commit pendiente). ·
  `plant3d.bom` (Bill of Materials — agregación de `list_components` por la tupla clase/spec/tamaño/descripción con `quantity` por recuento; implementada y testeada 2026-06-23, 72 tests nuevos, suite total 578 verdes; apta para commit). ·
  `plant3d.pipe_length` (sumatorio de longitudes reales de tubería — tabla `Pipe`, columna `Length`,
  unidad desde `LengthUnit`; group_by line/spec/size; tramos sin línea en campo `untagged` y grupo
  `"(SIN LÍNEA)"`; implementada y testeada 2026-06-24, 101 tests nuevos, suite total 679 verdes,
  test de integración real incluido; validado: AIR LIQUIDE HUELVA = 1679 tramos ≈ 1.635.082 mm,
  con línea válida 1300 tramos ≈ 1.333.000 mm, untagged 379 tramos ≈ 302.000 mm; commit pendiente). ·
  `plant3d.weld_list` (recuento y desglose de soldaduras — tablas `Buttweld`/`Socketweld`/`TapWeld`;
  subtipo desde tabla de origen; línea vía `P3dLineGroupPartRelationship` → `P3dLineGroup.Tag`,
  ~97% cobertura; `Shop_Field` poblado (SHOP/FIELD), `WeldNumber` 100% NULL; group_by
  line/size/spec/shop_field/type; filtros line/spec/size/shop_field/weld_type; soldaduras sin
  línea en `untagged` y grupo `"(SIN LÍNEA)"`; implementada, testeada y revisada 2026-06-24,
  162 tests nuevos, suite total 841 verdes, test de integración real incluido; validado:
  AIR LIQUIDE HUELVA = 2953 soldaduras — Buttweld 2382 / Socketweld 365 / TapWeld 206, ~97%
  con línea válida; commit pendiente). ·
  `plant3d.bolt_gasket_list` (lista y recuento de pernos y juntas — tablas `BoltSet` y `Gasket`
  en `Piping.dcf`; IGNORA `Fasteners`; cada elemento casado por `PnPID` con `EngineeringItems`
  (Spec, NominalDiameter/NominalUnit); línea vía `P3dLineGroupPartRelationship` → `P3dLineGroup.Tag`
  (~80% cobertura; ~20% sin línea legítimo); doble métrica `item_count`/`bolt_sets`/
  `individual_bolts`/`gaskets` (`NumberInSet` parseado de texto); group_by
  line/size/spec/material/item_type/shop_field/bolt_size; filtros item_type/line/spec/size/
  shop_field; items sin línea en `untagged` y grupo `"(SIN LÍNEA)"`; implementada, testeada y
  revisada 2026-06-24, 223 tests nuevos, suite total 1064 verdes, test de integración real
  incluido; validado: AIR LIQUIDE HUELVA = bolt_sets 248 / individual_bolts 1952 / gaskets 262 /
  item_count 510; commit pendiente).
  Detección del proyecto abierto: lee `DWGPREFIX` del dibujo activo y sube hasta `Project.xml`.
- Las herramientas de solo lectura del trío original ya están implementadas vía SQLite.
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
