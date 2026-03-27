# pdf2md

Convert PDF documents into clean, structured, token-efficient Markdown for downstream LLM consumption (RAG, summarization, Q&A).

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

## Project structure

```
pdf-to-markdown/
├── .venv/              # Virtual environment (create with python -m venv .venv)
├── app/                # Streamlit UI
│   ├── app.py          # Entry point (run with streamlit run app/app.py)
│   └── .streamlit/     # Streamlit config
├── src/
│   ├── backends/       # Extraction backends (Marker, Docling, pdfplumber)
│   ├── classifier.py   # Born-digital vs scanned detection
│   ├── cli.py          # CLI entry point
│   ├── models.py       # ConversionResult, ValidationReport
│   ├── pipeline.py     # Main orchestrator
│   ├── postprocess.py  # Cleaning pipeline
│   └── validation.py   # Quality validation
├── test/
│   └── test_pipeline.py
├── pipeline.py         # Run the pipeline (python pipeline.py ...)
├── launch_app.bat      # Start Streamlit app (Windows)
├── requirements.txt
└── README.md
```

## Setup

Use a virtual environment in the project so dependencies are not installed into system Python. From the project root:

```bash
# Create .venv in the workspace
python -m venv .venv

# Activate it (Windows)
.venv\Scripts\activate

# Activate it (Linux/macOS)
# source .venv/bin/activate

# Install dependencies into .venv only
pip install -r requirements.txt
```

All backends (including `marker-pdf` and `docling`) are listed in `requirements.txt`; install with `pip install -r requirements.txt`. Omit the optional ML packages if you only need pdfplumber.

### CUDA support (GPU acceleration for Marker / Docling)

The Marker and Docling backends use PyTorch and can run on your NVIDIA GPU if you install a CUDA-enabled PyTorch build **after** installing `requirements.txt`. Use the same venv (e.g. `.venv`).

**Most NVIDIA GPUs (up to Ada Lovelace, e.g. RTX 40 series):**

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

**RTX 50 series / Blackwell (sm_120) and other very new GPUs:**  
Stable PyTorch does not yet include kernels for sm_120. Use the nightly build with CUDA 12.8:

```bash
pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
```

**Verify CUDA in your venv:**

```bash
python -c "import torch; print('CUDA:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

If you see a warning that your GPU (e.g. RTX 5060 Ti) is not compatible with the current PyTorch install, switch to the nightly cu128 index above. If you prefer to run on CPU only, use `--device cpu` when converting (e.g. `python pipeline.py convert doc.pdf --device cpu`).

## Usage

Run all commands from the project root with the venv activated (so `python` and `pytest` use `.venv`).

### Web UI (Streamlit)

```bash
streamlit run app/app.py
```

On Windows you can double-click `launch_app.bat` (or run it from a terminal) to start the app with the project venv.

### CLI

```bash
# Convert a single PDF
python pipeline.py convert document.pdf -o output/

# Convert with validation
python pipeline.py convert document.pdf -o output/ --validate

# Convert a directory (parallel)
python pipeline.py convert input_pdfs/ -o output_markdown/ --workers 4

# Choose a specific backend
python pipeline.py convert document.pdf -b pdfplumber -o output/

# List available backends
python pipeline.py backends

# Validate an existing conversion
python pipeline.py validate document.pdf output/document.md
```

Alternatively: `python -m src.cli convert document.pdf -o output/`

### Python API

From the project root (so `src` is on the path):

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
| Docling    | ML-powered  | Yes (OCR)    | Install `docling`             |
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
python pipeline.py convert document.pdf -b vertexai

# With 3 refinement passes
python pipeline.py convert document.pdf -b vertexai --refine-iterations 3

# Override project/model
PROJECT_ID=my-project MODEL_ID=gemini-2.5-flash python pipeline.py convert doc.pdf -b vertexai
```

#### Prompt files

The backend reads prompt templates from:
- `prompts/extraction.md` — extraction instructions sent with the PDF
- `prompts/refinement.md` — quality criteria used during refinement passes

Override the paths via the UI or by passing `extraction_prompt_file` / `refinement_prompt_file` kwargs.

## Tests

```bash
pytest
```

Run from the project root so `src` is importable.

## License

MIT
