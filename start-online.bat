@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   CAO BINH LUAN - CHAY ONLINE (Cloudflare Tunnel)
echo ================================================
echo.

rem --- chong may/card mang ngu khi khoa man hinh (giu web song 24/7) ---
powercfg -change -standby-timeout-ac 0 >nul 2>nul
powercfg -change -hibernate-timeout-ac 0 >nul 2>nul
powercfg -change -disk-timeout-ac 0 >nul 2>nul

rem --- cai thu vien python neu thieu ---
python -c "import httpx,fastapi,uvicorn,playwright,cryptography,openpyxl,PIL" 2>nul || python -m pip install -q -r requirements.txt

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
echo  LUU Y: neu tunnel roi, no tu noi lai - nhung LINK SE DOI.
echo  Muon link CO DINH 24/7 on dinh: dung "tunnel co dinh" (xem
echo  DEPLOY_CLOUDFLARE.md) hoac hoi de duoc huong dan.
echo ========================================================
echo.

:tunnel
cloudflared.exe tunnel --no-autoupdate --protocol http2 --url http://localhost:8765
echo.
echo [!] Tunnel bi roi luc %date% %time%. Dang noi lai sau 3 giay...
timeout /t 3 >nul
goto tunnel
