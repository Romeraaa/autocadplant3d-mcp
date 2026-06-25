# Herramientas `plant3d` — referencia detallada

Detalle operación por operación del tool MCP `plant3d`. El `CLAUDE.md` solo lista
las operaciones; **lee este fichero cuando necesites el comportamiento exacto, los
parámetros o el formato de salida de una operación concreta**.

**Por defecto consulta el proyecto que el usuario tiene abierto en AutoCAD:** si no se pasa
`project`, lee `DWGPREFIX` del dibujo activo (vía backend File IPC) y sube hasta el `Project.xml`.
También admite `project` explícito (ruta a la carpeta o, con `AUTOCAD_MCP_PLANT3D_ROOT`, el nombre).

La mayoría de las operaciones leen directamente las bases SQLite (`.dcf`) del proyecto — no
requieren el plugin .NET y nunca modifican el proyecto (apertura `mode=ro`). **Excepción:**
`locate` y `plugin_status` van por el plugin .NET (ver más abajo).

---

## Operaciones de consulta (SQLite, solo lectura)

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
  en el dibujo** (sin handle/GUID en SQLite — para eso usar `locate`).

---

## Operaciones vía plugin .NET (NO SQLite)

- `locate` — **EXCEPCION: va por el plugin .NET, NO por SQLite.** Localiza objetos Plant 3D en
  el DWG abierto por su `PnPID` y los resalta/encuadra (select + zoom). Resuelve PnPID →
  ObjectId en el DWG activo vía `DataLinksManager` del proyecto Plant 3D; recorre los
  DataLinksManager de todas las partes del proyecto. Los ObjectId cuya `Database` no sea la
  del documento activo van a `not_found` (existen en el proyecto pero no en el DWG abierto).
  Parámetros: `data={"pnpids": [int]}` (o `pnpid` único; acepta strings numéricas coercionadas
  a int; rechaza no convertibles y bool con error en español), `zoom?=True`, `select?=True`.
  Payload: `{requested, found, not_found, found_count, dwg}`. Requiere AutoCAD 2026 abierto
  con el plugin cargado (NETLOAD) y el DWG del modelo correspondiente abierto.
  **PENDIENTE DE VALIDACION en AutoCAD vivo** — firmas de API Plant 3D descubiertas con el
  `probe`, marcadas con comentarios `PENDIENTE DE VALIDAR`; bloqueado por DWG de prueba
  multi-modelo (pendiente de recibir de la organización). Tests unitarios: 28 (suite 1092
  verdes). Commit: `b1897a5` (2026-06-25).
- `plugin_status` — **EXCEPCION: va por el plugin .NET, NO por SQLite.** Ping al plugin para
  verificar que está cargado y responde. Payload: `{plugin, version, plant3d_available,
  project}`. Mismas exigencias de backend `file_ipc` que `locate`. Commit: `b1897a5`
  (2026-06-25).

---

## Plugin .NET `PlantMcpDispatch` — arquitectura

Plugin C# (`plant3d-plugin/PlantMcpDispatch.dll`) con APIs de Plant 3D (`Autodesk.ProcessPower.*`).
Necesario cuando la operación requiere: (a) **localizar objetos en el DWG** (handles/ObjectId, no
disponibles en SQLite), o (b) **ESCRITURA** en la sesión viva (p.ej. asignar capas de verdad).
AutoLISP no puede acceder a estas APIs.

**Estructura en `plant3d-plugin/`** (net8.0-windows):
- `src/IpcContract.cs` — contratos de mensajes IPC
- `src/IpcChannel.cs` — canal IPC (escritura atómica tmp+rename, UTF-8)
- `src/Plant3dAccess.cs` — acceso aislado a la API Plant 3D (`DataLinksManager`)
- `src/DispatchCommand.cs` — comando `MCPPLANTDISPATCH` + dispatcher whitelist sin eval,
  try/catch global que garantiza respuesta
- `probe/` — utilidad de descubrimiento de API (no se carga en AutoCAD)

**Canal IPC del plugin** (distinto del canal LISP):
- Ficheros: `autocad_mcp_plant_cmd_{id}.json` / `autocad_mcp_plant_result_{id}.json` en `C:\temp`
- Trigger: comando AutoCAD `MCPPLANTDISPATCH` (sin paréntesis, a diferencia del LISP `(c:mcp-dispatch)`)
- Comparte el MISMO lock asyncio único con la ruta LISP — solo un comando en vuelo entre ambas rutas
- `_dispatch_core` en `file_ipc.py` parametrizado por prefijo + trigger; limpia comandos huérfanos
  por prefijo antes de escribir

**Estado actual:**
- Build C# Release: compila limpia (0 avisos/errores) en el entorno actual (net8.0-windows, VS Code + .NET SDK 9).
- DLLs en `C:\Program Files\Autodesk\AutoCAD 2026\PLNT3D\` y `...\AutoCAD 2026\`.
- Tests unitarios: 28 (`tests/test_file_ipc_plant.py`, mockean PostMessage/trigger), suite total 1092 verdes.
- **`locate` PENDIENTE DE VALIDACION en AutoCAD vivo** — bloqueado por DWG de prueba multi-modelo.
- La ESCRITURA en Plant 3D (p.ej. `assign-layers-by-property`) sigue PENDIENTE / no abordada aún.

> El historial de implementación operación por operación (fechas, conteos de tests, commits,
> cifras validadas en AIR LIQUIDE HUELVA) vive en la memoria del proyecto, no aquí.
