"""PyMuPDF compatibility shim.

Tries the canonical ``pymupdf`` package name (PyMuPDF >= 1.24) first and falls
back to the legacy ``fitz`` alias so callers never need their own try/except.

All project code that needs PyMuPDF must import from here rather than writing
its own try/except or importing ``fitz`` directly.
"""
from __future__ import annotations

try:
    import pymupdf as fitz  # type: ignore[import]  # PyMuPDF >= 1.24
except ImportError:
    try:
        import fitz  # type: ignore[import]  # legacy alias kept for <1.24
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF is required. Install it with: pip install pymupdf"
        ) from exc

__all__ = ["fitz"]
