# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

## Architecture

```
PDF Input
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
│   ├── logger_exec.py      # Structured JSONL execution logger
│   ├── models.py           # ConversionResult, ChunkResult, BatchResult, ValidationReport
│   ├── pipeline.py         # Single-file orchestrator
│   ├── postprocess.py      # Cleaning pipeline
│   ├── validation.py       # Quality validation
│   └── vertexai_pricing.py # Gemini pricing fetch and cache
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

## Backends

| Backend    | Type        | Scanned PDFs | Notes |
|------------|-------------|--------------|-------|
| Vertex AI  | Cloud LLM   | Yes          | Primary — `google-genai` required |
| Marker     | ML-powered  | Yes (OCR)    | Secondary — `marker-pdf` required |
| pdfplumber | Heuristic   | No           | Secondary — always available |

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

## License

MIT
