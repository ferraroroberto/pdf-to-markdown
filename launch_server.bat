@echo off
setlocal
cd /d "%~dp0"

set PORT=8501

:: --- Check dependencies ---
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found!
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

where cloudflared >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cloudflared is not installed.
    echo.
    echo   Install it first:
    echo     winget install Cloudflare.cloudflared
    echo     -- or --
    echo     https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
    echo.
    pause
    exit /b 1
)

:: --- Start Streamlit in the background ---
echo [1/2] Starting Streamlit on port %PORT% ...
start "pdf2md-streamlit" /B ".venv\Scripts\python.exe" -m streamlit run "app\app.py" ^
    --server.port %PORT% ^
    --server.headless true ^
    --browser.gatherUsageStats=false

:: Give Streamlit a moment to start
timeout /t 3 /nobreak >nul

:: --- Start Cloudflare Tunnel ---
echo [2/2] Opening Cloudflare Tunnel ...
echo.
echo   Share the https:// URL printed below with anyone.
echo   Press Ctrl+C to stop the tunnel, then close this window.
echo.
cloudflared tunnel --url http://localhost:%PORT%

:: Cleanup: kill the Streamlit process
taskkill /fi "windowtitle eq pdf2md-streamlit" /f >nul 2>&1
echo.
echo Server stopped.
pause
