# Herramientas Propuestas — AutoCAD MCP / Plant 3D

## Fase 1 — Consulta de Datos (solo lectura)

> **Cambio de enfoque (2026-06-18).** Tras la reunión con el responsable de ingeniería, el
> proyecto se centra específicamente en **AutoCAD Plant 3D**. La primera fase construirá
> únicamente herramientas de **consulta** de datos: leer e interrogar la información de un
> modelo Plant 3D sin modificarlo. La creación, validación-con-corrección y automatización de
> escritura quedan para fases posteriores.
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

**Mecanismo de acceso:** todas estas herramientas se apoyan en el plugin .NET
`PlantMcpDispatch.dll` (ver `04 - Plugin Plant 3D`), que usa las APIs
`Autodesk.ProcessPower.*` — principalmente el `DataLinksManager` para leer filas y propiedades,
y `PnPProjectManager` para la estructura del proyecto. AutoLISP **no** puede acceder a estos
datos, por eso es imprescindible el plugin.

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

#### B1. `plant3d-list-lines`
Lista todos los **números de línea** del proyecto con sus propiedades: servicio/fluido,
especificación, diámetro, aislamiento y P&ID de origen.
**Valor:** la *line list* es uno de los entregables más solicitados.

#### B2. `plant3d-list-components`
Lista componentes de tubería con filtros por clase (pipe, valve, fitting, flange, instrument),
línea, especificación o diámetro. Devuelve handle, tag, clase y propiedades principales.
**Valor:** consulta general y base de casi todas las demás.

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
| ⭐⭐⭐ | B2. `list-components` | Lector base que reutilizan casi todas las demás |
| ⭐⭐⭐ | B1. `list-lines` | Line list — entregable de máximo valor |
| ✅ | E1. `find-untagged` | IMPLEMENTADA (2026-06-20), solo lectura vía SQLite |
| ✅ | E2. `validate-specs` | IMPLEMENTADA (2026-06-22), solo lectura vía SQLite + .pspc |
| ⭐⭐ | B5. `list-valves` / B6. `list-instruments` | Entregables habituales |
| ⭐⭐ | D1. `bom` / D2. `pipe-length` | Medición automática |
| ⭐⭐ | E4. `pid-vs-3d-consistency` | Comprobación muy costosa a mano |
| ⭐ | A2, B3, B4, C1, C2, E3, F1 | Complementarias; se priorizan según el listado del cliente |

---

## Plan de ejecución de la Fase 1

1. **Recibir** el listado de consultas de la organización y los modelos Plant 3D de prueba.
2. **Inspeccionar** un modelo real para confirmar nombres de clase y propiedades exactas vía
   `DataLinksManager` (el mapeo real solo se puede cerrar con un DWG de proyecto).
3. **Scaffolding** del plugin `PlantMcpDispatch.dll` con un primer lector genérico
   (`list-components`) como prueba de concepto end-to-end (Python → IPC → plugin → JSON).
4. **Iterar** el catálogo: implementar las herramientas ⭐⭐⭐ primero, validar con el cliente,
   y ampliar.

---

*Documento de análisis preliminar — sesión de trabajo con Claude Code. AutoCAD MCP / Plant 3D, Fase 1: Consulta de Datos.*
