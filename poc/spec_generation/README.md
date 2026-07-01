# specgen — autoría de especificaciones de AutoCAD Plant 3D (`.pspc` + `.pspx`)

Herramienta que **autora** un fichero de especificación de Plant 3D desde Python puro (solo
librería estándar: `sqlite3`, `zipfile`, `uuid`, `xml.etree.ElementTree`, `datetime`, `shutil`)
más `openpyxl` para leer el piping class. Sin más dependencias.

> **Ubicación (desde 2026-07):** el paquete se promovió de este PoC al servidor MCP y vive en
> **`src/autocad_mcp/specgen/`** (importable como `autocad_mcp.specgen`). Este directorio
> (`poc/spec_generation/`) solo conserva ahora las salidas de ejemplo en `out/` y esta nota. La
> lógica de generación está expuesta como **tool MCP `specgen`** y sigue disponible como CLI.

---

## Uso como tool MCP (recomendado)

Tool `specgen` en `src/autocad_mcp/server.py` (`readOnlyHint: False`). Tres operaciones, `data`
con rutas absolutas:

| Operation | Qué hace | `data` |
|---|---|---|
| `analyze` | **Solo análisis** (no construye spec). Parsea el Excel, empareja y devuelve cobertura por confianza, recuentos por familia y la **lista de huecos** (piezas sin match). Con `out`, escribe además `REVISION_MATCHING.xlsx`. | `{piping_class, catalogs, out?, extend_h2?}` |
| `build` | **Pipeline completo**: parsea, (amplía `-H2`), empareja, escribe el informe, construye `<spec_name>.pspc`/`.pspx` y verifica (integrity + grafo + `.pspx` ZIP/XML). | `{piping_class, catalogs, out, spec_name?, extend_h2?, template_pspc?}` |
| `extend_catalog` | Crea **solo** las variantes `-H2` en copias de los catálogos bajo `out/catalogs`. | `{piping_class, catalogs, out}` |

Devuelve JSON con `ok`, cobertura y (en `build`) las rutas de los ficheros generados + el resumen
de verificación. Validación de parámetros con mensajes en español (fichero/carpeta inexistente,
parámetros ausentes) devueltos como `{"error": "..."}`, sin lanzar excepción.

## Uso como CLI

```bash
python -m autocad_mcp.specgen build \
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

Tanto la CLI como la tool comparten la MISMA orquestación en `autocad_mcp.specgen.api`
(`analyze` / `build` / `extend_catalog`); no hay lógica duplicada.

### Salidas (en `--out` / `out`)

| Fichero | Qué es |
|---|---|
| `<NOMBRE>.pspc` / `.pspx` | La especificación generada (SQLite + paquete ZIP/OPC). |
| `REVISION_MATCHING.xlsx` | Informe de revisión: una fila por entrada, ordenado **dudoso primero** (BAJA → MEDIA → SUSTITUCION → ALTA), con la familia de catálogo elegida, los candidatos alternativos y el estado. |
| `catalogs/` | Solo con `--extend-h2` / `extend_h2`: las **copias** de los catálogos ampliadas con las familias `-H2` (los originales NO se tocan). |

### Niveles de confianza (señal de revisión)

`ALTA` (familia única e inequívoca) · `MEDIA` (casa pero ambiguo: varias familias compiten, falta una
señal discriminante, o un retry sin filtro de diámetro resolvió la familia —p. ej. espárragos cuyo
diámetro de tornillo difiere del `NominalDiameter` del catálogo—) · `SUSTITUCION` (variante `-H2`
resuelta a su familia base) · `BAJA` (sin L-code, L-code ausente de todo catálogo, o sin fila). El
ingeniero confía en `ALTA` y revisa lo demás en `REVISION_MATCHING.xlsx`.

### Módulos (`src/autocad_mcp/specgen/`)

`common` (primitivas GUID/ticks, texto, parseo numérico) · `piping_class` (parser del Excel) ·
`catalog_index` (descubre e indexa `.pcat`) · `matcher` (matching con confianza) ·
`spec_builder` (materializa la `.pspc`/`.pspx`) · `catalog_extender` (clona familias `-H2`) ·
`report` (`REVISION_MATCHING.xlsx` + cobertura) · `api` (orquestación reutilizable) ·
`cli` (`python -m autocad_mcp.specgen build`).

### Tests

```bash
python -m pytest tests/specgen/ -q
```

Unitarios (sin ficheros) + integración con datos de muestra (`skipif` si faltan en el scratchpad)
+ el dispatch de la tool MCP `specgen`.

---

## Mecanismo (referencia técnica)

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

`specgen` parte de una plantilla `.pspc` (o del primer catálogo) heredando **intactas** las tablas
de metadatos de esquema (`PnPTables`, `PnPProperties`, `PnPColumnAttributes`, `PnPTableAttributes`,
`PnPRelationshipTypes`, ...), vacía el grafo y **materializa** las piezas elegidas por el matcher,
copiando los BLOB de geometría/GUID del catálogo y construyendo PnPIDs/PnPGuid nuevos consistentes
para componente, Port, PartPort y la relación.

### El paquete `.pspx` (ZIP/OPC)

- `_rels/.rels`: el `Relationship` de tipo `Plant/Specification/Data` se reapunta al nuevo `.pspc`
  (`TargetMode="External"`).
- `content/branchtable.xml`: se toma de la plantilla (`--template-pspc` → su `.pspx` hermano) o se
  emite vacío.
- El resto de partes (`[Content_Types].xml`, `editor/CatalogReferences.xml`, `SpecNotes.xml`,
  `SpecSheetSettings.xml`) se copian.

### Supuestos documentados

- **Orden de bytes de los GUID:** BLOB de 16 bytes en orden .NET. La forma de texto del `.pspx`
  corresponde a `uuid.UUID(bytes_le=blob)`. Los GUID nuevos se generan con `uuid.uuid4().bytes_le`.
  `RepositoryID` es **texto** con llaves `{guid}`.
- **PnPTimestamp = ticks .NET:** intervalos de 100 ns desde `0001-01-01`.
- **Espacios de PnPID separados:** entidades (`PnPBase`) y relaciones (`PartPort`) usan contadores
  distintos; pueden coincidir numéricamente.

## Verificación programática (la hace la herramienta, NO sustituye a Plant 3D)

Para cada `.pspc` generado, `verify()` comprueba: `PRAGMA integrity_check`, recuentos por clase,
todos los GUID BLOB de exactamente 16 bytes, **grafo consistente** (sin huérfanos), y que el `.pspx`
abre como ZIP con todas sus partes XML parseables y el Data target apuntando al nuevo `.pspc`.

> **No afirmamos que el fichero sea válido para Plant 3D**: la validez final solo se confirma
> abriéndolo en el Spec Editor. Reporta cualquier error de carga (mensaje exacto) para iterar.
