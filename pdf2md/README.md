# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

## Why?

PDF is a presentation format, not a semantic format. When PDFs are passed directly to LLMs, the extracted content is often incorrect — tables are mangled, reading order is wrong, structure is flattened, and headers/footers waste tokens. `pdf2md` solves this with a preprocessing pipeline that produces clean, deterministic Markdown.

## Architecture

```
PDF Input
    │
    ├─► Classify (born-digital vs scanned)
    ├─► Extract via backend (Marker / Docling / pdfplumber)
    ├─► Post-process (clean headers/footers, normalize, fix paragraphs)
    ├─► Validate (char similarity, structural checks, table integrity)
    │
    └─► Markdown Output + ValidationReport + token estimate
```

## Installation

```bash
# Base install (pdfplumber backend only — no ML dependencies)
pip install pdf2md

# With Marker backend (ML-powered, best general-purpose)
pip install "pdf2md[marker]"

# With Docling backend (IBM, strong on tables/patents)
pip install "pdf2md[docling]"

# All backends
pip install "pdf2md[all]"

# Development
pip install "pdf2md[dev]"
```

## Quick Start

### Python API

```python
from pdf2md import Pipeline

# Basic conversion
result = Pipeline().convert("document.pdf")
print(result.markdown[:500])
print(f"~{result.token_estimate:,} tokens")

# With validation
result = Pipeline().convert("document.pdf", validate_output=True)
print(result.validation.summary())

# Save to file
result.save("output/document.md")
```

### CLI

```bash
# Convert a single PDF
pdf2md convert document.pdf -o output/

# Convert with validation
pdf2md convert document.pdf -o output/ --validate

# Convert a directory
pdf2md convert input_pdfs/ -o output_markdown/ --workers 4

# Choose a specific backend
pdf2md convert document.pdf -b pdfplumber -o output/

# List available backends
pdf2md backends

# Validate an existing conversion
pdf2md validate document.pdf output/document.md
```

## Backends

| Backend    | Type        | Scanned PDFs | Dependencies     |
|------------|-------------|--------------|------------------|
| Marker     | ML-powered  | Yes (OCR)    | `marker-pdf`     |
| Docling    | ML-powered  | Yes (OCR)    | `docling`        |
| pdfplumber | Heuristic   | No           | `pdfplumber`     |

The pipeline automatically selects the best available backend. For scanned PDFs, it prefers ML-powered backends with OCR support. For born-digital PDFs, any backend works.

## Post-Processing

All backend output goes through the same cleaning pipeline:

1. **Strip headers/footers** — removes repeated short lines
2. **Strip page numbers** — removes standalone page number lines
3. **Fix broken paragraphs** — joins lines split mid-sentence
4. **Compact tables** — normalizes pipe-delimited table formatting
5. **Normalize whitespace** — collapses excessive blank lines

## Validation

The validation system extracts raw text from the PDF independently and compares it to the stripped Markdown output:

- **Character similarity** via `SequenceMatcher` (target: ≥85%)
- **Structural element counts** (headings, tables, list items)
- **Table row consistency** (column count checks)
- **Content ratio warnings** (detects content loss or duplication)

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
