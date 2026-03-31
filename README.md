# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

## Architecture

```
File Input (PDF, Word, PowerPoint, Excel, Image)
    │
    ├─► [Pre-convert] Non-PDF → Office COM/PyMuPDF → PDF
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
│   ├── app.py              # Streamlit entry point (5 tabs, sidebar machine selector)
│   ├── execute.py          # Convert File tab (resume, per-chunk corrections)
│   ├── tab_batch.py        # Batch Convert tab
│   ├── tab_log.py          # History tab
│   ├── tab_settings.py     # Settings tab (machine profiles + full config.json editor)
│   ├── tab_vertexai.py     # Vertex AI tab (pricing table, cache refresh, usage link)
│   └── .streamlit/
│       └── config.toml     # Streamlit theme
├── prompts/
│   ├── extraction.md       # Universal prompt: text, tables, and meaningful visuals
│   ├── extraction_text.md  # Text and tables only, images skipped
│   ├── extraction_rag.md   # RAG-optimized extraction (default)
│   ├── refinement.md       # Iterative quality audit (skeptical-bias)
│   └── refinement_rag.md   # RAG-optimized convergent refinement (default)
├── src/
│   ├── backends/
│   │   └── __init__.py     # Thin compatibility shim (re-exports VertexAIBackend)
│   ├── auth.py             # Authentication factory (api | gcloud)
│   ├── batch.py            # Folder discovery and batch orchestrator
│   ├── chunker.py          # PDF page splitter and markdown merger
│   ├── classifier.py       # Born-digital vs scanned detection
│   ├── cli.py              # CLI entry point (Click + Rich)
│   ├── config.json         # Runtime configuration (machines + all settings)
│   ├── config.py           # Settings / MachineProfile dataclass loader / saver
│   ├── file_converter.py   # Pre-conversion: Office/image → PDF
│   ├── logger_exec.py      # Structured JSONL execution logger
│   ├── logging_config.py   # Centralised logging (console + rotating file)
│   ├── models.py           # ConversionResult, ChunkResult, BatchResult, ValidationReport
│   ├── pipeline.py         # Single-file orchestrator
│   ├── postprocess.py      # Markdown cleaning pipeline
│   ├── validation.py       # Quality validation
│   ├── vertexai_backend.py # Google Gemini / Vertex AI extraction backend
│   └── vertexai_pricing.py # Gemini pricing fetch and cache (uses pricing/ folder)
├── pricing/
│   ├── vertexai_pricing_cache.json  # Live pricing cache (JSON)
│   └── vertexai_pricing.md          # Pricing table as Markdown (human-readable)
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

Copy `.env.example` to `.env` and set your API key:

```bash
GOOGLE_API_KEY=your-api-key    # for auth_mode=api
# (for gcloud mode, use: gcloud auth application-default login)
```

All other settings (project ID, location, model, etc.) are configured per **machine profile** in `src/config.json` or via the **Settings** tab in the UI.

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
| Settings | Edit machine profiles and `config.json` from the UI |
| Vertex AI | Pricing table, cache refresh, and usage dashboard link |

**UI layout:**

- **Sidebar** shows only the **Machine** selector. Switching a machine reloads the app with its settings.
- **Auth Mode** is configured per machine in the **Settings** tab or overridden per run in the **Advanced options** expander on the Convert File and Batch Convert tabs.
- The **Convert File** and **Batch Convert** tabs show only essential options by default. All Vertex AI settings (project, location, auth mode, model, refinement) and chunking options are in the **Advanced options** expander — matching the same layout as the **Settings** tab.
- Clicking **Convert** or **Run Batch** automatically clears the previous result — no manual "Clean" step needed.
- The **Execution Log** appears below the action button in chronological order with auto-scroll.
- Results lead with the converted markdown preview and a **Download Markdown** button.
- **Dry run** is a toggle next to the main action button.
- Prompt dropdowns are **filtered by type**: extraction prompts only list files starting with `extraction`, refinement prompts only list files starting with `refinement`.
- The **Vertex AI** tab shows the live pricing table (from `pricing/vertexai_pricing.md`), cache metadata (date and model count), a **Refresh pricing** button, and a direct link to the Google Cloud usage dashboard for the active project.

### CLI

```bash
# Convert a single PDF (uses active machine profile defaults)
.venv\Scripts\python.exe -m src.cli convert document.pdf -o output/

# Override specific Vertex AI settings at runtime
.venv\Scripts\python.exe -m src.cli convert document.pdf \
    --auth-mode api \
    --project-id my-proj --model gemini-2.5-pro \
    --refine-iterations 3 -o output/

# Large PDF: split into 10-page chunks with 1-page overlap
.venv\Scripts\python.exe -m src.cli convert bigdoc.pdf \
    --chunk-size 10 --chunk-overlap 1 -o output/

# Batch folder processing
.venv\Scripts\python.exe -m src.cli convert input_pdfs/ -o output/

# Use the text-and-tables-only prompt (no image extraction)
.venv\Scripts\python.exe -m src.cli convert report.pdf \
    --extraction-prompt prompts/extraction_text.md -o output/

# Dry run — estimate tokens and cost without calling the API
.venv\Scripts\python.exe -m src.cli convert document.pdf --dry-run

# Validate an existing conversion
.venv\Scripts\python.exe -m src.cli validate document.pdf output/document.md

# List available backend
.venv\Scripts\python.exe -m src.cli backends
```

**Full `convert` options:**

| Flag | Default | Description |
|---|---|---|
| `--auth-mode` | config | `api` \| `gcloud` |
| `--project-id` | config | Google Cloud project ID |
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

## Machine Profiles

Machine profiles let you maintain different Vertex AI configurations for different machines or environments (e.g. home desktop vs work laptop vs CI/CD), all in a single `config.json`.

- Select the active machine in the **sidebar** of the Streamlit app.
- Switching a machine immediately reloads the app with its Vertex AI settings.
- Add, rename, and delete machines from the **Settings** tab.
- Each profile stores: project ID, location, model, auth mode, refinement settings, and prompt files.

**Default profile** — on first run a `"Default"` machine is created. Rename it to something meaningful (e.g. `"Desktop"`, `"Laptop"`).

## Multi-file Type Support

The Vertex AI backend can process Word, PowerPoint, and image files in addition to PDFs by converting them to PDF first.

### Supported input types

| Category | Extensions |
|---|---|
| PDF | `.pdf` |
| Word | `.docx`, `.doc`, `.odt`, `.rtf` |
| PowerPoint | `.pptx`, `.ppt`, `.odp` |
| Excel / Spreadsheets | `.xlsx`, `.xls`, `.ods` |
| Images | `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.tif`, `.webp`, `.gif` |

### Requirements

- **Office documents** (Word, PowerPoint, Excel): On Windows, conversion uses the Office COM API (requires Microsoft Office). On other platforms, `docling` is used.
- **Images**: [PyMuPDF](https://pymupdf.readthedocs.io/) (`fitz`) is used — already a project dependency.

### CLI usage

```bash
# Convert a Word document
.venv\Scripts\python.exe -m src.cli convert report.docx -o output/

# Convert a PowerPoint presentation
.venv\Scripts\python.exe -m src.cli convert slides.pptx -o output/

# Batch process a folder containing PDFs, Word docs, and images
.venv\Scripts\python.exe -m src.cli convert input/ --extensions ".pdf,.docx,.png" -o output/
```

### Notes

- **Chunking works for all file types.** When `--chunk-size` is set and a non-PDF file is given, the file is first converted to PDF, then split into chunks normally. A 50-page PowerPoint with `--chunk-size 10` produces 5 chunks.
- The conversion step is logged in the execution log (visible in the Execution Log panel in the UI).
- In the Batch tab, use the **File types to process** multiselect to include non-PDF extensions.

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
  "active_machine": "Desktop",
  "machines": [
    {
      "name": "Desktop",
      "project_id": "my-gcp-project",
      "location": "europe-west3",
      "model": "gemini-2.5-pro",
      "auth_mode": "api",
      "refine_iterations": 0,
      "clean_stop_max_errors": 0,
      "diminishing_returns_enabled": true,
      "extraction_prompt": "prompts/extraction_rag.md",
      "refinement_prompt": "prompts/refinement_rag.md"
    }
  ],
  "processing": {
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

Set via `--auth-mode` CLI flag, the **Auth Mode** field in the **Advanced options** expander (per-run override), or `auth_mode` in the machine profile (persistent default via **Settings** tab).

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

To force all refinement passes to run regardless — for example, on critical documents where you want maximum scrutiny — set `diminishing_returns_enabled` to `false` in the machine profile, uncheck it in the Settings tab, or toggle it in the Advanced options of the Convert File / Batch Convert tabs.

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

## Verbose Mode — Intermediate File Saving

When **Verbose** is enabled in the **Convert File** tab, intermediate artifacts are saved next to the output Markdown file.

| File | When created | Content |
|------|-------------|---------|
| `{name}.pdf` | Non-PDF source + verbose | Converted PDF, saved before extraction starts |
| `{name}.raw_step_00.txt` | Vertex AI, verbose | Raw text response from the initial extraction call |
| `{name}.raw_step_NN.txt` | Vertex AI, verbose + refine | Raw JSON response from refinement pass N |
| `{name}.step_01.md` | Vertex AI, verbose | Processed markdown after extraction |
| `{name}.step_NN.md` | Vertex AI, verbose + refine | Processed markdown after refinement pass N |
| `{name}.chunk_NNN.md` | Verbose + chunking | Markdown for chunk NNN |
| `{name}.chunk_NNN.pdf` | Verbose + chunking | PDF page-range slice for chunk NNN |
| `{name}.chunk_NNN.corrections.md` | Chunking (always) | Corrections report for chunk NNN |

## Vertex AI Iterative Refinement

Set `--refine-iterations N` (or slider in UI) to run N self-correction passes after extraction. Each pass sends the PDF + current Markdown back to Gemini, which returns a structured JSON correction report. Stops early on `CLEAN` or diminishing returns (unless `diminishing_returns_enabled` is `false`).

Recommendation: 1–2 passes for most documents; 3–5 for complex tables / financial reports.

## Testing

The `testing/` folder contains a self-contained unit test suite covering the core library modules. No API credentials or external services are required.

### Run the tests

```bash
.venv\Scripts\python.exe -m pytest testing/ -v                                # Windows
.venv/bin/python -m pytest testing/ -v                                         # Linux/macOS
```

Run with coverage:

```bash
.venv\Scripts\python.exe -m pytest testing/ --cov=src --cov-report=term-missing
```

---

## Evolution Log — What Changed and Why

### Step 1 — Initial Pipeline

Built the foundational pipeline — Streamlit UI, CLI, pluggable backend system, and post-processing. Config-first architecture from day one.

### Step 2 — Vertex AI Backend and Refinement

Added Gemini multimodal extraction and iterative refinement loop. Dual auth modes (`api` vs `gcloud`) for personal and production use.

### Step 3 — Refinement Optimization

Stateless refinement: each pass only sees the PDF + current Markdown, not accumulated history. Simpler context → better reasoning.

### Step 4 — Multi-file Type Support + Test Suite

Pre-conversion of Office and image files to PDF. In-memory test fixtures (no binary blobs, no credentials needed).

### Step 5 — Prompt Redesign

New `extraction_rag.md` and `extraction_text.md` prompts. Dynamic prompt discovery from the `prompts/` folder.

### Step 6 — Verbose Mode and Universal Chunking

Raw API responses saved to disk immediately. Chunking extended to all file types. `--max-chunks N` for testing.

### Step 7 — Dual-handler Logging System

Two streams: clean console at INFO + full-detail rotating file at DEBUG. 8-char run ID for correlation.

### Step 8 — Settings Tab and Logging Config

Full `config.json` editable from the Streamlit Settings tab. All logging keys exposed in the UI.

### Step 9 — UI/UX Overhaul and Cross-platform Support

Cross-platform launchers, `pathlib.Path` throughout, robust JSON repair for malformed LLM responses.

### Step 10 — Diminishing Returns, Resume, and RAG Prompts

Resume from interrupted chunk runs, per-chunk corrections reports, `diminishing_returns_enabled` flag, and RAG-optimised prompts as defaults.

### Step 12 — UI Cleanup and Vertex AI Tab

**Problem:** The sidebar Auth Mode selector was redundant (already in Settings); the pricing refresh button was buried in Advanced options of the Convert tab; and advanced option layouts differed between Convert and Batch tabs, causing inconsistency.

**What was changed:**
- Removed **Auth Mode** from the sidebar. It is now a per-run override in the **Advanced options** expander on each tab (defaulting to the active machine's value).
- Added a dedicated **Vertex AI** tab showing the full pricing table, cache metadata (date + model count), a **Refresh pricing** button, and a dynamic link to the Google Cloud usage dashboard.
- Moved the pricing cache from `tmp/` (gitignored) to a new `pricing/` folder (tracked by git).
- Unified **Advanced options** layout across Convert File and Batch Convert tabs to match the **Settings** tab field order: Project ID / Location / Refinement Passes → Auth Mode / Model / Max Errors → prompts → processing → batch-specific options.
- Added a tooltip to **Validate after convert by default** in Settings explaining what validation does.
- Removed the internal JSON-repair unit tests for the VertexAI backend (implementation-detail tests, not public API).

### Step 11 — Single Backend + Machine Profiles

**Problem:** The multi-backend architecture (Vertex AI, Marker, pdfplumber) added complexity without real benefit — Vertex AI is the only backend used in practice. Project ID, location, and model were stored in `.env` alongside the API key, making it awkward to switch between machines.

**What was changed:**
- Removed Marker and pdfplumber backends. `src/vertexai_backend.py` is now the single backend file, directly in `src/`.
- Removed `PROJECT_ID`, `LOCATION`, and `MODEL_ID` from `.env` and `.env.example`. The `.env` file now only holds `GOOGLE_API_KEY`.
- Added **machine profiles** to `src/config.json`: each profile stores a full set of Vertex AI settings (project ID, location, model, auth mode, prompts, refinement settings). Switch machines in the sidebar; the app reloads instantly with the new profile's settings.
- The Settings tab now manages machine profiles (add, rename, delete, edit) in addition to processing/batch/logging settings.

**Key design decisions:**
- Machine profiles replace per-machine `.env` overrides. Instead of editing environment variables when switching between a home desktop and a work laptop, just select the right profile in the sidebar.
- `active_machine` is persisted in `config.json` so the last-used machine is remembered across restarts.
- A `Default` machine is created on first run if no machines are defined.

## License

MIT
