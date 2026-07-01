# PoC: generación de una especificación de AutoCAD Plant 3D (`.pspc` + `.pspx`)

Prueba de concepto **independiente** (no es código del servidor MCP) que demuestra que podemos
**autorar** un fichero de especificación de Plant 3D desde Python puro (solo librería estándar:
`sqlite3`, `zipfile`, `uuid`, `xml.etree.ElementTree`, `datetime`, `shutil`) más `openpyxl` para
leer el piping class. Sin más dependencias.

---

## Herramienta `specgen` (paquete + CLI)

El código está empaquetado y generalizado en `specgen/` (nada cableado: descubre los catálogos de
una carpeta, deriva la definición de la spec del piping class, deduce las variantes `-H2` del propio
Excel y toma la branch table de una plantilla opcional). Es lo que debe usarse.

### Uso

```bash
python -m specgen build \
    --piping-class RUTA/piping_class.xlsx \
    --catalogs   CARPETA_CON_LOS_PCAT \
    --out        CARPETA_SALIDA \
    [--spec-name NOMBRE] \
    [--extend-h2] \
    [--template-pspc RUTA/PLANTILLA.pspc]
```

| Opción | Qué hace |
|---|---|
| `--piping-class` | `.xlsx` del piping class REPSOL (una hoja por familia). **Obligatorio.** |
| `--catalogs` | Carpeta con los `.pcat` (SQLite). Se descubren todos automáticamente. **Obligatorio.** |
| `--out` | Carpeta de salida. **Obligatorio.** |
| `--spec-name` | Nombre de la spec (por defecto, el nombre del fichero del piping class). |
| `--extend-h2` | Amplía copias de los catálogos con las familias `-H2` **deducidas del Excel** (no una lista fija) para que las entradas de servicio de hidrógeno casen con una familia dedicada en vez de sustituir la base. |
| `--template-pspc` | Plantilla `.pspc`; su `.pspx` hermano aporta la **branch table** y los fragmentos XML. Sin plantilla, se emite una branch table mínima (vacía) y la spec abre igualmente. |

### Salidas (en `--out`)

| Fichero | Qué es |
|---|---|
| `<NOMBRE>.pspc` / `.pspx` | La especificación generada (SQLite + paquete ZIP/OPC). |
| `REVISION_MATCHING.xlsx` | Informe de revisión: una fila por entrada, ordenado **dudoso primero** (BAJA → MEDIA → SUSTITUCION → ALTA), con la familia de catálogo elegida, los candidatos alternativos y el estado. |
| `catalogs/` | Solo con `--extend-h2`: las **copias** de los catálogos ampliadas con las familias `-H2` (los originales NO se tocan). |

Al terminar imprime un resumen: cobertura por nivel de confianza, nº de piezas materializadas, huecos
sin casar, y la verificación de la spec (`integrity_check`, grafo sin huérfanos, `.pspx` ZIP/XML).

### Niveles de confianza (señal de revisión)

`ALTA` (familia única e inequívoca) · `MEDIA` (casa pero ambiguo: varias familias compiten, falta una
señal discriminante, o un retry sin filtro de diámetro resolvió la familia —p. ej. espárragos cuyo
diámetro de tornillo difiere del `NominalDiameter` del catálogo—) · `SUSTITUCION` (variante `-H2`
resuelta a su familia base) · `BAJA` (sin L-code, L-code ausente de todo catálogo, o sin fila). El
ingeniero confía en `ALTA` y revisa lo demás en `REVISION_MATCHING.xlsx`.

### Módulos

`common` (primitivas GUID/ticks, texto, parseo numérico) · `piping_class` (parser del Excel) ·
`catalog_index` (descubre e indexa `.pcat`) · `matcher` (matching con confianza) ·
`spec_builder` (materializa la `.pspc`/`.pspx`) · `catalog_extender` (clona familias `-H2`) ·
`report` (`REVISION_MATCHING.xlsx` + cobertura) · `cli` (`python -m specgen build`).

### Tests

```bash
python -m pytest tests/ -q
```

Unitarios (sin ficheros) + integración con datos de muestra (`skipif` si faltan en el scratchpad).

---

## Scripts originales del PoC (históricos)

Lo que sigue documenta los scripts monolíticos originales (`generate_spec_poc.py`,
`spec_builder.py`, `catalog_extender.py`, `piping_class_reader.py`), de los que se portó la lógica
validada a `specgen/`. Se conservan como referencia.

## Ejecución

```bash
python generate_spec_poc.py
```

No requiere argumentos. Lee los ficheros de entrada en **modo solo lectura** y escribe la salida en
`out/`. Las rutas de entrada están como constantes al inicio del script
(`TEMPLATE_PSPC`, `TEMPLATE_PSPX`, `SOURCE_PCAT`).

## Ficheros generados (en `out/`)

| Fichero | Track | Qué es |
|---|---|---|
| `POC-PIPE.pspc` / `POC-PIPE.pspx` | A | Spec recortada: SOLO la familia tubería (clase `Pipe`) de la plantilla NXD-2. |
| `POC-PIPE-FROMCAT.pspc` / `POC-PIPE-FROMCAT.pspx` | B | Spec con 4 tuberías seleccionadas del catálogo `REPSOL_TUBERIA.pcat`. |

## Mecanismo

Tanto `.pspc` como `.pcat` son bases **SQLite** con esquema idéntico. Una spec es esencialmente una
**copia selectiva** del catálogo. La tabla central es `EngineeringItems`; el grafo de cada
componente es:

```
PnPBase(class='Pipe')  ──┐
EngineeringItems         ├─ mismo PnPID (espacio de entidades, contador PnPSys_PnPBase_PnPID)
PipeRunComponent         │
Pipe                   ──┘
        │
        │  PartPort.Part = PnPID componente
        ▼
PartPort (PnPID propio, espacio de relaciones, contador PnPSys_RelationshipSystem_PnPID;
          NO está en PnPBase)
        │  PartPort.Port = PnPID del Port
        ▼
Port  ── tiene fila propia en PnPBase(class='Port')

PnPRowRelations(ROWID = PnPID componente, RELID = PnPID PartPort, 'PartPort')
```

### Track A — subsetting (prioritario)

1. Copia la plantilla `NXD-2.pspc` (hereda **intactas** las tablas de metadatos de esquema:
   `PnPTables`, `PnPProperties`, `PnPColumnAttributes`, `PnPTableAttributes`,
   `PnPRelationshipTypes`, etc., que deben coincidir con la versión de Plant 3D).
2. Sobre la copia, **borra** todo lo que no sea tubería: vacía las tablas de los demás tipos
   (`Elbow`, `Tee`, `Valve`, `Flange`, `BoltSet`, `Gasket`, ...), recorta
   `PnPBase`/`EngineeringItems`/`PipeRunComponent`/`Pipe`/`Port`/`PartPort`/`PnPRowRelations` a
   solo los 18 componentes `Pipe` y su grafo, y vacía `LookUps`.
3. Asigna **identidad nueva**: `RepositoryDescriptor.Name='POC-PIPE'`, `RepositoryID` nuevo,
   `PnPDatabase.DBID` nuevo. `VACUUM`.
4. Genera el `.pspx` (ver abajo).

### Track B — sourcing desde el catálogo (stretch)

Igual partida (copia + vaciado total del grafo) y luego **inserta** filas seleccionadas del
`.pcat` por familia + diámetro nominal (familia `PIPE, SEAMLESS, PE, ASME B36.10`, diámetros
0.5/0.75/1.0/2.0"), copiando los BLOB de geometría/GUID **tal cual** del catálogo y construyendo
PnPIDs/PnPGuid nuevos consistentes para componente, Port, PartPort y la relación.

### El paquete `.pspx` (ZIP/OPC)

Se copia el ZIP de la plantilla y se ajustan tres partes:

- `_rels/.rels`: el `Relationship` de tipo `Plant/Specification/Data` se reapunta al nuevo `.pspc`
  (`TargetMode="External"`).
- `content/PartUsePriorities.xml`: se filtran las `PartTypeUsePriority` para conservar solo
  `PartType == Pipe`.
- `content/branchtable.xml`: se emite **vacío** (`BranchSymbols` y `Branches` sin hijos), porque
  las ramas mezclan tipos de pieza que ya no existen.

El resto de partes (`[Content_Types].xml`, `editor/CatalogReferences.xml`, `SpecNotes.xml`,
`SpecSheetSettings.xml`) se copian intactas.

## Supuestos documentados

- **Orden de bytes de los GUID:** los GUID se guardan como BLOB de 16 bytes en orden .NET. La forma
  de texto que aparece en el `.pspx` corresponde a `uuid.UUID(bytes_le=blob)`
  (verificado: `71f80a8d...` → `8d0af871-e5d2-400f-...`). Los GUID nuevos se generan con
  `uuid.uuid4().bytes_le`. `RepositoryID` es **texto** con llaves `{guid}`.
- **PnPTimestamp = ticks .NET:** intervalos de 100 ns desde `0001-01-01`
  (`int((dt - datetime(1,1,1)).total_seconds() * 1e7)`).
- **Espacios de PnPID separados:** las entidades (`PnPBase`: componentes y ports) y las relaciones
  (`PartPort`) usan contadores distintos; pueden coincidir numéricamente. En Track B los nuevos
  PnPID arrancan por encima del máximo que la plantilla usó nunca (`sqlite_sequence`).
- **Branch table vacío** en el subset (decisión de robustez).
- **Track B**: `CatalogPartFamilyId` del spec = `PartFamilyId` del catálogo; se copian los BLOB de
  geometría (`ContentGeometry*`) y los GUID tal cual.

## Verificación programática (la hace el script, NO sustituye a Plant 3D)

Para cada `.pspc` generado, `verify()` comprueba e imprime:

- `PRAGMA integrity_check` → **ok** en ambos.
- Recuentos por tabla; las tablas de tipos no-Pipe quedan a 0.
- `EngineeringItems` contiene **solo** componentes de clase `Pipe`.
- Todos los GUID son BLOB de exactamente 16 bytes (Track A: 128 OK / 0 fallos; Track B: 30 / 0).
- **Grafo consistente** (sin huérfanos): `PartPort.Part/Port` existen, `PnPRowRelations` apunta a
  componente + PartPort válidos, cada `Port` tiene su `PartPort`.
- El `.pspx` abre como ZIP, sus 7 partes XML parsean, y el Data target apunta al nuevo `.pspc`.

> **No afirmamos que el fichero sea válido para Plant 3D**: no disponemos de Plant 3D en este
> entorno. La validez final solo se confirma abriéndolo en el Spec Editor.

## CHECKLIST de validación manual (AutoCAD Plant 3D — Spec Editor)

1. Copia `out/POC-PIPE.pspc` y `out/POC-PIPE.pspx` a la misma carpeta (el `.pspx` referencia al
   `.pspc` por nombre relativo).
2. Abre `POC-PIPE.pspx` en el **Spec Editor**. Debe cargar **sin errores**.
3. Comprueba que en la lista de piezas aparece **solo la clase `Pipe`** con sus tamaños
   (18 entradas de la familia "TUBO, SIN SOLDADURA").
4. Revisa que las propiedades (diámetro nominal, schedule, descripción, geometría) se ven correctas.
5. Genera/visualiza la **Spec Sheet** y confirma que no salta ningún aviso de integridad.
6. (Opcional) Asocia la spec a un proyecto de prueba y comprueba que se puede **enrutar tubería**
   con ella en el modelo 3D.
7. Repite 2–5 con `out/POC-PIPE-FROMCAT.pspx` (4 tuberías `PIPE, SEAMLESS, PE, ASME B36.10`,
   diámetros 0.5/0.75/1/2"). Verifica que las referencias al catálogo `REPSOL - TUBERÍA` resuelven.

Reporta cualquier error de carga (mensaje exacto) para iterar sobre el script.
