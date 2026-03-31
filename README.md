# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

## Architecture

```
File Input (PDF, Word, PowerPoint, Excel, Image)
    │
    ├─► [Pre-convert] Non-PDF → LibreOffice/PyMuPDF → PDF
    │       └─► [Verbose] Save converted PDF to output folder
    │
    ├─► Classify (born-digital vs scanned)
    ├─► Split into chunks (optional, configurable page size + overlap)
    │       └─► Per-chunk: Extract → Post-process → (Validate)
    │           [Verbose] Save raw AI response → {name}.raw_step_NN.txt
    │           [Verbose] Save chunk markdown + slice PDF → {name}.chunk_NNN.md / .pdf
    │           [Always]  Save corrections report → {name}.chunk_NNN.corrections.md
    ├─► Resume support: existing chunk .md files are loaded from disk, skipped
    ├─► Merge chunks into final document
    ├─► Log execution row to tmp/exec_log.jsonl
    │
    └─► Markdown Output + ValidationReport + token/cost estimate
```

## Project Structure

```
pdf2md/
├── .venv/                  # Virtual environment (not committed)
├── app/
│   ├── app.py              # Streamlit entry point (4 tabs, sidebar globals)
│   ├── execute.py          # Convert File tab (resume, per-chunk corrections)
│   ├── tab_batch.py        # Batch Convert tab
│   ├── tab_log.py          # History tab
│   ├── tab_settings.py     # Settings tab (full config.json editor)
│   └── .streamlit/
│       └── config.toml     # Streamlit theme
├── prompts/
│   ├── extraction.md       # Universal prompt: text, tables, and meaningful visuals
│   ├── extraction_text.md  # Text and tables only, images skipped
│   ├── extraction_rag.md   # RAG-optimized extraction (default)
│   ├── refinement.md       # Iterative quality audit (skeptical-bias)
│   └── refinement_rag.md   # RAG-optimized convergent refinement (default)
├── src/
│   ├── backends/           # Extraction backends (Vertex AI, Marker, pdfplumber)
│   │   ├── __init__.py     # Backend registry and auto-selection
│   │   ├── base.py         # Shared backend interface
│   │   ├── vertexai_backend.py   # Google Gemini / Vertex AI extraction
│   │   ├── marker_backend.py     # Marker ML pipeline
│   │   └── pdfplumber_backend.py # Heuristic text extraction
│   ├── auth.py             # Authentication factory (api | gcloud)
│   ├── batch.py            # Folder discovery and batch orchestrator
│   ├── chunker.py          # PDF page splitter and markdown merger
│   ├── classifier.py       # Born-digital vs scanned detection
│   ├── cli.py              # CLI entry point (Click + Rich)
│   ├── config.json         # Runtime configuration (all settings)
│   ├── config.py           # Settings dataclass loader / saver
│   ├── file_converter.py   # Pre-conversion: Office/image → PDF
│   ├── logger_exec.py      # Structured JSONL execution logger
│   ├── logging_config.py   # Centralised logging (console + rotating file)
│   ├── models.py           # ConversionResult, ChunkResult, BatchResult, ValidationReport
│   ├── pipeline.py         # Single-file orchestrator
│   ├── postprocess.py      # Markdown cleaning pipeline
│   ├── validation.py       # Quality validation
│   └── vertexai_pricing.py # Gemini pricing fetch and cache
├── testing/
│   ├── conftest.py         # Shared fixtures (in-memory PDF/PNG generators)
│   ├── test_config.py      # Config loading, saving, merging
│   ├── test_models.py      # Data models and properties
│   ├── test_postprocess.py # Markdown cleaning pipeline
│   ├── test_file_converter.py  # File type detection, image→PDF conversion
│   ├── test_chunker.py     # PDF splitting and markdown merging
│   ├── test_validation.py  # Quality validation helpers
│   └── test_vertexai_backend.py # Vertex AI backend (mocked, no credentials)
├── tmp/
│   ├── exec_log.jsonl      # Persistent execution log (append-only)
│   └── pdf2md_*.log        # Rotating debug log files
├── launch_app.bat          # Windows launcher for Streamlit
├── launch_app.sh           # Linux/macOS launcher for Streamlit
├── .env.example            # Template for GCP credentials
├── requirements.txt
├── LICENSE                 # MIT
└── README.md
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
# or
.venv/bin/pip install -r requirements.txt                      # Linux/macOS
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
.venv\Scripts\streamlit.exe run app/app.py            # Windows
# or double-click launch_app.bat
# or
./launch_app.sh                                        # Linux/macOS
```

**Tabs:**

| Tab | Purpose |
|---|---|
| Convert File | Single-file conversion with file picker, live log, and resume |
| Batch Convert | Folder processing with results table |
| History | Browse and filter `exec_log.jsonl` |
| Settings | Edit `config.json` from the UI |

**UI layout:**

- **Sidebar** shows available backends and holds the global **Backend** and **Auth Mode** selectors (shared across all tabs).
- The **Convert File** and **Batch Convert** tabs show only essential options by default. Chunking, Vertex AI model/prompt, and refinement settings are behind an **Advanced options** expander.
- Clicking **Convert** or **Run Batch** automatically clears the previous result — no manual "Clean" step needed.
- The **Execution Log** appears below the action button in chronological order with auto-scroll.
- Results lead with the converted markdown preview and a **Download Markdown** button.
- **Dry run** is a toggle next to the main action button.
- Prompt dropdowns are **filtered by type**: extraction prompts only list files starting with `extraction`, refinement prompts only list files starting with `refinement`.

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

# Use the text-and-tables-only prompt (no image extraction)
.venv\Scripts\python.exe -m src.cli convert report.pdf \
    --extraction-prompt prompts/extraction_text.md -b vertexai -o output/

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
| `--chunk-size` | config | Pages per chunk (0 = off). Works for all file types. |
| `--chunk-overlap` | config | Overlap pages between chunks |
| `--max-chunks` | 0 (all) | Stop after processing this many chunks (0 = all) |
| `--workers` | config | Parallel workers for batch |
| `--extensions` | `.pdf` | Comma-separated extensions for batch (e.g. `.pdf,.docx,.pptx`) |
| `--extraction-prompt` | config | Path to extraction prompt file (relative to project root) |
| `--refinement-prompt` | config | Path to refinement prompt file (relative to project root) |
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

- **Chunking works for all file types.** When `--chunk-size` is set and a non-PDF file is given, the file is first converted to PDF, then split into chunks normally. A 50-page PowerPoint with `--chunk-size 10` produces 5 chunks.
- The conversion step is logged in the execution log (visible in the Execution Log panel in the UI).
- In the Batch tab, use the **File types to process** multiselect to include non-PDF extensions.

## Backends

| Backend    | Type        | Scanned PDFs | Non-PDF support | Notes |
|------------|-------------|--------------|-----------------|-------|
| Vertex AI  | Cloud LLM   | Yes          | Yes (via pre-conversion) | Primary — `google-genai` required |
| Marker     | ML-powered  | Yes (OCR)    | No              | Secondary — `marker-pdf` required |
| pdfplumber | Heuristic   | No           | No              | Secondary — always available |

## Prompts

The Vertex AI backend uses Markdown prompt files from the `prompts/` folder. Both the UI and CLI discover all `.md` files in that folder automatically — add a new file there and it appears as a selectable option immediately. Prompt dropdowns are filtered by type: extraction selectors only show files whose name starts with `extraction`, refinement selectors only show files starting with `refinement`.

| File | Purpose |
|---|---|
| `prompts/extraction_rag.md` | **Default.** RAG-optimized: factual accuracy and structural clarity for AI retrieval. Prioritises exact text, exact numbers, complete tables, and useful image descriptions. |
| `prompts/extraction.md` | Universal prompt: reads like a human expert — extracts text, tables, and meaningful visuals; omits decorative elements silently. |
| `prompts/extraction_text.md` | Text and tables only. All images are skipped. Best for financial reports, contracts, and pure-text documents. |
| `prompts/refinement_rag.md` | **Default.** Convergent RAG refinement: objective audit with a high bar — only flags errors that would cause an AI to retrieve or answer incorrectly. `CLEAN` is the default verdict. |
| `prompts/refinement.md` | Skeptical-bias iterative audit. Approaches every document looking for what was missed or wrong. |

**Switching prompts at runtime:**
- **UI**: Use the "Extraction prompt" and "Refinement prompt" dropdowns under **Advanced options** in the Convert File or Batch Convert tabs.
- **CLI**: Pass `--extraction-prompt prompts/extraction_text.md` to override for a single run.
- **Settings tab / config.json**: Change the default applied to every run.

## Configuration (`src/config.json`)

All settings live in `src/config.json`. CLI flags and UI selections override these at runtime. Edit from the UI via the **Settings** tab.

```json
{
  "vertexai": {
    "project_id": "",
    "location": "europe-west3",
    "model": "gemini-3.1-flash-lite-preview",
    "auth_mode": "api",
    "refine_iterations": 0,
    "clean_stop_max_errors": 0,
    "diminishing_returns_enabled": true,
    "extraction_prompt": "prompts/extraction_rag.md",
    "refinement_prompt": "prompts/refinement_rag.md"
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
    "exec_log_file": "exec_log.jsonl",
    "log_dir": "tmp",
    "log_max_bytes": 10485760,
    "log_backup_count": 5
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

## Chunking

For large documents, set `--chunk-size N` (or `chunk_size` in config) to split into N-page chunks. **Chunking works for all supported file types** — non-PDF files (Word, PowerPoint, images) are converted to PDF first, then split.

- Each chunk is processed as an independent document (full pipeline: extract → refine → validate).
- `chunk_overlap` (default 1) adds trailing pages from the previous chunk to the next for context continuity.
- At merge time, overlapped pages that were re-extracted by the next chunk are automatically deduplicated. Exact line matches are stripped first; if the LLM produced minor differences (added emoji, punctuation, reformatted URLs), fuzzy matching (≥ 85% character similarity) removes the duplicate tail from the previous chunk.
- `--max-chunks N` (UI: **Max chunks** field) stops after N chunks — useful for testing large documents without processing the whole file.
- Chunks are merged with a `---` separator. Failed chunks are skipped with a warning embedded in the output. If the merge itself fails, a plain newline-separator join is used as fallback.
- Chunk PDF files are written as `{stem}.chunk_NNN.pdf` next to the source file (flat naming, no subdirectory).
- Starting a new **Convert** on the same file removes prior outputs for that basename first; existing chunk `.md` files are preserved by default so that **resume** can pick them up. Your original source file is never deleted.

### Resume from interrupted run

If a conversion is interrupted (API timeout, crash, manual stop), chunk `.md` files already written to disk are kept. On the next run of the same file, existing `{stem}.chunk_NNN.md` files are detected, loaded from disk, and **skipped** — only the missing chunks are sent to the API. This saves both time and cost for large documents.

## Diminishing Returns

When `diminishing_returns_enabled` is `true` (default), the refinement loop stops early if a pass produces no improvements or returns a `CLEAN` verdict. This avoids wasting API calls on already-good output.

To force all refinement passes to run regardless — for example, on critical documents where you want maximum scrutiny — set `diminishing_returns_enabled` to `false` in `config.json`, uncheck it in the Settings tab, or toggle it in the Advanced options of the Convert File / Batch Convert tabs.

## Per-chunk Corrections Reports

After each chunk finishes processing, a corrections report is written to `{stem}.chunk_NNN.corrections.md`. These reports are generated independently of verbose mode and contain the detailed track record and corrections for each chunk. They appear in the "Saved artifacts" expander in the UI when processing multi-chunk documents.

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

Browse and filter the log in the **History** tab, or load in Python:

```python
import pandas as pd
df = pd.read_json("tmp/exec_log.jsonl", lines=True)
```

## Logging

The project uses two complementary logging systems:

1. **Execution Log** (`tmp/exec_log.jsonl`) — Structured JSONL table, one row per API call. Best for cost tracking, token audits, and the Log Viewer tab.
2. **Debug Log** (`tmp/pdf2md_*.log`) — Traditional rotating log file at `DEBUG` level. Best for tracing execution flow, diagnosing errors, and auditing every step.

### Debug Log

Every run writes a detailed debug log to `tmp/pdf2md_<timestamp>.log`. The file always captures `DEBUG`-level messages regardless of the console verbosity, so you get full traceability without cluttering the UI.

**What the console shows (INFO level, default):**

```
INFO      pipeline: Classified report.pdf as born-digital (12 pages, 1482 avg chars/page)
INFO      pipeline: Using backend: vertexai
INFO      backends.vertexai: API Extraction completed in 4.23s — model=gemini-2.5-pro, tokens=8,412 (in=6,100, out=2,312)
```

**What the file log captures (DEBUG level, always):**

```
2026-03-28T14:22:01+0000 | DEBUG    | a1b2c3d4 | pipeline.convert:58 | Pipeline.convert() — file=report.pdf, validate=False
2026-03-28T14:22:01+0000 | DEBUG    | a1b2c3d4 | pipeline.convert:63 | Classifying PDF: report.pdf
2026-03-28T14:22:01+0000 | DEBUG    | a1b2c3d4 | pipeline.convert:65 | Classification took 0.042s
2026-03-28T14:22:01+0000 | INFO     | a1b2c3d4 | pipeline.convert:66 | Classified report.pdf as born-digital (12 pages, 1482 avg chars/page)
2026-03-28T14:22:01+0000 | DEBUG    | a1b2c3d4 | backends.vertexai.convert:312 | convert() called — pdf_path=report.pdf, size=245760 bytes, ...
2026-03-28T14:22:01+0000 | DEBUG    | a1b2c3d4 | backends.vertexai._call_with_retry:228 | API call attempt 1/3 — model=gemini-2.5-pro
2026-03-28T14:22:05+0000 | DEBUG    | a1b2c3d4 | backends.vertexai._call_with_retry:235 | API call succeeded in 4.23s (attempt 1)
2026-03-28T14:22:05+0000 | INFO     | a1b2c3d4 | backends.vertexai.convert:430 | API Extraction completed in 4.23s — model=gemini-2.5-pro, tokens=8,412 (in=6,100, out=2,312)
2026-03-28T14:22:05+0000 | DEBUG    | a1b2c3d4 | backends.vertexai.convert:431 | API Extraction detail: {'pdf': 'report.pdf', 'prompt_hash': '9ce3a687'}
2026-03-28T14:22:05+0000 | DEBUG    | a1b2c3d4 | pipeline.convert:82 | Post-processing took 0.003s, output=14200 chars
2026-03-28T14:22:05+0000 | DEBUG    | a1b2c3d4 | pipeline.convert:95 | Pipeline.convert() finished in 4.32s — backend=vertexai, chars=14200, tokens=~3550
```

### Log file format

Each line contains:

| Field | Example | Description |
|---|---|---|
| Timestamp | `2026-03-28T14:22:01+0000` | ISO-8601 UTC |
| Level | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| Run ID | `a1b2c3d4` | 8-char hex correlating all messages from one execution |
| Location | `pipeline.convert:58` | `module.function:line` for pinpointing the source |
| Message | (free text) | Human-readable detail |

### Configuration

| Setting | Default | Description |
|---|---|---|
| `logging.log_dir` | `"tmp"` | Directory for log files (relative to project root) |
| `logging.log_max_bytes` | `10485760` (10 MB) | Max size per log file before rotation |
| `logging.log_backup_count` | `5` | Number of rotated log files to keep |

### Verbose mode and log levels

| Mode | Console | File |
|---|---|---|
| Default | `INFO` | `DEBUG` |
| `--verbose` / Verbose checkbox | `DEBUG` | `DEBUG` |

With `--verbose` (CLI) or the Verbose checkbox (UI), the console output matches the file — you see every debug message in real time. Without verbose, the console stays clean (INFO) while the file always captures full DEBUG detail.

### API call timing

Every API call (extraction and refinement) is logged at `INFO` level with wall-clock latency, model, and token counts. This appears in both the Streamlit UI log stream and the file log:

```
INFO  backends.vertexai: API Extraction completed in 4.23s — model=gemini-2.5-pro, tokens=8,412 (in=6,100, out=2,312)
INFO  backends.vertexai: API Refinement pass 1 completed in 3.87s — model=gemini-2.5-pro, tokens=12,045 (in=8,500, out=3,545)
```

### Correlation and auditing

- The **Run ID** (`a1b2c3d4`) groups all log messages from a single CLI invocation or Execute-tab run. Use `grep a1b2c3d4 tmp/pdf2md_*.log` to extract a complete execution trace.
- Retry attempts, backoff delays, and error details are logged at `DEBUG` and `WARNING` levels for full API call auditing.
- Pipeline stages (classification, extraction, post-processing, validation) are individually timed at `DEBUG` level.

## Verbose Mode — Intermediate File Saving

When **Verbose** is enabled in the **Convert File** tab, intermediate artifacts are saved next to the output Markdown file. The CLI `-v` flag only increases log verbosity; it does not write these artifacts (use the app for full verbose dumps).

Clicking **Convert** automatically clears the previous result and log from the UI, then removes prior outputs for that output basename before starting (same folder as `{name}.md`), so you do not accumulate stale steps. With Verbose on, each removed path is logged. Your original source file (e.g. `{name}.pdf`) is never deleted.

| File | When created | Content |
|------|-------------|---------|
| `{name}.pdf` | Non-PDF source + verbose | Converted PDF, saved before extraction starts |
| `{name}.raw_step_00.txt` | Vertex AI, verbose | Raw text response from the initial extraction call |
| `{name}.raw_step_01.txt` | Vertex AI, verbose + refine | Raw JSON response from refinement pass 1 |
| `{name}.raw_step_NN.txt` | Vertex AI, verbose + refine | Raw JSON response from refinement pass N |
| `{name}.step_01.md` | Vertex AI, verbose | Processed markdown after extraction |
| `{name}.step_NN.md` | Vertex AI, verbose + refine | Processed markdown after refinement pass N |
| `{name}_chunk_001.raw_step_*.txt` | Vertex AI, verbose + chunking | Raw responses per chunk |
| `{name}.chunk_001.md` | Verbose + chunking | Markdown for chunk 1 (saved immediately after each chunk) |
| `{name}.chunk_001.pdf` | Verbose + chunking | PDF page-range slice for chunk 1 |
| `{name}.chunk_001.corrections.md` | Chunking (always) | Corrections report for chunk 1 |

Raw response files are written to disk **immediately after each API call**, so if a later step crashes you can still inspect what was returned and diagnose parsing issues.

## Vertex AI Iterative Refinement

Set `--refine-iterations N` (or slider in UI) to run N self-correction passes after extraction. Each pass sends the PDF + current Markdown back to Gemini, which returns a structured JSON correction report. Stops early on `CLEAN` or diminishing returns (unless `diminishing_returns_enabled` is `false`).

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
├── test_validation.py       # Similarity scoring, heading/table/list counting, row consistency
└── test_vertexai_backend.py # Vertex AI backend (mocked/stubbed — no credentials needed)
```

### Run the tests

```bash
.venv\Scripts\python.exe -m pytest testing/ -v                                # Windows
.venv/bin/python -m pytest testing/ -v                                         # Linux/macOS
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

---

## Evolution Log — What Changed and Why

This section traces the project's development through its major iterations. Each step documents the problem that was solved, the design decision behind it, and the lesson learned. It serves as both a changelog and an instructional guide for understanding the architecture.

### Step 1 — Initial Pipeline (PR #1)

**Problem:** We needed a way to convert PDF documents into clean Markdown for LLM consumption (RAG, summarization, Q&A). No existing tool produced output clean enough for production LLM pipelines.

**What was built:** The foundational `pdf2md` pipeline — a Streamlit UI with a CLI, a pluggable backend system (Marker, pdfplumber, Docling), and a post-processing layer that cleans up raw extraction output into token-efficient Markdown.

**Key design decisions:**
- **Pluggable backends** — different PDFs need different extractors. Born-digital PDFs work fine with heuristic extractors (pdfplumber), while scanned documents need OCR (Marker). The `BaseBackend` interface and registry pattern lets us add backends without touching the pipeline.
- **Post-processing pipeline** — raw extraction output is never clean enough. The `postprocess.py` module chains cleaning steps (normalise whitespace, fix broken tables, strip noise) into a deterministic pipeline that runs after every backend.
- **Config-first architecture** — all settings live in `config.json` so the same tool works in different contexts without code changes.

**Lesson:** Separation of concerns (extraction vs cleaning vs orchestration) makes each piece independently testable and replaceable. Starting with a pluggable backend system from day one avoided a painful refactor later.

### Step 2 — Vertex AI Backend and Refinement (commits before PR #3)

**Problem:** Heuristic and ML-based backends (pdfplumber, Marker) produced acceptable output for simple PDFs, but struggled with complex layouts, nested tables, and documents mixing text with meaningful visuals. We needed a smarter extraction approach.

**What was built:** A Vertex AI (Gemini) backend that sends the entire PDF to a multimodal LLM with a structured extraction prompt. The LLM "reads" the document as a human would and produces structured Markdown. An iterative refinement loop was added: after initial extraction, the PDF + current Markdown are sent back to the model for self-correction passes.

**Key design decisions:**
- **LLM as primary extractor** — instead of fighting heuristics for every edge case, delegate the hard work to a model that understands layout, context, and semantics.
- **Prompt-driven extraction** — the extraction behavior is controlled by editable Markdown prompt files, not code. This means we can tune extraction quality without changing the codebase.
- **Dual auth modes** (`api` vs `gcloud`) — the `api` mode (API key) is fast for personal use; `gcloud` mode (ADC / service account) is production-grade. The `auth.py` factory hides this choice from the rest of the codebase.

**Lesson:** Multimodal LLMs transformed the quality ceiling. Documents that were impossible with heuristics (scanned forms, mixed-content slides) became tractable. But raw LLM output still needs the same post-processing pipeline — the model is smart but not consistent.

### Step 3 — Refinement Optimization (PR #3)

**Problem:** The refinement loop accumulated error logs from all previous iterations and passed the full history to each new pass. This caused confusion: the model would sometimes "fix" things that had already been corrected, or get distracted by stale error reports.

**What was fixed:** Each refinement pass now only sees the PDF and the current Markdown — no accumulated error history. The model independently audits the current state each time. Errors naturally reduce as the Markdown improves across iterations.

**Lesson:** Stateless refinement is more robust than stateful. Giving the model a clean view each time produces more consistent improvements than feeding it a growing correction log. Simpler context = better reasoning.

### Step 4 — Multi-file Type Support + Test Suite (PR #5)

**Problem:** Real-world document pipelines include Word documents, PowerPoint presentations, Excel spreadsheets, and images — not just PDFs. Users had to pre-convert these manually before running the tool.

**What was built:**
- A `file_converter.py` module that detects non-PDF inputs and converts them to PDF automatically (LibreOffice for Office documents, PyMuPDF for images) before the extraction pipeline runs.
- A comprehensive test suite (`testing/`) with in-memory PDF and image generators — no fixture files, no API credentials needed.

**Key design decisions:**
- **Pre-conversion to PDF** — rather than teaching every backend to handle every format, normalise all inputs to PDF first. This keeps backends simple and focused on one format.
- **In-memory test fixtures** — `conftest.py` generates test PDFs and PNGs using PyMuPDF at runtime. No fixture files to maintain, no binary blobs in the repo, and tests run anywhere without setup.

**Lesson:** Normalising inputs early in the pipeline (the "funnel" pattern) is far more maintainable than handling N formats in M backends. The test suite design — zero external dependencies, zero fixture files — ensures tests stay green across environments.

### Step 5 — Prompt Redesign (PR #6)

**Problem:** The original extraction prompt was generic and produced inconsistent results across document types. Image handling was rule-based ("[Figure: ...]" placeholders) rather than semantic, creating noise in the output.

**What was built:**
- `extraction.md` — reframed as a "universal human reader" prompt that extracts meaningful content (text, tables, UI screenshots, diagrams) and omits decorative elements without leaving placeholder noise.
- `extraction_text.md` — a specialised prompt for text-heavy documents (contracts, financial reports) that explicitly skips all images.
- Dynamic prompt discovery: the UI and CLI scan the `prompts/` folder for `.md` files at startup, so adding a new prompt is just dropping a file.

**Lesson:** Prompt engineering is as important as code architecture. A well-structured prompt with clear priorities produces dramatically better output than a list of rules. The "read like a human expert" framing gave the model the right mental model for extraction.

### Step 6 — Verbose Mode and Universal Chunking (PR #7)

**Problem:** Large PDFs (100+ pages) exceeded LLM context windows. When conversions failed partway through, there was no way to inspect what the API returned before the crash. Chunking was PDF-only and didn't work with pre-converted files.

**What was built:**
- **Verbose mode** in the UI saves every raw API response to disk immediately (`{name}.raw_step_NN.txt`), so partial failures can be diagnosed.
- **Universal chunking** — non-PDF files are converted to PDF first, then split. A 50-page PowerPoint with `--chunk-size 10` produces 5 chunks.
- **`--max-chunks N`** parameter — process only the first N chunks, useful for testing large documents without burning through the entire file.

**Lesson:** Observability is a feature. When you're sending documents to a cloud API, you need to see exactly what was sent and returned at every step. Saving raw responses immediately (not after all steps complete) is the only way to debug partial failures.

### Step 7 — Dual-handler Logging System (PR #9)

**Problem:** The console log was either too noisy (DEBUG during development) or too quiet (INFO in production). There was no persistent record of what happened across runs, and no way to correlate log messages from the same execution.

**What was built:**
- A centralised logging setup (`logging_config.py`) with two independent streams: console at INFO (or DEBUG with `--verbose`) and a rotating file handler at DEBUG that always captures everything.
- Every execution is tagged with an 8-char `run_id` for correlation and auditing.
- API call timing is logged at INFO level with latency, model, and token breakdowns.

**Key design decisions:**
- **Console: clean / File: everything** — the UI stays readable while the file captures the full story. `--verbose` promotes the console to DEBUG for real-time debugging.
- **Run ID correlation** — `grep a1b2c3d4 tmp/pdf2md_*.log` extracts every message from a single execution, even across modules.
- **Rotating file handler** — caps at 10 MB with 5 backups, so log files don't fill the disk.

**Lesson:** Two-stream logging (clean console + full-detail file) is the right pattern for any tool with a UI. The run ID concept came from wanting to answer "what exactly happened in that conversion that failed yesterday?" — without it, grep across log files is nearly useless.

### Step 8 — Settings Tab and Logging Config (PR #10)

**Problem:** The logging configuration (`log_dir`, `log_max_bytes`, `log_backup_count`) was only editable by hand in `config.json`. Users wanted to change logging settings from the Streamlit UI like every other setting.

**What was fixed:** All logging configuration keys were exposed in the Settings tab. The entire `config.json` is now fully editable from the UI.

**Lesson:** If a setting exists in the config file but isn't in the UI, users will forget it exists. Every configurable value should be accessible through the same interface.

### Step 9 — UI/UX Overhaul and Cross-platform Support (PR #11)

**Problem:** The app was Windows-only (batch scripts, Windows paths). The UI had usability issues: inconsistent layouts, confusing option grouping, and fragile JSON parsing that crashed on malformed API responses.

**What was built:**
- **Cross-platform support** — `launch_app.sh` for Linux/macOS, path handling with `pathlib.Path` throughout, conditional `pywin32` dependency.
- **Robust JSON repair** — the Vertex AI backend now handles malformed JSON responses (truncated, trailing commas, unquoted keys) instead of crashing.
- **UI/UX improvements** — consistent tab layouts, better option grouping, clearer labels.

**Lesson:** Cross-platform support should be built in from the start, but when it isn't, `pathlib.Path` makes the retrofit manageable. JSON repair for LLM responses is not optional — models produce invalid JSON often enough that it needs to be handled gracefully.

### Step 10 — Diminishing Returns, Resume, and RAG Prompts (PR #12)

**Problem:** Several issues converged: (1) refinement sometimes wasted API calls when the output was already clean, (2) large multi-chunk conversions that failed partway through had to restart from scratch, (3) the default prompts weren't optimised for the primary use case (RAG knowledge bases).

**What was built:**
- **Diminishing returns control** — a `diminishing_returns_enabled` flag (default: `true`) that lets the refinement loop stop early when no improvements are found. Can be disabled for critical documents.
- **Resume from interrupted run** — before calling the API, the pipeline checks for existing `{stem}.chunk_NNN.md` files on disk. Complete chunks are loaded and skipped. Only missing chunks are processed.
- **Per-chunk corrections reports** — `{stem}.chunk_NNN.corrections.md` is written after each chunk, independent of verbose mode, capturing the full correction track record.
- **RAG-optimised prompts** — `extraction_rag.md` and `refinement_rag.md` became the new defaults. The extraction prompt prioritises factual accuracy for AI retrieval; the refinement prompt uses an objective "high bar" approach that only flags errors that would cause wrong AI answers.
- **Filtered prompt selectors** — UI dropdowns now only show extraction prompts for extraction and refinement prompts for refinement, preventing accidental cross-assignment.

**Key design decisions:**
- **Resume is opt-in by default** — chunk files are preserved during cleanup unless the user explicitly removes them. This makes resume the natural path after any failure.
- **Corrections reports are always written** — not gated on verbose mode, because understanding what was corrected in each chunk is essential for quality assurance, not just debugging.
- **RAG as default prompt** — the majority of users are building RAG pipelines, so the default should be optimised for that. The universal and text-only prompts remain available for other use cases.

**Lesson:** Resume capability transforms the economics of large-document processing. A 200-page document split into 20 chunks no longer needs to restart from chunk 1 when chunk 15 fails. The cost of implementing resume (checking for existing files before API calls) was minimal compared to the cost of re-processing entire documents. Convergent refinement (objective, high-bar auditing) produces better results than skeptical refinement (looking for problems) because the model stops over-correcting things that were already correct.

## License

MIT
