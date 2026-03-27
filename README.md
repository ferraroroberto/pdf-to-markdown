# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

## Architecture

```
PDF Input
    │
    ├─► Classify (born-digital vs scanned)
    ├─► Extract via backend (Marker / Vertex AI / pdfplumber)
    ├─► Post-process (clean headers/footers, normalize, fix paragraphs)
    ├─► Validate (char similarity, structural checks, table integrity)
    │
    └─► Markdown Output + ValidationReport + token estimate
```

## Project structure

```
pdf-to-markdown/
├── .venv/                  # Virtual environment (create with python -m venv .venv)
├── app/                    # Streamlit UI
│   ├── app.py              # Entry point (run with streamlit run app/app.py)
│   ├── execute.py          # Execute tab logic
│   └── .streamlit/         # Streamlit config
├── prompts/                # Prompt templates for Vertex AI backend
│   ├── extraction.md
│   └── refinement.md
├── src/
│   ├── backends/           # Extraction backends (Marker, Vertex AI, pdfplumber)
│   ├── classifier.py       # Born-digital vs scanned detection
│   ├── cli.py              # CLI entry point
│   ├── config.json         # Runtime configuration
│   ├── models.py           # ConversionResult, ValidationReport
│   ├── pipeline.py         # Main orchestrator
│   ├── postprocess.py      # Cleaning pipeline
│   ├── validation.py       # Quality validation
│   └── vertexai_pricing.py # Gemini pricing fetch and cache
├── launch_app.bat          # Start Streamlit app (Windows)
├── requirements.txt
└── README.md
```

## Setup

Use a virtual environment in the project so dependencies are not installed into system Python. From the project root:

```bash
# Create .venv in the workspace
python -m venv .venv

# Install dependencies into .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

All backends (including `marker-pdf`) are listed in `requirements.txt`. Omit the optional ML packages if you only need pdfplumber.

### CUDA support (GPU acceleration for Marker)

The Marker backend uses PyTorch and can run on your NVIDIA GPU if you install a CUDA-enabled PyTorch build **after** installing `requirements.txt`.

**Most NVIDIA GPUs (up to Ada Lovelace, e.g. RTX 40 series):**

```bash
.venv\Scripts\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

**RTX 50 series / Blackwell (sm_120) and other very new GPUs:**  
Stable PyTorch does not yet include kernels for sm_120. Use the nightly build with CUDA 12.8:

```bash
.venv\Scripts\python.exe -m pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
```

**Verify CUDA in your venv:**

```bash
.venv\Scripts\python.exe -c "import torch; print('CUDA:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

If you see a warning that your GPU (e.g. RTX 5060 Ti) is not compatible with the current PyTorch install, switch to the nightly cu128 index above. If you prefer to run on CPU only, use `--device cpu` when converting (e.g. `.venv\Scripts\python.exe -m src.cli convert doc.pdf --device cpu`).

## Usage

Run all commands from the project root using the `.venv` interpreter directly.

### Web UI (Streamlit)

```bash
.venv\Scripts\streamlit.exe run app/app.py
```

On Windows you can double-click `launch_app.bat` to start the app with the project venv.

### CLI

```bash
# Convert a single PDF
.venv\Scripts\python.exe -m src.cli convert document.pdf -o output/

# Convert with validation
.venv\Scripts\python.exe -m src.cli convert document.pdf -o output/ --validate

# Convert a directory (parallel)
.venv\Scripts\python.exe -m src.cli convert input_pdfs/ -o output_markdown/ --workers 4

# Choose a specific backend
.venv\Scripts\python.exe -m src.cli convert document.pdf -b pdfplumber -o output/

# List available backends
.venv\Scripts\python.exe -m src.cli backends

# Validate an existing conversion
.venv\Scripts\python.exe -m src.cli validate document.pdf output/document.md
```

### Python API

```python
from src import Pipeline

result = Pipeline().convert("document.pdf")
print(result.markdown[:500])
print(f"~{result.token_estimate:,} tokens")

# With validation
result = Pipeline().convert("document.pdf", validate_output=True)
print(result.validation.summary())

# Save to file
result.save("output/document.md")
```

## Backends

| Backend    | Type        | Scanned PDFs | Notes                         |
|------------|-------------|--------------|-------------------------------|
| Marker     | ML-powered  | Yes (OCR)    | Install `marker-pdf`          |
| Vertex AI  | Cloud LLM   | Yes          | Install `google-genai`        |
| pdfplumber | Heuristic   | No           | Included in requirements      |

The pipeline picks the best available backend automatically.

### Vertex AI Backend (Gemini)

The `vertexai` backend uses Google Gemini via the Vertex AI API for high-fidelity PDF extraction. It natively handles both born-digital and scanned PDFs and supports optional iterative self-refinement for maximum accuracy.

#### Prerequisites

1. A Google Cloud project with the **Vertex AI API** enabled.
2. The `google-genai` package: `pip install google-genai>=1.0.0`
3. Authentication:
   ```bash
   gcloud auth application-default login
   # or set GOOGLE_APPLICATION_CREDENTIALS to your service account key path
   ```

#### Configuration

Set via environment variables or the Streamlit UI:

| Variable      | Default          | Description                              |
|---------------|------------------|------------------------------------------|
| `PROJECT_ID`  | *(required)*     | Your Google Cloud project ID             |
| `LOCATION`    | `europe-west3`   | Vertex AI region (e.g. `us-central1`)    |
| `MODEL_ID`    | `gemini-2.5-pro` | Gemini model string                      |

```bash
# Example .env or shell exports
export PROJECT_ID=my-gcp-project
export LOCATION=europe-west3
export MODEL_ID=gemini-2.5-pro
```

Runtime options are read from `src/config.json`:

```json
{
  "vertexai": {
    "clean_stop_max_errors": 0
  }
}
```

| Key                    | Default | Description                                                                 |
|------------------------|---------|-----------------------------------------------------------------------------|
| `clean_stop_max_errors`| `-1`    | Early-stop threshold during refinement. `-1` = stop on any CLEAN verdict; `0` = only stop if 0 errors found; `N` = stop if errors ≤ N. |

#### Iterative Refinement

Set `--refine-iterations N` (CLI) or the slider in the UI to run N additional self-correction passes after the initial extraction. Each pass sends the PDF and the current Markdown back to Gemini and asks it to audit and fix errors, returning a structured JSON correction report. Refinement stops early when the document is declared `CLEAN` or when two consecutive passes show no improvement.

Use refinement when:
- The PDF contains complex tables or dense numerical data
- High-fidelity extraction is critical (e.g. financial reports, regulatory documents)
- A single extraction pass leaves visible errors

Typical recommendation: 1–2 passes for most documents; 3–5 passes for complex multi-table documents.

#### CLI usage

```bash
# Extraction only
.venv\Scripts\python.exe -m src.cli convert document.pdf -b vertexai

# With 3 refinement passes
.venv\Scripts\python.exe -m src.cli convert document.pdf -b vertexai --refine-iterations 3

# Override project/model
$env:PROJECT_ID="my-project"; $env:MODEL_ID="gemini-2.5-flash"; .venv\Scripts\python.exe -m src.cli convert doc.pdf -b vertexai
```

#### Prompt files

The backend reads prompt templates from:
- `prompts/extraction.md` — extraction instructions sent with the PDF
- `prompts/refinement.md` — quality criteria used during refinement passes

Override the paths via the UI or by passing `extraction_prompt_file` / `refinement_prompt_file` kwargs.

## License

MIT
