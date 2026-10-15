@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import httpx, fastapi, uvicorn, playwright" 2>nul || python -m pip install -q -r requirements.txt
python app.py
pause
