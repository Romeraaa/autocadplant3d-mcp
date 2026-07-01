"""Patrones (regex) y parsers para reconocer tokens de linea en P&IDs Repsol.

Cada token de linea es auto-contenido (una sola "word" del PDF); no hay que reconstruirlo
uniendo palabras. Aqui se definen las dos familias de naming observadas en los P&IDs legacy
de Repsol Cartagena y su parseo a campos estructurados.

Familias soportadas
-------------------
* **legacy**  -> ``<diametro>-<servicio>[-<nombre>]``           p.ej. ``2"-H2-PUROS``, ``6"-HIDROGENO``,
  ``8"-GAS``, ``3"-VAPORES`` (el servicio es texto alfabetico, no solo H2).
* **coded**   -> ``[<area>-]<diametro>[-]<fluido>-<numero>[-<clase>...]``
  p.ej. ``C29-2"-P-1026``, ``C29-6"P-1027``, ``8"H6-1001-CK1``,
  ``C29-2"-P-0813-HD4-H``, ``465-2"-P-0003-D1`` (numero + una o varias clases).

Notas de tolerancia
-------------------
* La comilla del diametro puede venir como ``"`` (comilla recta ASCII).
* El diametro admite enteros (``6"``), fracciones (``3/4"``, ``1/2"``), mixtos (``1�"``) y
  reducciones (``3"x2"``, ``2"x1�"``).
* La familia codificada a veces pega el diametro al fluido (``C29-6"P-1027`` sin guion antes de P)
  y puede llevar varios segmentos de clase (``...-HD4-H``).
* La fraccion 1/2 aparece como glifo roto ``�"`` (U+FFFD). Se tolera dentro del diametro
  pero se marca como limitacion conocida (ver README).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Piezas base
# ---------------------------------------------------------------------------

# Glifos de fraccion vulgar que aparecen en los PDF: ½ ¾ (Unicode real) y � (U+FFFD, glifo
# roto que a veces sustituye a la fraccion 1/2 segun la fuente del PDF).
_FRAC = "½¾�"
# Un valor de diametro simple, seguido de comilla. Cubre:
#   entero 6"  fraccion ASCII 3/4"  mixto 1½"  solo fraccion ½"  glifo roto �"
_DIAM_ONE = rf'(?:\d+/\d+|\d+[{_FRAC}]|\d+|[{_FRAC}])"'
# Diametro completo: uno o una reduccion "A"xB"" (x o X). Ejemplos: 6"  3"x2"  2"x1½"  10"X8"
_DIAM = rf"{_DIAM_ONE}(?:[xX]{_DIAM_ONE})?"

# Servicio legacy: texto alfabetico (con . y /), p.ej. H2, HIDROGENO, GAS, VAPORES, PROD.FG.
# Debe contener al menos una letra para no confundir con numeros sueltos.
_LEGACY_SERVICE = r"[A-Z][A-Z0-9./]*"

# Fluido de la familia codificada: 1-3 letras + digito opcional
# (P, H, OW, FL, AN, CWR, CWS, H6, ...).
_CODED_FLUID = r"[A-Z]{1,3}\d?"

# Numero de linea codificada: 3+ digitos (0460, 1026, 00013, ...).
_CODED_NUMBER = r"\d{3,}"

# Segmento de clase: alfanumerico corto (B5, HD4, CK1, C12, D1, D8H2, ST., ...). Puede repetirse.
# Admite punto final (ST.) y un sufijo entre parentesis en el ultimo tramo (p.ej. D8(H2)).
_CODED_CLASS = r"[A-Z0-9]{1,5}\.?(?:\([A-Z0-9]+\))?"

# Segmento de area: letra + digitos (C29, C10, C43, ...) o prefijo numerico de planta (424, 465).
_AREA_SEG = r"[A-Z]\d{1,3}|\d{2,3}"
# Area completa: uno o dos segmentos (p.ej. C43-424, C43-465). Se captura en bruto.
_AREA = rf"(?:{_AREA_SEG})(?:-(?:{_AREA_SEG}))?"


# ---------------------------------------------------------------------------
# Regex por familia (anclados: el token completo debe casar)
# ---------------------------------------------------------------------------

# legacy: 2"-H2-PUROS | 6"-HIDROGENO | 8"-GAS | 3"-VAPORES | 8"-PROD.FG | 4"-H2-A.U. |
#         2"EVACUACION | 4"COLECTOR (diametro pegado al servicio, sin guion)
# Diametro + servicio alfabetico + nombre opcional. El servicio arranca con letra (evita
# que las lineas codificadas, que arrancan area/diametro, caigan aqui). El separador entre
# diametro y servicio puede faltar; en ese caso se exige servicio de 3+ letras para no
# confundir con specs de brida tipo 2"150#RF (que ademas llevan '#', excluido del servicio).
RE_LEGACY = re.compile(
    rf"^(?P<diameter>{_DIAM})"
    rf"(?:-(?P<service>{_LEGACY_SERVICE})|(?P<service2>[A-Z]{{3,}}[A-Z0-9./]*))"
    rf"(?:-(?P<name>[A-Z0-9./\-]+))?$"
)

# coded: C29-2"-P-1026 | C29-6"P-1027 | C10-3"H-00013-C2 | 8"H6-1001-CK1 (sin area) |
#        C29-2"-P-0813-HD4-H | 465-2"-P-0003-D1 | 8"P-0120-B5-P
# - area opcional (algunos tokens empiezan directo por diametro).
# - separador entre diametro y fluido "-" o nada.
# - numero de 3+ digitos.
# - una o varias clases al final (capturadas en bruto en 'clase').
RE_CODED = re.compile(
    rf"^(?:(?P<area>{_AREA})-)?"
    rf"(?P<diameter>{_DIAM})-?(?P<fluid>{_CODED_FLUID})"
    rf"-(?P<number>{_CODED_NUMBER})"
    rf"(?:-(?P<clase>{_CODED_CLASS}(?:-{_CODED_CLASS})*))?$"
)


# ---------------------------------------------------------------------------
# Bonus: instrumentos y equipos (tablas aparte, no forman parte del line-list)
# ---------------------------------------------------------------------------

# Instrumentos: FCV-202, FCV-15, PCV-2A, PV-203, PSV-0502, 465PV-0503, 619-FCV-8
RE_INSTRUMENT = re.compile(
    r"^(?:(?P<prefix>\d{1,3})-?)?(?P<func>[A-Z]{2,4})-(?P<number>\d{1,4}[A-Z]?)$"
)

# Equipos: 460-F-2, H-637-E, 617-K-1, 611-D-4, 681-D-104, 419-E-01
RE_EQUIPMENT = re.compile(
    r"^(?P<a>[A-Z0-9]{1,4})-(?P<b>[A-Z0-9]{1,4})-(?P<c>[A-Z0-9]{1,4})$"
)


# ---------------------------------------------------------------------------
# Ruido: tokens que NO deben reconocerse como linea aunque parezcan alfanumericos.
# ---------------------------------------------------------------------------

RE_NOISE = re.compile(
    r"""^(
        KG/CM2                 # unidades de presion
        | FG-\d+\#             # FG-180#
        | SUB\.N.*             # SUB.N.14 (con glifo roto)
        | H\.\d+/\d+           # H.8/17
        | .*\.dgn              # nombres de fichero dgn
        | .*\.pdf
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class LineMatch:
    """Resultado del parseo de un token de linea a campos estructurados."""

    line_id: str
    family: str                       # "legacy" | "coded"
    diameter: str | None = None
    service: str | None = None        # servicio/fluido (H2, HIDROGENO, P, H, H6, ...)
    area: str | None = None
    number: str | None = None
    clase: str | None = None
    name: str | None = None
    extra: dict = field(default_factory=dict)


def is_noise(token: str) -> bool:
    """True si el token es ruido conocido a ignorar."""
    return bool(RE_NOISE.match(token.strip()))


def parse_line(token: str) -> LineMatch | None:
    """Parsea un token a :class:`LineMatch` si casa alguna familia de linea; si no, None.

    Prueba la familia codificada primero (mas especifica) y luego la legacy.
    """
    tok = token.strip()
    if not tok or is_noise(tok):
        return None

    m = RE_CODED.match(tok)
    if m:
        return LineMatch(
            line_id=tok,
            family="coded",
            diameter=m.group("diameter"),
            service=m.group("fluid"),
            area=m.group("area"),
            number=m.group("number"),
            clase=m.group("clase"),
        )

    m = RE_LEGACY.match(tok)
    if m:
        return LineMatch(
            line_id=tok,
            family="legacy",
            diameter=m.group("diameter"),
            service=m.group("service") or m.group("service2"),
            name=m.group("name"),
        )

    return None


def is_coverage_candidate(token: str) -> bool:
    """True si el token es un candidato "largo" para el bucket de cobertura.

    Alfanumerico de longitud >= 6 con al menos un digito y una letra, que no sea ruido.
    Se usa para medir cobertura (reconocidos / candidatos) y listar los no reconocidos.
    """
    tok = token.strip()
    if len(tok) < 6 or is_noise(tok):
        return False
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    return has_digit and has_alpha


def parse_instrument(token: str):
    """Bonus: parsea un token de instrumento; devuelve dict o None."""
    tok = token.strip()
    if is_noise(tok):
        return None
    m = RE_INSTRUMENT.match(tok)
    if not m:
        return None
    return {"tag": tok, "prefix": m.group("prefix"), "func": m.group("func"), "number": m.group("number")}


def parse_equipment(token: str):
    """Bonus: parsea un token de equipo; devuelve dict o None.

    Heuristica laxa (3 segmentos A-B-C); puede solapar con instrumentos, por eso
    conviene aplicarlo solo a tokens que NO casaron instrumento/linea.
    """
    tok = token.strip()
    if is_noise(tok):
        return None
    m = RE_EQUIPMENT.match(tok)
    if not m:
        return None
    return {"tag": tok, "a": m.group("a"), "b": m.group("b"), "c": m.group("c")}
