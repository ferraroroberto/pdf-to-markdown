#!/bin/bash
# Launch the Streamlit Application

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if virtual environment exists
if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] Virtual environment not found!"
    echo "[ERROR] Please run the setup script first."
    echo ""
    echo "  python -m venv .venv"
    echo "  .venv/bin/pip install -r requirements.txt"
    echo ""
    exit 1
fi

# Launch Streamlit
.venv/bin/python -m streamlit run app/app.py --server.enableXsrfProtection false --server.enableCORS false
