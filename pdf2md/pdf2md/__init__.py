"""pdf2md — Convert PDF documents into clean, structured Markdown for LLMs."""

from pdf2md.backends import get_backend, get_best_available, list_available
from pdf2md.classifier import PDFInfo, classify_pdf
from pdf2md.models import ConversionResult, ValidationReport
from pdf2md.pipeline import Pipeline
from pdf2md.postprocess import postprocess
from pdf2md.validation import validate

__version__ = "0.1.0"

__all__ = [
    "Pipeline",
    "ConversionResult",
    "ValidationReport",
    "PDFInfo",
    "classify_pdf",
    "validate",
    "postprocess",
    "list_available",
    "get_backend",
    "get_best_available",
    "__version__",
]
