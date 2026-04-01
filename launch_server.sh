#!/bin/bash
# Launch the Streamlit app and expose it to the internet via Cloudflare Tunnel.
# Your API keys stay on this machine — only the web UI is shared.
#
# Prerequisites:
#   sudo apt install cloudflared          # Debian/Ubuntu
#   brew install cloudflared              # macOS
#   See: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=8501

# --- Check dependencies ---
if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] Virtual environment not found!"
    echo "  python -m venv .venv"
    echo "  .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if ! command -v cloudflared &> /dev/null; then
    echo "[ERROR] cloudflared is not installed."
    echo ""
    echo "  Install it first:"
    echo "    Debian/Ubuntu : sudo apt install cloudflared"
    echo "    macOS         : brew install cloudflared"
    echo "    Other         : https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    echo ""
    exit 1
fi

# --- Start Streamlit in the background ---
# Tell the app it is being accessed remotely (disables native file dialogs,
# enables browser-based drag-and-drop upload instead).
export PDF2MD_REMOTE=1

echo "[1/2] Starting Streamlit on port $PORT ..."
.venv/bin/python -m streamlit run app/app.py \
    --server.port "$PORT" \
    --server.enableXsrfProtection false \
    --server.enableCORS false \
    --server.headless true &
STREAMLIT_PID=$!

# Give Streamlit a moment to bind the port
sleep 3

if ! kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    echo "[ERROR] Streamlit failed to start."
    exit 1
fi

# --- Start Cloudflare Tunnel ---
echo "[2/2] Opening Cloudflare Tunnel ..."
echo ""
echo "  Share the https:// URL printed below with anyone."
echo "  Press Ctrl+C to stop both the tunnel and Streamlit."
echo ""
cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | awk '!/Cannot determine default origin certificate path/'

# When the tunnel is stopped (Ctrl+C), also stop Streamlit
kill "$STREAMLIT_PID" 2>/dev/null
wait "$STREAMLIT_PID" 2>/dev/null
echo ""
echo "Server stopped."
