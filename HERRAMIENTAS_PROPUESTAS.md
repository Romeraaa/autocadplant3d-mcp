# Herramientas Propuestas — AutoCAD MCP / Plant 3D

## Fase 1 — Consulta de Datos (solo lectura)

> **Cambio de enfoque (2026-06-18).** Tras la reunión con el responsable de ingeniería, el
> proyecto se centra específicamente en **AutoCAD Plant 3D**. La primera fase construirá
> únicamente herramientas de **consulta** de datos: leer e interrogar la información de un
> modelo Plant 3D sin modificarlo.
>
> **Decisión 2026-06-22 — fase actual: EXCLUSIVAMENTE consulta (solo lectura).** La escritura,
> la validación-con-corrección y cualquier automatización que modifique el modelo quedan aplazadas.
> El plugin .NET (`PlantMcpDispatch.dll`) **no se desarrolla por ahora**: las herramientas de
> consulta se implementan directamente sobre los SQLite del proyecto (`.dcf`, `.pspc`) mediante
> Python, sin necesidad del plugin.
>
> **Pendiente de la organización:**
> - Listado de referencias / consultas concretas que se quieren hacer al MCP.
> - Varios modelos Plant 3D reales para las primeras pruebas.
>
> Este documento es un **primer análisis** de las herramientas que podríamos desarrollar. Se
> revisará y priorizará cuando lleguen el listado de consultas y los modelos de prueba.

---

## Por qué "solo consulta" es la fase correcta para empezar

1. **Riesgo cero sobre el modelo.** Todas las operaciones son de lectura. No hay posibilidad de
   corromper la base de datos del proyecto Plant 3D ni los DWG. Ideal para validar la
   arquitectura del plugin con modelos reales del cliente.
2. **Máximo valor inmediato.** El grueso del trabajo manual en Plant 3D es *extraer y verificar
   datos* (line lists, listas de válvulas, mediciones, comprobaciones de coherencia). Son
   exactamente las tareas que un asistente que "lee el modelo" resuelve en segundos.
3. **Base para las fases siguientes.** Las herramientas de escritura (asignar capas, corregir
   tags, completar propiedades) reutilizarán los mismos lectores. Construir bien la capa de
   consulta primero simplifica todo lo demás.

---

## El modelo de datos de Plant 3D (contexto)

A diferencia de un DWG de AutoCAD "plano", un proyecto Plant 3D combina **geometría** con una
**base de datos de proyecto**:

- **Proyecto** — definido por `Project.xml`. Agrupa varios DWG organizados por tipo: **P&ID**,
  **Modelo 3D** (piping), **Isométricos** y **Ortográficos**, más informes y especificaciones.
- **Base de datos del proyecto** — SQLite (`PnPID.dcf`, `PnPProjectDb…`) o SQL Server. Guarda
  las propiedades de cada componente: cada objeto del dibujo está **vinculado** a una fila de la
  base de datos mediante el `DataLinksManager`.
- **Clases de objeto** — Pipe, Fitting (codos, tés, reducciones), Valve, Flange, Instrument,
  Pipe Support, Equipment, Nozzle… Cada clase tiene su conjunto de propiedades.
- **Propiedades clave** que un ingeniero consulta habitualmente:
  - `LineNumberTag` / número de línea, `Service` / fluido, `SpecName` (especificación de
    tubería), `NominalDiameter` / `Size`, material, tipo y espesor de aislamiento, `Tag` de
    equipo/instrumento, fabricante, modelo, tipo de extremos, presión/clase nominal.
- **Especificaciones (.pspx)** — catálogo de piping del proyecto (qué componentes están
  permitidos para cada clase de tubería).
- **P&ID** — los diagramas llevan sus propios tags de activo y de línea, que *deberían* coincidir
  con el modelo 3D.

**Mecanismo de acceso (hallazgo clave de esta fase):** los ficheros de base de datos del
proyecto Plant 3D (`.dcf`: `Piping.dcf`, `ProcessPower.dcf`…) y los catálogos de specs
(`Spec Sheets\*.pspc`) son **bases SQLite estándar**. Las herramientas de **consulta** las leen
directamente con el módulo `sqlite3` de Python en modo solo lectura (`mode=ro`), **sin necesidad
del plugin .NET** ni de AutoCAD abierto.

El plugin .NET (`PlantMcpDispatch.dll`) quedaría reservado para dos casos específicos, ambos
**aplazados** en la fase actual:
- (a) **Escritura** en la sesión viva de AutoCAD (p.ej. asignar capas o propiedades en el dibujo).
- (b) **Datos no presentes en el SQLite** — como handles o GUIDs para localizar objetos
  físicamente en el dibujo, accesibles únicamente vía `DataLinksManager`.

AutoLISP sigue sin poder acceder a las APIs de Plant 3D (`Autodesk.ProcessPower.*`); esa
limitación no afecta a la fase actual porque toda la consulta va directamente contra el SQLite.
Si alguna herramienta del catálogo necesitara handles o escritura, se anotará explícitamente.

---

## Catálogo de herramientas de consulta

Agrupadas por tipo de dato. Cada una devuelve datos estructurados (JSON) que Claude presenta al
usuario en tabla, resumen o exportable a CSV/Excel.

### A · Estructura del proyecto y dibujos

#### A1. `plant3d-project-info`
Lee `Project.xml` y devuelve la ficha del proyecto: nombre, ruta, tipo de base de datos, sistema
de unidades, listado de especificaciones referenciadas y número de dibujos por tipo.
**Valor:** primer comando de "orientación" al abrir un proyecto ajeno.

#### A2. `plant3d-list-drawings`
Enumera todos los dibujos del proyecto clasificados por tipo (P&ID, Modelo 3D, Isométrico,
Ortográfico), con ruta y estado.
**Valor:** inventario rápido del alcance del proyecto.

---

### B · Componentes y líneas

#### B1. `plant3d-list-lines` — ✅ IMPLEMENTADA (2026-06-22)

Genera la **LINE LIST** del proyecto: una fila por número de línea válido (`LineNumberTag` no
NULL/vacío/`?`). Expuesta como la operación `list_lines` del tool `plant3d`.

**Estrategia híbrida** sobre `Piping.dcf` (SQLite, `mode=ro`):
- **Propiedades de línea** (Service, NominalSpec, NominalSize, aislamiento) desde la tabla
  cabecera `P3dLineGroup` (1 fila por línea). El match de Tag se hace por clave normalizada
  (TRIM+UPPER) para tolerar espacios y variaciones de caja. **No se usa** `PipeRunComponent.Service`
  porque se contamina con ramales (p.ej. `"AC,P"`).
- **Specs reales y diámetros** agregados desde `EngineeringItems` — los tamaños se mantienen
  **separados por unidad** (in/mm), sin colapsar a rango global.
- **`model_dwgs`** — DWG del modelo 3D donde vive la línea, vía
  `P3dDrawingLineGroupRelationship` → `PnPDrawings`. No es el P&ID.
- **Robustez de esquema:** el esquema de `P3dLineGroup` varía por proyecto; se usa
  `PRAGMA table_info` para seleccionar solo las columnas presentes. Degrada con gracia
  (null/[] + notas) si faltan columnas opcionales o las tablas de relación de DWG.

**Parámetros:** `data["ignore_specs"]` (lista de specs auxiliares a excluir, mapea a
`_DEFAULT_IGNORE_SPECS`) · `data["limit"]` (acota salida, default 50, 0 = sin tope).

**Salida:** `project`, `path`, `count`, `lines` (acotada por `limit`, con `omitted`), `notes`.

**Limitaciones (no disponibles vía SQLite — aplazadas, requerirían plugin .NET):**
- P&ID de origen de la línea.
- Localización física del objeto en el dibujo (sin handle/GUID en SQLite).

**Validado:** proyecto `23099 - AIR LIQUIDE HUELVA` = 114 líneas. ~98 tests nuevos; suite
total: 306 tests, todos verdes. Commit `6c40dee` (2026-06-22).

**Valor:** la *line list* es uno de los entregables más solicitados en ingeniería de proceso.

#### B2. `plant3d-list-components` — ✅ IMPLEMENTADA (2026-06-22)

Lista genérica de componentes de tubería (`PipeRunComponent` ⨝ `EngineeringItems` por `PnPID`).
**Solo lectura vía SQLite** (`Piping.dcf`, `mode=ro`) — no requiere el plugin .NET.
Expuesta como la operación `list_components` del tool `plant3d`.

**Filtros opcionales:**
- `classes` — lista de clases. Mapeo canónico: `pipe` → Pipe, `valve` → Valves,
  `fitting` → Fittings + Olet, `flange` → Flanges, `instrument` → Instruments,
  `support` → PartCategory NULL / '' / Default. Valor no canónico = passthrough literal
  de `PartCategory`. Omitido = todas las clases.
- `line` — filtro por `LineNumberTag` (normalizado TRIM+UPPER, coincidencia exacta).
- `spec` — filtro por `EngineeringItems.Spec` (normalizado exacto).
- `size` — `{"value", "unit"}`; **exige unidad** (no mezcla in/mm); sin unidad no filtra
  y genera una nota de advertencia en la salida.
- `limit` — default 50, 0 = sin tope (reporta `omitted`; sin truncado silencioso).

**Salida:** `{ok, project, path, limit, filters, count, omitted, by_class, components, notes}`.
Cada componente incluye: `pnpid`, `class`, `tag`, `description`, `spec`, `size`, `line`.
El campo `tag` (= `PipeRunComponent.Tag`) se sanea: NULL / '' / '?' / '?-?' → None.
Degrada con gracia si la columna `Tag` no existe en el esquema del proyecto.
`by_class` usa `(sin clase)` para PartCategory NULL / ''.

**Limitación (igual que las demás tools de consulta):** identifica cada componente por
`PnPID` + propiedades; **no lo localiza en el dibujo** (sin handle/GUID en SQLite — la
localización requeriría el plugin .NET).

**Validado:** proyecto `23099 - AIR LIQUIDE HUELVA`: Pipe = 1682, Fittings = 1471,
Flanges = 431, Valves = 357, Olet = 97, Instruments = 33, total = 4666;
`classes=["valve"]` → 357. **96 tests nuevos; suite total: 402 tests, todos verdes.**
Revisada por code-reviewer: aprobada, sin bloqueantes. Implementada 2026-06-22.

**Valor:** consulta general y base de casi todas las demás herramientas de listado.

#### B3. `plant3d-get-component`
Volcado completo de propiedades de un único componente identificado por handle o tag (todas sus
propiedades de base de datos + datos de spec).
**Valor:** inspección de detalle de un elemento concreto.

#### B4. `plant3d-list-equipment`
Lista de equipos con tag, tipo, propiedades y nozzles asociados.
**Valor:** equipment list, base para comprobar conexiones.

#### B5. `plant3d-list-valves`
Lista de válvulas con tag, tipo, diámetro, clase, spec y línea a la que pertenecen.
**Valor:** *valve list*, entregable habitual de ingeniería de proceso.

#### B6. `plant3d-list-instruments`
Lista de instrumentos con tag, tipo de función, línea y conexión.
**Valor:** *instrument list*, cruzable con el P&ID.

---

### C · Especificaciones y catálogo

#### C1. `plant3d-list-specs`
Especificaciones de tubería referenciadas en el proyecto, con su descripción y rango de uso.
**Valor:** saber qué specs maneja el proyecto antes de auditar componentes.

#### C2. `plant3d-spec-contents`
Componentes permitidos en una especificación dada (clase, tamaño, descripción).
**Valor:** comprobar qué está disponible al modelar una línea de cierta clase.

---

### D · Medición y materiales (BOM / MTO)

#### D1. `plant3d-bom`
Lista de materiales agregada por clase, especificación y diámetro: número de componentes y
longitud total de tubería por tamaño.
**Valor:** medición automática, base de presupuesto.

#### D2. `plant3d-pipe-length`
Longitud total de tubería desglosada por línea, servicio y diámetro.
**Valor:** mediciones de tubería sin contar manualmente.

---

### E · Calidad y coherencia de datos (comprobaciones de solo lectura)

> Detectan problemas y **reportan**; no corrigen (la corrección será una fase posterior).

#### E1. `plant3d-find-untagged` — ✅ IMPLEMENTADA (2026-06-20)
Lista los componentes de tubería sin `LineNumberTag` válido (NULL, vacío o `?`), con desglose por
clase (`PartCategory`) y por especificación. **Solo lectura vía SQLite** (`Piping.dcf`, `mode=ro`) —
no requiere el plugin .NET. Expuesta como la operación `find_untagged` del tool `plant3d`.
**Limitación:** identifica cada componente por `PnPID` + propiedades; **no lo localiza en el dibujo**
(el SQLite no guarda handle/GUID; la localización requeriría el plugin .NET). Por eso no se agrupa por
capa como se planteó inicialmente.
**Conteos validados:** AIR LIQUIDE HUELVA = 1158 untagged de 4666 · PI2588002 = 7.
**Valor:** detectar elementos huérfanos antes de generar isométricos o informes.

#### E2. `plant3d-validate-specs` — ✅ IMPLEMENTADA (2026-06-22)
Valida coherencia de especificaciones cruzando `Piping.dcf` con los catálogos `Spec Sheets\*.pspc`
(ambos SQLite). **Solo lectura** — no requiere el plugin .NET.
Cuatro comprobaciones: (1) Spec ≠ Required Spec de la línea; (2) specs mezcladas dentro de un
mismo `LineNumberTag`; (3) componentes con spec vacía/NULL; (4) spec fantasma (usada en el
proyecto pero sin `.pspc` en el catálogo) y material/schedule fuera de catálogo.
**Hallazgo clave:** los catálogos de specs (`Spec Sheets\*.pspc`) también son SQLite, accesibles
directamente con Python. Degrada con gracia si la carpeta no existe o un `.pspc` es ilegible.
Parámetros: `data["ignore_specs"]` (excluir specs auxiliares) y `data["limit"]` (acotar salida).
Identifica componentes por `PnPID` + propiedades; **no los localiza en el dibujo** (sin handle).
65 tests nuevos; suite total: 208 tests, todos verdes. Commit `f4ecdab`.
**Valor:** evitar errores de especificación que disparan rechazos en revisión.

#### E3. `plant3d-find-missing-properties`
Lista componentes a los que les faltan propiedades obligatorias (diámetro, servicio, material…),
según un perfil de campos requeridos configurable.
**Valor:** asegurar la completitud de datos antes de exportar informes.

#### E4. `plant3d-pid-vs-3d-consistency`
Compara los números de línea y tags entre los P&ID y el modelo 3D, reportando líneas presentes en
uno pero no en el otro o con propiedades discrepantes.
**Valor:** la verificación P&ID ↔ 3D es una de las comprobaciones más costosas de hacer a mano.

---

### F · P&ID

#### F1. `plant3d-list-pid-tags`
Enumera los tags de activo y de línea presentes en los dibujos P&ID, con su clase y dibujo de
origen.
**Valor:** inventario del P&ID, base para el cruce con el modelo 3D (E4).

---

## Priorización preliminar

> Sujeta a revisión cuando llegue el listado de consultas de la organización.

| Prioridad | Herramienta | Motivo |
|-----------|-------------|--------|
| ⭐⭐⭐ | A1. `project-info` | Punto de entrada; valida acceso al proyecto y la BD |
| ✅ | B2. `list-components` | IMPLEMENTADA (2026-06-22), filtros por clase/línea/spec/size; 96 tests; suite 402 verde |
| ✅ | B1. `list-lines` | IMPLEMENTADA (2026-06-22), estrategia híbrida P3dLineGroup + EngineeringItems |
| ✅ | E1. `find-untagged` | IMPLEMENTADA (2026-06-20), solo lectura vía SQLite |
| ✅ | E2. `validate-specs` | IMPLEMENTADA (2026-06-22), solo lectura vía SQLite + .pspc |
| ⭐⭐ | B5. `list-valves` / B6. `list-instruments` | Entregables habituales |
| ⭐⭐ | D1. `bom` / D2. `pipe-length` | Medición automática |
| ⭐⭐ | E4. `pid-vs-3d-consistency` | Comprobación muy costosa a mano |
| ⭐ | A2, B3, B4, C1, C2, E3, F1 | Complementarias; se priorizan según el listado del cliente |

---

## Plan de ejecución de la Fase 1

> **Fase actual: SOLO CONSULTA vía SQLite** — el plugin .NET queda aplazado (ver nota de cabecera).

1. **Recibir** el listado de consultas de la organización y los modelos Plant 3D de prueba.
2. **Inspeccionar** el esquema SQLite de un modelo real (`sqlite3` + `.dcf` / `.pspc`) para
   confirmar nombres de tabla, columnas y propiedades exactas — no requiere el plugin ni AutoCAD.
3. **Implementar** las herramientas ⭐⭐⭐ del catálogo como operaciones del tool `plant3d`,
   siguiendo el patrón de `find_untagged`, `validate_specs` y `list_lines` (Python →
   `plant3d_query.py` → JSON).
4. **Iterar**: validar con el cliente y ampliar el catálogo según el listado de consultas recibido.

*Si en el futuro se abre la fase de escritura, se retoma el scaffolding de `PlantMcpDispatch.dll`
con el modelo de datos ya conocido por las consultas.*

---

## Fase 2 (propuesta 2026-06-29) — Extracción de P&ID legacy y generación de especificaciones

> Origen: reunión con ingeniería (jun-2026). Dos focos: **extraer información de P&IDs** y **explotar/generar especificaciones y catálogos**. Análisis basado en ficheros reales aportados:
> - P&IDs de ejemplo en `…/MCP-Plant3D/Proyectos/PID_PI2588002`: **planos legacy de Repsol Cartagena (líneas de hidrógeno, rev. 2007)** en formato **`.dgn` (MicroStation/Bentley) + PDF**. **NO son nativos de Plant 3D** (no hay `.dcf`); las herramientas SQLite de la Fase 1 no aplican aquí.
> - Catálogo exportado en `…/MCP-Plant3D/Excel exportado_NXD-2`: dos representaciones de la **misma clase de tubería NXD-2** → `piping_class.xlsx` (documento de spec "oficial" de ingeniería, bilingüe ES/EN, una hoja por familia con código UNICODE, Ø, schedule, rating, L-code, límites P/T) y `prueba.xlsx` (**export del Spec Editor de Plant 3D**: hojas *Spec Sheet*, *Branch Table*, *Spec Data* con 415 componentes y todos los atributos).
>
> **Hallazgo clave:** el PDF de los P&ID tiene **capa de texto vectorial real** (no es imagen escaneada). Se extrae texto limpio (tags de línea, Ø, válvulas/instrumentos `FCV`/`PCV`/`PI`/`FE`, servicios, notas, cajetín) **sin OCR**, parseando el PDF. Vía recomendada frente a leer el `.dgn` (formato Bentley, complejo → fase posterior solo si hace falta geometría/conectividad).

### G · Extracción de información de P&ID (parseo del texto del PDF)
- **G1. line-list-pdf** — extraer nº de línea, Ø, servicio/fluido de cada P&ID y volcar a Excel (line list automático; hoy manual, máximo ahorro).
- **G2. valve-instrument-index** — listar y contar válvulas e instrumentos (`FCV`, `PCV`, `PI`, `FE`…) con tag y plano.
- **G3. equipment-list** — bombas, depósitos, cisternas, torres.
- **G4. flag-hotspots** — marcar notas tipo `LINEA CORTADA`, `FUERA DE SERVICIO`, `NO LOCALIZADA EN PLANTA`, bridas ciegas, para revisión.
- **G5. cross-sheet-continuity** — detectar referencias entre hojas (`P&I 38361 H.2/7`) y reconstruir continuidad de líneas.
- **G6. drawing-register** — leer cajetín de todos los PDF (nº plano, revisión, fecha, escala, firmas) → índice de planos automático.
- **G7. batch-folder** — procesar una carpeta entera de P&ID y consolidar los registros anteriores.
- **G8. nl-search** — búsqueda en lenguaje natural sobre el conjunto ("¿en qué planos aparece la línea de 8\" de H2?").

> Riesgo G: el texto del PDF sale sin orden espacial fiable. Para line lists precisos hay que extraer **posición** de cada texto (factible con PyMuPDF), **a validar con prueba** antes de prometer precisión.

### H · Explotación de specs / catálogos (consulta sobre los Excel)
- **H1. spec-query-nl** — consulta en lenguaje natural ("¿qué válvula uso para 3\" en NXD-2?", "¿qué schedule lleva el tubo de 6\"?").
- **H2. branch-table-lookup** — "¿cómo conecto una derivación de 2\" sobre un colector de 6\"?" → responde según la Branch Table (p.ej. `W`=WELDOLET, `SK`=SOCKOLET, `RT1`=TE RED).
- **H3. code-lookup** — traducir entre código UNICODE / Item Code / L-code y descripción, material, rating.
- **H4. bom-from-linelist** — generar BOM/MTO a partir de un line list + la clase, usando la Branch Table.
- **H5. pt-limits** — extraer y consultar la tabla de límites presión/temperatura de la spec.

### I · Cruces y validación (mayor valor de ingeniería)
- **I1. validate-plant3d-vs-oficial** — comprobar que cada componente/UNICODE del documento oficial existe en el *Spec Data* de Plant 3D y que material/schedule/rating coinciden. Caza errores de carga de spec.
- **I2. spec-gaps** — detectar tamaños o variantes (p.ej. variante H2 `L-1276-H2`) presentes en un lado y ausentes en el otro.
- **I3. spec-text-qa** — normalizar erratas detectadas en los datos (`VÁVULA`, `VÁLVOLET`, `VÁvula`…).
- **I4. pid-vs-spec** — para cada línea del P&ID (Ø + servicio), comprobar que la clase cubre ese tamaño y esa variante de material.
- **I5. spec-diff** — comparar dos exports/revisiones de spec y resaltar cambios.

### J · Generación de especificaciones Plant 3D (`.pspc` / `.pspx`) — petición de ingeniería
> Idea de ingeniería: "creamos catálogos y especificaciones a partir de un piping class (de cliente, propio…). Igual con IA, volcándole un Excel/PDF, sacamos archivos compatibles con `.pspc`/`.pspx`". Es la propuesta de mayor valor, pero la más compleja. Análisis de viabilidad:
>
> - **Lo fácil (donde brilla la IA): parsear y normalizar la entrada.** Convertir un piping class desordenado (Excel fiable; PDF poco fiable) en una **definición de spec estructurada**: familias, rangos de tamaño, tipos de extremo (BW/SW/THD/FLG), materiales/L-codes, descripciones ES/EN y branch table.
> - **Lo difícil: ESCRIBIR un fichero válido que Plant 3D acepte sin corromper.**
>   - **Dependencia dura:** una spec no inventa piezas; **selecciona piezas existentes en un catálogo `.pcat`** (cada componente apunta a una pieza por identidad/GUID). Si la pieza no existe → hay que crearla en el catálogo (más difícil).
>   - **Vía A — API .NET oficial** (`Autodesk.ProcessPower`, automatizar Spec Editor): soportada y segura, encaja con el track del plugin .NET. Apuesta sólida a medio plazo.
>   - **Vía B — escribir el SQLite directamente** (`.pspc`/`.pcat` son SQLite, ya sabemos leerlos): posible pero **arriesgado** (corrupción). Solo exploración.
> - **Victoria intermedia realista:** generar la **definición de spec validada + branch table + datasheets bilingües** en el layout que importa el Spec Editor, recortando el trabajo manual de elegir pieza a pieza aunque el paso final lo dé Spec Editor.
> - **Para evaluar viabilidad, pedir a la organización:** un `.pspc` + `.pspx` de ejemplo **junto con el catálogo `.pcat`** del que sale, y la versión de Plant 3D.
>
> - **J1. parse-piping-class** — Excel/PDF de piping class → definición de spec estructurada y normalizada (intermedio reutilizable).
> - **J2. build-branch-table** — derivar/validar la matriz de conexión de derivaciones.
> - **J3. gen-spec-sheet** — generar el layout Spec Sheet + datasheets ES/EN listos para Spec Editor.
> - **J4. write-pspc (.NET)** — generación del `.pspc`/`.pspx` vía API .NET (fase avanzada; requiere catálogo con las piezas).

### Priorización Fase 2 (preliminar)
| Prioridad | Herramienta | Por qué |
|---|---|---|
| ⭐⭐⭐ | G1. line-list-pdf | Máximo ahorro; datos ya confirmados en el PDF |
| ⭐⭐⭐ | I1. validate-plant3d-vs-oficial | Caza errores de spec; datos ya disponibles |
| ⭐⭐⭐ | J1. parse-piping-class | Base de la generación de specs; donde más brilla la IA |
| ⭐⭐ | G2/G6. índices y registro de planos | Entregables habituales, bajo coste |
| ⭐⭐ | H1/H2. consulta de spec y branch table | Consulta diaria en lenguaje natural |
| ⭐ | G3-G5, G7-G8, H3-H5, I2-I5 | Complementarias |
| ⭐ (fase avanzada) | J2-J4. generación `.pspc`/`.pspx` | Requiere `.pcat` de ejemplo y/o API .NET |

> **Pendiente de la organización para Fase 2:** confirmar si el objetivo son proyectos nuevos (Plant 3D nativo) o legacy (DGN/PDF); aportar un `.pspc`+`.pspx`+`.pcat` de ejemplo y la versión de Plant 3D; validar precisión del parseo de PDF con una prueba real.

---

*Documento de análisis preliminar — sesión de trabajo con Claude Code. AutoCAD MCP / Plant 3D, Fase 1: Consulta de Datos.*
