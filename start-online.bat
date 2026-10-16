@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   CAO BINH LUAN - CHAY ONLINE (Cloudflare Tunnel)
echo ================================================
echo.

rem --- cai thu vien python neu thieu ---
python -c "import httpx,fastapi,uvicorn,playwright,cryptography,openpyxl" 2>nul || python -m pip install -q -r requirements.txt

rem --- tai cloudflared.exe neu chua co ---
if not exist cloudflared.exe (
  echo Dang tai cloudflared.exe ...
  powershell -Command "Invoke-WebRequest -Uri https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile cloudflared.exe"
)

rem --- chay app trong cua so rieng (xem log + mat khau admin o do) ---
start "Cao binh luan - APP (giu mo)" cmd /k python app.py --no-browser

echo Dang khoi dong app, cho vai giay...
timeout /t 6 >nul

echo.
echo ====================  LINK CHIA SE  ====================
echo  Tim dong "https://....trycloudflare.com" ben duoi va gui
echo  cho nguoi dung. Trang quan tri: them /admin vao cuoi link.
echo  GIU 2 cua so nay mo thi web moi song. Dong la web tat.
echo  (Tat/mo lai se doi link.)
echo ========================================================
echo.
cloudflared.exe tunnel --url http://localhost:8765
pause
