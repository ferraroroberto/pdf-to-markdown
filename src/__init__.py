"""PDF to Markdown conversion — clean, structured output for LLMs."""

from src.backends import get_backend, get_best_available, list_available
from src.classifier import PDFInfo, classify_pdf
from src.models import ConversionResult, ValidationReport
from src.pipeline import Pipeline
from src.postprocess import postprocess
from src.validation import validate

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
