"""Unit tests for :mod:`specgen.common` -- no files required."""

from __future__ import annotations

import uuid

import pytest

from specgen import common


# --------------------------------------------------------------------------- diameters / fractions
@pytest.mark.parametrize("value,expected", [
    (2, 2.0),
    (1.5, 1.5),
    ('2"', 2.0),
    ("3/4", 0.75),
    ("1 1/2", 1.5),
    ("1 1/4", 1.25),
    ("0.5", 0.5),
    (None, None),
    ("", None),
    ("abc", None),
])
def test_parse_diameter(value, expected):
    assert common.parse_diameter(value) == expected


@pytest.mark.parametrize("value", ["1/0", "1 1/0", "5/0"])
def test_parse_diameter_zero_denominator_is_safe(value):
    # H6: a zero denominator must not raise, returns None.
    assert common.parse_diameter(value) is None


# --------------------------------------------------------------------------- normalisation
def test_norm_strips_accents_lowercases_collapses():
    assert common.norm("  ESPÁRRAGO   Y  Tuercas ") == "esparrago y tuercas"
    assert common.norm(None) == ""


def test_norm_schedule():
    assert common.norm_schedule("SCH 80") == "80"
    assert common.norm_schedule("160") == "160"
    assert common.norm_schedule("80.0") == "80"
    assert common.norm_schedule("STD") == "STD"
    assert common.norm_schedule("-") is None
    assert common.norm_schedule(None) is None


def test_norm_rating():
    assert common.norm_rating("600 #") == "600"
    assert common.norm_rating("6000#") == "6000"
    assert common.norm_rating(None) is None
    assert common.norm_rating("RF") is None


def test_xml_escape():
    assert common.xml_escape('a&b<c>"d') == "a&amp;b&lt;c&gt;&quot;d"
    assert common.xml_escape(None) == ""


# --------------------------------------------------------------------------- L/H codes
@pytest.mark.parametrize("text,expected", [
    ("PIPE ... L-1276", "L-1276"),
    ("FLANGE L-453-H2 RF", "L-453-H2"),
    ("STUD H-291", "H-291"),
    ("no code here", None),
    (None, None),
])
def test_extract_lcode(text, expected):
    assert common.extract_lcode(text) == expected


def test_variant_detection_and_base():
    assert common.is_variant_code("L-1276-H2") is True
    assert common.is_variant_code("L-1276") is False
    assert common.base_lcode("L-1276-H2") == "L-1276"
    assert common.base_lcode("L-1276") == "L-1276"
    assert common.base_lcode(None) is None


def test_lcode_in_desc_rejects_longer_number_and_variant():
    assert common.lcode_in_desc("FAMILY L-453 SW", "L-453") is True
    assert common.lcode_in_desc("FAMILY L-4530 SW", "L-453") is False   # longer number
    assert common.lcode_in_desc("FAMILY L-453-H2 SW", "L-453") is False  # variant, not base


# --------------------------------------------------------------------------- GUID round trip
def test_guid_blob_round_trip_is_bytes_le():
    blob = common.new_guid_blob()
    assert isinstance(blob, bytes) and len(blob) == 16
    text = common.blob_to_guid_text(blob)
    # bytes_le round trip: rebuilding from the text via bytes_le reproduces the blob.
    assert uuid.UUID(text).bytes_le == blob


def test_new_repository_id_is_braced_guid():
    rid = common.new_repository_id()
    assert rid.startswith("{") and rid.endswith("}")
    uuid.UUID(rid[1:-1])   # parses


def test_now_ticks_is_positive_int():
    t = common.now_ticks()
    assert isinstance(t, int) and t > 0
