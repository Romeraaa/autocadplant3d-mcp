"""Tests deterministas de ``patterns.py`` con tokens reales de los P&IDs Repsol.

No requieren PyMuPDF ni PDFs: alimentan tokens observados y verifican el parseo a campos y
que el ruido NO se reconoce como linea.
"""

from __future__ import annotations

import pytest

from autocad_mcp.pnid import patterns as P


# (token, familia, diameter, service/fluid, area, number, clase)
LEGACY_CASES = [
    ('2"-H2-PUROS', "legacy", '2"', "H2", None, None, None),
    ('8"-H2-REFINERIA', "legacy", '8"', "H2", None, None, None),
    ('3"-H2-ENFERSA', "legacy", '3"', "H2", None, None, None),
    ('4"-H2-A.U.', "legacy", '4"', "H2", None, None, None),
    ('2"-HIDROGENO', "legacy", '2"', "HIDROGENO", None, None, None),
    ('6"-HIDROGENO', "legacy", '6"', "HIDROGENO", None, None, None),
    ('8"-GAS', "legacy", '8"', "GAS", None, None, None),
    ('3"-VAPORES', "legacy", '3"', "VAPORES", None, None, None),
    ('8"-PROD.FG', "legacy", '8"', "PROD.FG", None, None, None),
    ('2"EVACUACION', "legacy", '2"', "EVACUACION", None, None, None),  # sin guion
    ('4"COLECTOR', "legacy", '4"', "COLECTOR", None, None, None),      # sin guion
]

CODED_CASES = [
    ('C29-2"-P-1026', "coded", '2"', "P", "C29", "1026", None),
    ('C29-6"P-1027', "coded", '6"', "P", "C29", "1027", None),          # diametro pegado a fluido
    ('C29-4"P-1030', "coded", '4"', "P", "C29", "1030", None),
    ('C29-3"P-0460', "coded", '3"', "P", "C29", "0460", None),
    ('C10-3"H-00013-C2', "coded", '3"', "H", "C10", "00013", "C2"),
    ('C10-4"H-00016-C2', "coded", '4"', "H", "C10", "00016", "C2"),
    ('C10-3"H-00001-C12', "coded", '3"', "H", "C10", "00001", "C12"),
    ('8"H6-1001-CK1', "coded", '8"', "H6", None, "1001", "CK1"),         # sin area
    ('C29-2"-P-0813-HD4-H', "coded", '2"', "P", "C29", "0813", "HD4-H"),  # doble clase
    ('465-2"-P-0003-D1', "coded", '2"', "P", "465", "0003", "D1"),        # area numerica
    ('8"P-0120-B5-P', "coded", '8"', "P", None, "0120", "B5-P"),
    ('C29-4"-CWR-0835-B1', "coded", '4"', "CWR", "C29", "0835", "B1"),    # fluido 3 letras
    ('C43-424-6"P-00503-D8H2', "coded", '6"', "P", "C43-424", "00503", "D8H2"),  # doble area
    ('3/4"-OW-0848-B1', "coded", '3/4"', "OW", None, "0848", "B1"),       # fraccion ASCII
    ('C29-1½"-OW-0819-HD4', "coded", '1½"', "OW", "C29", "0819", "HD4"),  # fraccion unicode
]

NOISE_TOKENS = [
    "KG/CM2",
    "FG-180#",
    "H.8/17",
    "32248005.dgn",
    "P&IDS.ING0050-19",
    "20.893.REPSOL",
    # tags de equipo / instrumento / referencia -> NO son lineas
    "460-F-2",
    "H-637-E",
    "617-K-1",
    "FCV-202",
    # specs de brida -> NO son lineas
    '2"150#RF',
    '1"300#RF',
    '2"-300#-RF',
    # reduccion de diametro suelta (sin fluido/numero) -> no es un line-id completo
    '10"x8"',
    '2"x1½"',
]


@pytest.mark.parametrize("token,family,diameter,service,area,number,clase", LEGACY_CASES)
def test_legacy_parse(token, family, diameter, service, area, number, clase):
    m = P.parse_line(token)
    assert m is not None, f"deberia reconocerse: {token!r}"
    assert m.family == family
    assert m.diameter == diameter
    assert m.service == service
    assert m.area == area


@pytest.mark.parametrize("token,family,diameter,fluid,area,number,clase", CODED_CASES)
def test_coded_parse(token, family, diameter, fluid, area, number, clase):
    m = P.parse_line(token)
    assert m is not None, f"deberia reconocerse: {token!r}"
    assert m.family == family
    assert m.diameter == diameter
    assert m.service == fluid
    assert m.area == area
    assert m.number == number
    assert m.clase == clase


@pytest.mark.parametrize("token", NOISE_TOKENS)
def test_noise_not_line(token):
    assert P.parse_line(token) is None, f"NO deberia reconocerse como linea: {token!r}"


def test_line_id_is_full_token():
    m = P.parse_line('C29-2"-P-0813-HD4-H')
    assert m.line_id == 'C29-2"-P-0813-HD4-H'


def test_coverage_candidate():
    assert P.is_coverage_candidate("460-F-2") is True       # >=6, letra+digito
    assert P.is_coverage_candidate("FCV-2") is False        # <6
    assert P.is_coverage_candidate("HIDROGENO") is False    # sin digito
    assert P.is_coverage_candidate("KG/CM2") is False       # ruido conocido


def test_instrument_bonus():
    assert P.parse_instrument("FCV-202")["func"] == "FCV"
    assert P.parse_instrument("619-FCV-8")["func"] == "FCV"


def test_reduction_diameter():
    m = P.parse_line('C29-3"x2"-P-1099')
    assert m is not None
    assert m.diameter == '3"x2"'
