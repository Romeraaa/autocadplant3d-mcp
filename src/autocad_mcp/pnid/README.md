# Tool `pnid` — Extracción de line-list desde P&IDs en PDF (sin OCR)

Extrae la **line-list** (lista de líneas de tubería) de diagramas P&ID en PDF **leyendo la
capa de texto vectorial** del propio PDF — **sin OCR**. Es la 11ª tool del servidor MCP.

> **No confundir con la tool `pid`.** `pid` **dibuja/inserta** símbolos P&ID en AutoCAD;
> `pnid` **EXTRAE** datos (líneas) de P&IDs existentes en PDF y es de **solo lectura**.

Los PDF de ejemplo son P&IDs legacy de Repsol Cartagena; cada tag de línea es un **token
auto-contenido** en el texto del PDF (no hay que reconstruirlo uniendo palabras por posición).

La lógica vive detrás de una capa `api.py` UI-agnóstica que devuelve un dict JSON-serializable,
compartida por la CLI (`python -m autocad_mcp.pnid`) y por la tool MCP.

## Qué hace

1. Abre cada PDF con **PyMuPDF** (`fitz`) y extrae las *words* (`page.get_text("words")`).
2. Aplica regex de dos familias de naming y **parsea cada token de línea a campos**.
3. **Deduplica** por `(sheet, line_id)` contando ocurrencias.
4. Calcula **cobertura**: candidatos alfanuméricos "largos" reconocidos / totales, y lista los
   **no reconocidos** para revisión del ingeniero.
5. Vuelca a **CSV** y/o **XLSX** (+ `COBERTURA.txt`).

## Familias de línea soportadas

| Familia | Forma | Ejemplos |
|--------|-------|----------|
| **legacy** | `<diámetro>-<servicio>[-<nombre>]` (el servicio es texto: H2, HIDROGENO, GAS, VAPORES, PROD.FG…). Admite diámetro pegado al servicio (`2"EVACUACION`). | `2"-H2-PUROS`, `6"-HIDROGENO`, `8"-GAS`, `4"-H2-A.U.` |
| **coded** | `[<área>[-<área2>]-]<diámetro>[-]<fluido>-<número>[-<clase>…]` | `C29-2"-P-1026`, `C29-6"P-1027`, `8"H6-1001-CK1`, `C29-2"-P-0813-HD4-H`, `465-2"-P-0003-D1`, `C43-424-6"P-00503-D8H2` |

**Diámetro** admite: entero (`6"`), fracción ASCII (`3/4"`), mixto Unicode (`1½"`), reducción
(`3"x2"`, `2"x1½"`) y el glifo roto `�"` (U+FFFD) por tolerancia.

**Campos extraídos** por línea: `line_id` (token completo), `family`, `diameter`, `service`
(servicio/fluido), `area`, `number`, `clase`, `name`, `sheet`, `page`, `x`, `y`, `count`.

## Uso como tool MCP

```jsonc
// Adjunta LINE_LIST.xlsx + COBERTURA.txt al chat (attach_files=True por defecto)
pnid(operation="line_list", data={"dir": "C:/pids"})

// Varios PDFs explícitos, CSV+XLSX, escribiendo a disco
pnid(operation="line_list", data={"pdfs": ["A.pdf", "B.pdf"], "out": "C:/salida", "format": "both"})

// Solo rutas en JSON (sin adjuntar)
pnid(operation="line_list", data={"pdf": "A.pdf", "out": "C:/salida", "attach_files": false})
```

`data`: `{pdf | pdfs | dir, out?, format?(csv|xlsx|both), attach_files?(def True), bonus?}`.

## Uso (CLI)

```bash
python -m autocad_mcp.pnid --pdf A.pdf --pdf B.pdf --out ./salida
python -m autocad_mcp.pnid --dir ./pids --out ./salida --format both
python -m autocad_mcp.pnid --dir ./pids            # solo resumen, sin escribir
python -m autocad_mcp.pnid --dir ./pids --out ./salida --bonus
```

Salida (cuando hay `--out`): `LINE_LIST.csv` y/o `LINE_LIST.xlsx` (hojas *Líneas*,
*NoReconocidos*, *Cobertura*, y *Instrumentos*/*Equipos* con `--bonus`) + `COBERTURA.txt`.

## Resultado sobre los 3 P&IDs de muestra

| Sheet | Líneas únicas | Candidatos | Reconocidos | Cobertura |
|-------|--------------:|-----------:|------------:|----------:|
| 32248004 | 22 | 119 | 22 | 18.5 % |
| 32248005 | 39 | 165 | 55 | 33.3 % |
| 38364008 | 86 | 190 | 95 | 50.0 % |
| **TOTAL** | **147** | **474** | **172** | **36.3 %** |

> La cobertura global no es 100 % **por diseño**: el denominador (tokens alfanuméricos ≥6 con
> letra+dígito) incluye legítimamente muchos elementos que **no son líneas** — tags de equipo
> (`460-F-2`), instrumentos (`FCV-202`), referencias de plano (`PL.Nº38362`), conexiones
> (`CONEX.Nº1`) y specs de brida (`2"150#RF`). El bucket *NoReconocidos* existe precisamente
> para que el ingeniero audite qué queda fuera.

## Limitaciones conocidas

- **Fracción ½/¾**: en estos 3 PDFs PyMuPDF las decodifica como Unicode real (`½`, `¾`) y se
  capturan bien. El regex tolera además el glifo roto `�"` (U+FFFD) por si otras fuentes de PDF
  lo rompen; en ese caso el `line_id` contendría el carácter de reemplazo.
- **Ambigüedad `FG-180#` / servicio FG**: el patrón de ruido descarta los tokens `FG-<n>#`
  (spec de brida FG-180#) para que no cuenten como línea. Como efecto colateral, una línea
  legítima cuyo *servicio* fuera literalmente `FG` seguida de dígitos y `#` no se distinguiría
  de esa spec. En los P&IDs de muestra `PROD.FG` (servicio FG como texto) sí se reconoce; el
  caso ambiguo `FG-<n>#` no aparece como línea real. Pendiente de un token real que lo fuerce.
- **DGN no leído**: solo se procesa el PDF; los `.dgn` de origen (MicroStation) no se abren.
- **Sin geometría / conectividad**: se extrae la etiqueta y su posición (x, y), no la traza de
  la línea ni de/a qué equipos conecta.
- **Bonus best-effort**: instrumentos/equipos usan heurísticas laxas y pueden solaparse; no es
  el núcleo.

## TODO (fuera de alcance)

- Prefijo de anotación pegado al tag: `No:607-3"P-0706-D8-ET` (el `No:` impide el match).
- Reducciones de diámetro sueltas sin fluido/número (`10"x8"`, `2"x1½"`): hoy no se cuentan
  como line-id (no son una línea completa); decidir si interesan.
- Consolidar `service`/`fluid` a un vocabulario controlado (diccionario de servicios).
- Normalizar la fracción rota `�` a `1/2` cuando se confirme el patrón por fuente de PDF.

## Estructura

```
src/autocad_mcp/pnid/
  patterns.py   # regex de las familias + parsers a campos (LineMatch)
  extract.py    # abre PDF con fitz, saca words, aplica patterns, dedup, cobertura
  report.py     # volcado a CSV / XLSX + COBERTURA.txt
  api.py        # capa UI-agnóstica -> dict JSON-serializable (CLI + tool MCP)
  cli.py        # CLI (consumidor delgado de api)
  __main__.py   # python -m autocad_mcp.pnid
```

## Tests

```bash
python -m pytest tests/pnid/ -q
```

Los tests de `patterns.py` no requieren PyMuPDF. Los de `extract.py`/`api.py` generan un PDF
sintético con `fitz`. El smoke test sobre PDFs reales lee la carpeta de la env var
`PNID_SAMPLE_DIR` y se salta si no está definida.
```
