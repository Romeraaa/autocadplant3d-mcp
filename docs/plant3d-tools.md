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

  **Filtro por DWG de modelo** (`data.dwg` / `data.active_dwg`):
  - `data.dwg`: basename o ruta completa de un DWG de modelo. Restringe el inventario a los
    componentes que están **físicamente en ese dibujo** (vía tabla `PnPDataLinks`). Incluye los
    componentes sin `LineNumberTag` válido (placeholder `'?'`), que el filtro `line` y la
    operación `list_lines` no alcanzarían. Solo lectura sobre `Piping.dcf`; no requiere AutoCAD.
  - `data.dwg: "@active"` o `data.active_dwg: true`: detecta automáticamente el DWG abierto en
    AutoCAD (lee la variable `DWGNAME` vía backend File IPC) y filtra por él. Requiere backend
    `file_ipc` (AutoCAD abierto con dibujo); sobre `ezdxf`/headless devuelve error en español.
  - Combinable con los filtros existentes (`classes`, `line`, `spec`, `size`, `limit`).

  **Caso de uso clave — piezas sin etiquetar en el DWG activo:** `list_lines` solo lista líneas
  con `LineNumberTag` válido; los componentes sin etiquetar no aparecen en ella. El filtro
  `dwg`/`active_dwg` sí los incluye porque opera sobre `PnPDataLinks` (presencia física en el
  DWG), no sobre el tag. Flujo típico: `list_components {active_dwg:true, class:"valve"}` →
  elegir un `pnpid` del resultado → `locate` para encuadrar la pieza en el dibujo.

  > **Nota — `list_lines` vs. `dwg`/`locate`:** `list_lines` mira el *tag de línea*
  > (`LineNumberTag`); el filtro `dwg` y la operación `locate` miran lo que está *físicamente en
  > el DWG* (tabla `PnPDataLinks` / handles). Pueden no coincidir: una pieza sin etiquetar existe
  > en el DWG pero no aparece en ninguna línea de `list_lines`.
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
- `find_missing_properties` — lista los componentes con propiedades obligatorias vacías o NULL.
  Solo lectura; NO emite SQL propio: agrega internamente la salida completa de `list_components`
  (sin tope) y evalúa cada componente contra un **perfil por clase canónica**.

  **Perfil por defecto:**

  | Clase canónica | Campos requeridos |
  |---|---|
  | `pipe` | `spec`, `size`, `line` |
  | `valve` | `spec`, `size`, `line`, `tag` |
  | `fitting` | `spec`, `size`, `line` |
  | `flange` | `spec`, `size`, `line` |
  | `instrument` | `tag`, `line` |
  | cualquier otra | `spec`, `size`, `line` |

  Un campo cuenta como ausente cuando es None, cadena vacía o solo espacios. `tag` adicionalmente
  detecta placeholders del tipo `"?"` / `"?-?"`. `size` también trata `"?"` como ausente.

  **Parámetros en `data`:**
  - `required` (opcional) — sobreescribe el perfil. Dos formas:
    - Lista plana `["spec","line"]` — se aplica a TODAS las clases.
    - Dict `{"valve": ["tag"], "pipe": ["spec","size","line"]}` — reemplaza solo las clases
      indicadas; las demás conservan su perfil por defecto.
    - Campos fuera de `{spec, size, line, tag, description}` se descartan con una nota en
      español en `notes`.
  - `classes` / `line` / `spec` / `dwg` — filtros de alcance idénticos a `list_components`,
    reenviados tal cual.
  - `limit` — máximo de componentes CON al menos un campo ausente que se devuelven (default 50,
    0 = sin tope). Los excluidos se reportan en `omitted`; nunca se trunca silenciosamente.

  **Salida:** `{ok, project, path, profile, filters, count, omitted, by_class, components, notes}`.
  - `profile` — perfil efectivo aplicado (refleja los overrides si los hay).
  - `count` — total de componentes con al menos un campo ausente (antes de aplicar `limit`).
  - `by_class` — lista `[{class, count}]` ordenada descendente por `count`.
  - `components` — lista de componentes marcados. Cada entrada: `{pnpid, class, tag, line, missing}`,
    donde `missing` es la lista de nombres de campo ausentes para ese componente.

  **No localiza el objeto en el dibujo** (sin handle/GUID en SQLite — para eso usar `locate`).
  Garantía de solo lectura: los `.dcf` no se modifican.

- `export` — vuelca cualquier listado del proyecto a un fichero **CSV o XLSX**. Solo lectura sobre
  los `.dcf`; el único fichero escrito es el de salida. Crea los directorios padre si no existen.

  **Parámetros obligatorios en `data`:**
  - `kind` — qué listado exportar. Valores admitidos:

    | `kind` | Función subyacente | Filas exportadas |
    |---|---|---|
    | `lines` | `list_lines` | una por línea |
    | `components` | `list_components` | una por componente |
    | `valves` | `list_valves` | una por válvula |
    | `instruments` | `list_instruments` | una por instrumento |
    | `equipment` | `list_equipment` | una por equipo |
    | `bom` | `bom` | una por línea de BOM |
    | `pipe_length` | `pipe_length` | una por grupo de longitud |
    | `weld_list` | `weld_list` | una por grupo de soldadura |
    | `bolt_gasket_list` | `bolt_gasket_list` | una por grupo pernos/juntas |
    | `specs` | `list_specs` | una por spec |
    | `untagged` | `find_untagged` | una por componente sin etiquetar |

  - `path` — ruta del fichero de salida. El formato se deduce por extensión: `.csv` (CSV
    `utf-8-sig`, con BOM para que Excel reconozca acentos) o `.xlsx` (una hoja, primera fila
    = cabecera). Cualquier otra extensión devuelve `ok:False` con mensaje en español.

  **Parámetros opcionales:**
  - Cualquier otro campo en `data` (excepto `kind`, `path` y `project`) se reenvía como filtro
    a la consulta subyacente (`line`, `spec`, `classes`, `group_by`, etc.).
  - `limit` — ignorado: la exportación fuerza siempre `limit=0` (sin tope) para no truncar el
    fichero. Se añade una nota en `notes` cuando el llamante lo incluía.
  - `untagged` no acepta filtros (firma sin `data`): si se pasan, se ignoran con una nota.

  **Columnas:** unión ordenada estable de las claves de todas las filas. Los valores compuestos
  (listas, dicts anidados) se serializan como JSON compacto en la celda.

  **Dependencia XLSX:** requiere `openpyxl>=3.1`. Si no está instalado, devuelve
  `{ok:False, error:"Para exportar a XLSX instala openpyxl"}` sin escribir el fichero. No afecta
  a la exportación CSV.

  **Salida (metadatos, nunca los datos en sí):**
  `{ok, project, path, kind, format, rows, columns, notes}`.
  - `format` — `"csv"` o `"xlsx"`.
  - `rows` — número de filas de datos escritas (sin contar la cabecera).
  - `columns` — lista ordenada de nombres de columna.

  **Garantía de solo lectura:** los `.dcf` quedan byte-idénticos tras la exportación.

---

## Operaciones vía plugin .NET (NO SQLite)

- `locate` — **EXCEPCION: va por el plugin .NET, NO por SQLite.** Localiza objetos Plant 3D en
  el DWG abierto y los resalta/encuadra (select + zoom). Admite tres modos de búsqueda con
  precedencia **pnpids > tag > line**.

  **Admite el parámetro `project`** (igual que el resto de operaciones plant3d): si se omite,
  detecta el proyecto del dibujo activo subiendo desde `DWGPREFIX` hasta `Project.xml`.

  **Modos de búsqueda:**

  - `pnpids` — lista de PnPIDs numéricos (o PnPID único; acepta strings numéricas coercionadas
    a int; rechaza no convertibles y bool con error en español). Modo más directo.
  - `tag` — tag de equipo o instrumento (string). Resuelve a PnPIDs siguiendo:
    `PnPTagRegistry.Tag`→`RowId` (búsqueda principal); si no encuentra, fallback a
    `Equipment.Tag`. El conjunto de PnPIDs resultante se reenvía al plugin.
  - `line` — número de línea (string, p.ej. `"10\"-609001OG010-A01SC1SX"`). Resuelve a
    PnPIDs vía `PipeRunComponent.LineNumberTag`→`PnPID`. Los PnPIDs se reenvían al plugin;
    si el DWG activo no contiene ninguno, `found=0` es la respuesta correcta (el filtrado
    por DWG activo es por diseño). Validado: línea real con 206 PnPIDs → `found=0` porque
    esa línea no estaba en el DWG `R9` abierto.

  **Flujo de resolución (vía HANDLE — vía principal):**
  Python llama a `plant3d_query.resolve_handles(project, pnpids)`, que lee `Piping.dcf`
  directamente (SQLite, solo lectura) siguiendo la cadena:
  `PnPDataLinks.RowId`=PnPID del objeto → `DwgId`=`PnPDrawings.PnPID` → handle en
  `DwgHandleLow/High` → ruta del DWG en `PnPDrawings."Dwg Name"`.
  El resultado es una lista `targets=[{pnpid, dwg, handle}]` que se envía al plugin junto con
  `pnpids`. El plugin C# usa `Database.TryGetObjectId(new Handle(handle))` filtrando por el
  basename del DWG activo, selecciona los objetos encontrados y ejecuta `ZOOM _Object` para
  encuadrar correctamente (funciona también en vistas 3D). PnPIDs cuyo handle no pertenece al
  DWG activo van a `not_found` (existen en el proyecto, pero en otro modelo).
  El enfoque antiguo (`SelectAcPpObjectIds`/`MakeAcDbObjectIds`) se conserva como fallback.

  **Parámetros:**
  - `data={"pnpids": [int]}` — modo pnpids (ver modos de búsqueda)
  - `data={"tag": str}` — modo tag
  - `data={"line": str}` — modo línea
  - `project` (opcional)
  - `zoom?=True`, `select?=True`
  - `isolate?=False` — si `True`, tras localizar los objetos ejecuta `ISOLATEOBJECTS` para
    ocultar todo lo demás y dejar visibles solo los objetos encontrados. Validado en vivo:
    sobre válvula PnPID 200293 dejó visible solo la válvula en el modelo 3D.

  Payload: `{requested, found, not_found, found_count, dwg}`.
  Requiere AutoCAD 2026 abierto con el plugin cargado (NETLOAD) y el DWG del modelo
  correspondiente abierto; sobre `ezdxf`/headless devuelve error en español.

  **PRECAUCION — `view.get_screenshot` tras isolate/unisolate:** el tool devuelve capturas en
  negro (byte-idénticas, falso negativo) después de un cambio de visibilidad por isolate. No
  refleja el estado real de AutoCAD. Confirmar el resultado del isolate mirando AutoCAD
  directamente.

  **VALIDADO EN VIVO** (2026-06-25/26) con el proyecto `23099 - AIR LIQUIDE HUELVA`
  (DWG `23099-PIP-MOD-0001_R9.dwg`):
  - Modo `pnpids`: válvulas 200171 / 200275 / 200293 localizadas y encuadradas en vista 3D
    (commit `b1897a5`, 2026-06-25).
  - Modo `line`: línea `10"-609001OG010-A01SC1SX` → 206 PnPIDs resueltos → `found=0` en R9
    (línea en otro DWG; filtrado correcto) (commit `660a8bc`, 2026-06-26).
  - Modo `tag`: resuelve vía `PnPTagRegistry` + fallback `Equipment` (commit `660a8bc`).
  - Modo `isolate`: PnPID 200293 → solo la válvula visible; confirmado visualmente (commit
    `e723da7`, 2026-06-26).
  Tests unitarios: suite 1141 verdes.

- `unisolate` — **EXCEPCION: va por el plugin .NET, NO por SQLite.** Revierte el aislamiento
  aplicado por `locate` con `isolate:true`, restaurando la visibilidad completa del modelo
  (ejecuta `UNISOLATEOBJECTS`). No requiere parámetros adicionales. Mismas exigencias de
  backend `file_ipc` que `locate`. **VALIDADO EN VIVO** (2026-06-26, `23099 - AIR LIQUIDE
  HUELVA`): restauró el modelo completo confirmado visualmente. Commit: `e723da7`.

  **PRECAUCION — `view.get_screenshot` tras unisolate:** misma limitación que con `isolate`
  (capturas en negro); confirmar en pantalla.

- `plugin_status` — **EXCEPCION: va por el plugin .NET, NO por SQLite.** Ping al plugin para
  verificar que está cargado y responde. Payload: `{plugin, version, plant3d_available,
  project}`. Mismas exigencias de backend `file_ipc` que `locate`. **VALIDADO EN VIVO**
  (2026-06-25, proyecto `23099 - AIR LIQUIDE HUELVA`). Commit: `b1897a5` (2026-06-25).

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

**Cadena de resolución (locate):**
La vía principal es el HANDLE: Python resuelve `PnPDataLinks` → `PnPDrawings` en `Piping.dcf`
(SQLite) y envía `targets=[{pnpid, dwg, handle}]` al plugin. El plugin usa
`Database.TryGetObjectId(new Handle(handle))` filtrando por basename del DWG activo y ejecuta
`ZOOM _Object`. El enfoque antiguo (`SelectAcPpObjectIds`/`MakeAcDbObjectIds`) se conserva como
fallback pero devolvía 0 found en vivo y fue descartado como vía principal.

**Recordatorio operativo** (tras abrir o reiniciar AutoCAD):
1. Cargar `lisp-code/mcp_dispatch.lsp` — sin esto, `system init` falla aunque el plugin esté.
2. NETLOAD de la DLL del plugin.
3. Ejecutar `system init` — el backend es un singleton cacheado; sin este paso se queda en `ezdxf`.
Para recompilar el plugin hay que CERRAR AutoCAD antes (NETLOAD bloquea `bin/Release`).

**Estado actual:**
- Build C# Release: compila limpia (0 avisos/errores) en el entorno actual (net8.0-windows, VS Code + .NET SDK 9).
- DLLs en `C:\Program Files\Autodesk\AutoCAD 2026\PLNT3D\` y `...\AutoCAD 2026\`.
- Tests unitarios: 28 (`tests/test_file_ipc_plant.py`, mockean PostMessage/trigger), suite total 1092 verdes.
- **`locate` VALIDADA EN VIVO** (2026-06-25/26, `23099 - AIR LIQUIDE HUELVA`): PnPIDs
  200171/200275/200293 OK (pnpids); modo tag (PnPTagRegistry + fallback Equipment); modo line
  (206 PnPIDs resueltos, found=0 correcto); isolate (PnPID 200293, confirmado visualmente);
  unisolate (restauración completa, confirmada). Commits: `b1897a5`, `660a8bc`, `e723da7`.
- **`unisolate` VALIDADA EN VIVO** (2026-06-26, mismo proyecto). Commit: `e723da7`.
- **`plugin_status` VALIDADA EN VIVO** (2026-06-25, mismo proyecto). Commit: `b1897a5`.
- La ESCRITURA en Plant 3D (p.ej. `assign-layers-by-property`) sigue PENDIENTE / no abordada aún.

> El historial de implementación operación por operación (fechas, conteos de tests, commits,
> cifras validadas en AIR LIQUIDE HUELVA) vive en la memoria del proyecto, no aquí.
