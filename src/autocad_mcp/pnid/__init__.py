"""Extraccion de line-list desde P&IDs en PDF (texto vectorial, sin OCR).

Reconoce los tokens de linea de dos familias de naming (legacy Repsol y codificada por
planta) a partir de la capa de texto vectorial del PDF, sin OCR. Ver README.md.

Es la tool MCP ``pnid`` (solo lectura, EXTRAE datos de P&IDs existentes en PDF), distinta de
la tool ``pid`` (que DIBUJA/inserta simbolos P&ID en AutoCAD).
"""

from __future__ import annotations

from .api import extract_line_list

__all__ = ["patterns", "extract", "report", "api", "extract_line_list"]
