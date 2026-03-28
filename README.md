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
│   ├── extraction.md       # Universal prompt (default): text, tables, and visuals
│   ├── extraction_text.md  # Specialized prompt: text and tables only, images skipped
│   └── refinement.md       # Iterative quality audit prompt
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
│   ├── logging_config.py   # Centralised logging setup (console + rotating file)
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
│   ├── exec_log.jsonl      # Persistent execution log (append-only)
│   └── pdf2md_*.log        # Rotating debug log files
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

The Vertex AI backend uses Markdown prompt files from the `prompts/` folder. Both the UI and CLI discover all `.md` files in that folder automatically — add a new file there and it appears as a selectable option immediately.

| File | Purpose |
|---|---|
| `prompts/extraction.md` | **Default.** Universal prompt: reads like a human expert — extracts text, tables, and meaningful visuals (UI screenshots, charts, diagrams); omits decorative elements silently. |
| `prompts/extraction_text.md` | Specialized: text and tables only. All images are skipped. Best for financial reports, contracts, and pure-text documents. |
| `prompts/refinement.md` | Iterative quality audit. Used when `refine_iterations > 0`. |

**Switching prompts at runtime:**
- **UI**: Use the "Extraction prompt" and "Refinement prompt" dropdowns in the Execute or Batch tabs.
- **CLI**: Pass `--extraction-prompt prompts/extraction_text.md` to override for a single run.
- **Settings tab / config.json**: Change the default applied to every run.

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
- Chunks are merged with a `---` separator. Failed chunks are skipped with a warning embedded in the output.
- Temp chunk files are written to `_chunks_<stem>/` next to the source PDF and removed after the run (verbose mode also copies each slice to `{stem}.chunk_NNN.pdf` beside the output).
- Starting a new **Execute** on the same file removes prior outputs for that basename first (`{stem}.*`, `{stem}_chunk_*`, and `_chunks_<stem>/`); the source PDF is never deleted. With **Verbose** on, each deleted path is logged in the execution log.

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

When **Verbose** is enabled in the **Execute** tab, intermediate artifacts are saved next to the output Markdown file. The CLI `-v` flag only increases log verbosity; it does not write these artifacts (use the app for full verbose dumps).

**🧹 Clean** clears the Execute tab (log and result panel) only — it does **not** delete files on disk.

Starting **⚡ Execute** again on the same input removes previous outputs for that output basename first (same folder as `{name}.md`), so you do not accumulate stale steps. With Verbose on, each removed path is logged. Your original source file (e.g. `{name}.pdf`) is never deleted.

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
| `{name}.chunk_001.pdf` | Verbose + chunking | PDF page-range slice for chunk 1 (copy of the temp chunk file) |

Raw response files are written to disk **immediately after each API call**, so if a later step crashes you can still inspect what was returned and diagnose parsing issues.

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
