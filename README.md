# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

## Architecture

```
File Input (PDF, Word, PowerPoint, Excel, Image)
    │
    ├─► [Pre-convert] Non-PDF → LibreOffice/PyMuPDF → PDF  (Vertex AI only)
    │
    ├─► Classify (born-digital vs scanned)
    ├─► Split into chunks (optional, configurable page size + overlap)
    │       └─► Per-chunk: Extract → Post-process → (Validate)
    ├─► Merge chunks into final document
    ├─► Log execution row to tmp/exec_log.jsonl
    │
    └─► Markdown Output + ValidationReport + token/cost estimate
```

## Project structure

```
pdf-to-markdown/
├── .venv/                  # Virtual environment
├── app/
│   ├── app.py              # Streamlit entry point (4 tabs)
│   ├── execute.py          # ⚡ Execute tab
│   ├── tab_batch.py        # 📂 Batch tab
│   ├── tab_log.py          # 📊 Log Viewer tab
│   ├── tab_settings.py     # ⚙️ Settings tab
│   └── .streamlit/
├── prompts/
│   ├── extraction.md
│   └── refinement.md
├── src/
│   ├── backends/           # Extraction backends (Vertex AI, Marker, pdfplumber)
│   ├── auth.py             # Authentication factory (api | gcloud)
│   ├── batch.py            # Folder discovery and batch orchestrator
│   ├── chunker.py          # PDF page splitter and markdown merger
│   ├── classifier.py       # Born-digital vs scanned detection
│   ├── cli.py              # CLI entry point
│   ├── config.json         # Runtime configuration (all settings)
│   ├── config.py           # Settings loader / saver
│   ├── file_converter.py   # Pre-conversion: Office/image → PDF (Vertex AI only)
│   ├── logger_exec.py      # Structured JSONL execution logger
│   ├── models.py           # ConversionResult, ChunkResult, BatchResult, ValidationReport
│   ├── pipeline.py         # Single-file orchestrator
│   ├── postprocess.py      # Cleaning pipeline
│   ├── validation.py       # Quality validation
│   └── vertexai_pricing.py # Gemini pricing fetch and cache
├── testing/
│   ├── conftest.py         # Shared fixtures (in-memory PDF/PNG generators)
│   ├── test_config.py      # Config loading, saving, merging
│   ├── test_models.py      # Data models and properties
│   ├── test_postprocess.py # Markdown cleaning pipeline
│   ├── test_file_converter.py  # File type detection, image→PDF conversion
│   ├── test_chunker.py     # PDF splitting and markdown merging
│   └── test_validation.py  # Quality validation helpers
├── tmp/
│   └── exec_log.jsonl      # Persistent execution log (append-only)
├── launch_app.bat
├── requirements.txt
└── README.md
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your credentials:

```bash
PROJECT_ID=my-gcp-project
GOOGLE_API_KEY=your-api-key        # for auth_mode=api
# GOOGLE_APPLICATION_CREDENTIALS= # for auth_mode=gcloud (service account)
```

### CUDA support (GPU acceleration for Marker)

```bash
# CUDA 12.4 (RTX 40 series and older)
.venv\Scripts\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# CUDA 12.8 nightly (RTX 50 series / Blackwell)
.venv\Scripts\python.exe -m pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
```

## Usage

### Web UI (Streamlit)

```bash
.venv\Scripts\streamlit.exe run app/app.py
# or double-click launch_app.bat on Windows
```

**Tabs:**

| Tab | Purpose |
|---|---|
| ⚡ Execute | Single-file conversion with all options |
| 📂 Batch | Folder processing with results table |
| 📊 Log Viewer | Browse and filter `exec_log.jsonl` |
| ⚙️ Settings | Edit `config.json` from the UI |

### CLI

```bash
# Convert a single PDF (uses config.json defaults)
.venv\Scripts\python.exe -m src.cli convert document.pdf -o output/

# Vertex AI with explicit options
.venv\Scripts\python.exe -m src.cli convert document.pdf \
    -b vertexai --auth-mode api \
    --project-id my-proj --model gemini-2.5-pro \
    --refine-iterations 3 -o output/

# Large PDF: split into 10-page chunks with 1-page overlap
.venv\Scripts\python.exe -m src.cli convert bigdoc.pdf \
    --chunk-size 10 --chunk-overlap 1 -o output/

# Batch folder processing
.venv\Scripts\python.exe -m src.cli convert input_pdfs/ -o output/

# Dry run — estimate tokens and cost without calling the API
.venv\Scripts\python.exe -m src.cli convert document.pdf --dry-run -b vertexai

# Validate an existing conversion
.venv\Scripts\python.exe -m src.cli validate document.pdf output/document.md

# List available backends
.venv\Scripts\python.exe -m src.cli backends
```

**Full `convert` options:**

| Flag | Default | Description |
|---|---|---|
| `-b / --backend` | config | `vertexai` \| `marker` \| `pdfplumber` |
| `--auth-mode` | config | `api` \| `gcloud` |
| `--project-id` | env `PROJECT_ID` | Google Cloud project ID |
| `--location` | config | Vertex AI region |
| `--model` | config | Gemini model ID |
| `--refine-iterations` | config | Iterative refinement passes |
| `--chunk-size` | config | Pages per chunk (0 = off) |
| `--chunk-overlap` | config | Overlap pages between chunks |
| `--workers` | config | Parallel workers for batch |
| `--extensions` | `.pdf` | Comma-separated extensions for batch (e.g. `.pdf,.docx,.pptx`) |
| `--validate` | off | Run quality validation |
| `--dry-run` | off | Estimate tokens, no API calls |
| `-v / --verbose` | off | DEBUG logging |

### Python API

```python
from src import Pipeline, load_settings

settings = load_settings({"processing": {"chunk_size": 10}})

result = Pipeline(backend="vertexai").convert(
    "document.pdf",
    project_id="my-proj",
    auth_mode="api",
    refine_iterations=2,
)
print(result.markdown[:500])
result.save("output/document.md")
```

## Multi-file Type Support

The **Vertex AI backend** can process Word, PowerPoint, and image files in addition to PDFs by converting them to PDF first.

### Supported input types

| Category | Extensions |
|---|---|
| PDF | `.pdf` |
| Word | `.docx`, `.doc`, `.odt`, `.rtf` |
| PowerPoint | `.pptx`, `.ppt`, `.odp` |
| Excel / Spreadsheets | `.xlsx`, `.xls`, `.ods` |
| Images | `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.tif`, `.webp`, `.gif` |

### Requirements

- **Office documents** (Word, PowerPoint, Excel): [LibreOffice](https://www.libreoffice.org/) must be installed and on `PATH`. Conversion runs headless via `libreoffice --headless --convert-to pdf`.
- **Images**: [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) is used — already a project dependency.
- **Backend**: Only the Vertex AI backend supports non-PDF inputs. Using a non-PDF file with `marker` or `pdfplumber` raises an error.

### CLI usage

```bash
# Convert a Word document
.venv\Scripts\python.exe -m src.cli convert report.docx -b vertexai -o output/

# Convert a PowerPoint presentation
.venv\Scripts\python.exe -m src.cli convert slides.pptx -b vertexai -o output/

# Convert an image
.venv\Scripts\python.exe -m src.cli convert scan.png -b vertexai -o output/

# Batch process a folder containing PDFs, Word docs, and images
.venv\Scripts\python.exe -m src.cli convert input/ --extensions ".pdf,.docx,.png" -b vertexai -o output/
```

### Notes

- **Chunking** is not supported for non-PDF files. If `--chunk-size` is set and a non-PDF file is encountered, chunking is skipped with a warning and the whole file is processed.
- The conversion step is logged in the execution log (visible in the Execution Log panel in the UI).
- In the Batch tab, use the **File types to process** multiselect to include non-PDF extensions.

## Backends

| Backend    | Type        | Scanned PDFs | Non-PDF support | Notes |
|------------|-------------|--------------|-----------------|-------|
| Vertex AI  | Cloud LLM   | Yes          | Yes (via pre-conversion) | Primary — `google-genai` required |
| Marker     | ML-powered  | Yes (OCR)    | No              | Secondary — `marker-pdf` required |
| pdfplumber | Heuristic   | No           | No              | Secondary — always available |

## Configuration (`src/config.json`)

All settings live in `src/config.json`. CLI flags and UI selections override these at runtime. Edit from the UI via the ⚙️ Settings tab.

```json
{
  "vertexai": {
    "project_id": "",
    "location": "europe-west3",
    "model": "gemini-2.5-pro",
    "auth_mode": "api",
    "refine_iterations": 0,
    "clean_stop_max_errors": 0,
    "extraction_prompt": "prompts/extraction.md",
    "refinement_prompt": "prompts/refinement.md"
  },
  "processing": {
    "backend": "vertexai",
    "chunk_size": 0,
    "chunk_overlap": 1,
    "workers": 1,
    "validate_after_convert": false
  },
  "batch": {
    "recursive": true,
    "extensions": [".pdf"]
  },
  "logging": {
    "exec_log_dir": "tmp",
    "exec_log_file": "exec_log.jsonl"
  }
}
```

## Authentication

Two modes for Vertex AI:

| Mode | How | When to use |
|---|---|---|
| `api` | `GOOGLE_API_KEY` env var | Express Mode, personal/testing |
| `gcloud` | ADC (`gcloud auth application-default login` or `GOOGLE_APPLICATION_CREDENTIALS`) | Production, service accounts |

Set via `--auth-mode` CLI flag, the Auth Mode selector in the UI, or `auth_mode` in `config.json`.

## Large PDF Chunking

For large PDFs, set `--chunk-size N` (or `chunk_size` in config) to split into N-page chunks:

- Each chunk is processed as an independent document (full pipeline: extract → refine → validate).
- `chunk_overlap` (default 1) adds trailing pages from the previous chunk to the next for context continuity.
- Chunks are merged with a `---` separator. Failed chunks are skipped with a warning embedded in the output.
- Temp chunk files are written to `_chunks_<stem>/` next to the source PDF and cleaned up automatically.

## Execution Log

Every run appends a row to `tmp/exec_log.jsonl` (JSONL, append-only):

```json
{
  "timestamp": "2026-03-28T14:22:01Z",
  "file": "invoices/report.pdf",
  "chunk_idx": 0, "chunk_pages": "0-9",
  "iteration": 2, "model": "gemini-2.5-pro", "auth_mode": "api",
  "input_tokens": 4821, "output_tokens": 1203, "total_tokens": 6024,
  "cost_label": "$0.042",
  "errors": 3, "critical": 0, "moderate": 2, "minor": 1,
  "verdict": "CLEAN",
  "extraction_prompt_hash": "9ce3a687",
  "refinement_prompt_hash": "d4f12b33"
}
```

Browse and filter the log in the **📊 Log Viewer** tab, or load in Python:

```python
import pandas as pd
df = pd.read_json("tmp/exec_log.jsonl", lines=True)
```

## Vertex AI Iterative Refinement

Set `--refine-iterations N` (or slider in UI) to run N self-correction passes after extraction. Each pass sends the PDF + current Markdown back to Gemini, which returns a structured JSON correction report. Stops early on `CLEAN` or diminishing returns.

Recommendation: 1–2 passes for most documents; 3–5 for complex tables / financial reports.

## Testing

The `testing/` folder contains a self-contained unit test suite covering the core library modules. No API credentials or external services are required.

```
testing/
├── conftest.py              # Shared fixtures (minimal PDF + PNG generators)
├── test_config.py           # Settings load, save, override, deep-merge
├── test_models.py           # ValidationReport, ConversionResult, ChunkResult, BatchResult
├── test_postprocess.py      # All markdown cleaning steps + postprocess() integration
├── test_file_converter.py   # File type detection, image→PDF conversion, ensure_pdf context manager
├── test_chunker.py          # PDF splitting, merge_chunks, cleanup
└── test_validation.py       # Similarity scoring, heading/table/list counting, row consistency
```

### Run the tests

```bash
.venv\Scripts\python.exe -m pytest testing/ -v
```

Run with coverage:

```bash
.venv\Scripts\python.exe -m pytest testing/ --cov=src --cov-report=term-missing
```

Run a specific module:

```bash
.venv\Scripts\python.exe -m pytest testing/test_postprocess.py -v
```

### Design principles

- **No credentials needed** — backends (Vertex AI, Marker) are not called; only pure-logic and PyMuPDF paths are tested.
- **No fixture files** — test PDFs and images are generated in-memory by `conftest.py` using PyMuPDF.
- **Self-contained** — each test class follows Arrange-Act-Assert and has a single focus.
- **Edge cases documented** — tests explicitly cover boundary conditions (empty inputs, threshold boundaries, missing dependencies).

## License

MIT
