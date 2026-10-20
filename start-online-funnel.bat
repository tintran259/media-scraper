@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   CAO BINH LUAN - LINK CO DINH (Tailscale Funnel)
echo ================================================
echo.

rem --- chong may/card mang ngu khi khoa man hinh (giu web song 24/7) ---
powercfg -change -standby-timeout-ac 0 >nul 2>nul
powercfg -change -hibernate-timeout-ac 0 >nul 2>nul
powercfg -change -disk-timeout-ac 0 >nul 2>nul

rem --- kiem tra da cai Tailscale chua ---
where tailscale >nul 2>nul
if errorlevel 1 (
  if exist "%ProgramFiles%\Tailscale\tailscale.exe" (
    set "PATH=%PATH%;%ProgramFiles%\Tailscale"
  ) else (
    echo [!] Chua cai Tailscale.
    echo     Tai va cai tai: https://tailscale.com/download/windows
    echo     Cai xong, dang nhap Tailscale 1 lan, roi chay lai file nay.
    pause
    exit /b 1
  )
)

rem --- dam bao da dang nhap tailnet ---
tailscale status >nul 2>nul
if errorlevel 1 (
  echo Dang mo trinh duyet de dang nhap Tailscale...
  tailscale up
)

rem --- cai thu vien python neu thieu ---
python -c "import httpx,fastapi,uvicorn,playwright,cryptography,openpyxl,PIL" 2>nul || python -m pip install -q -r requirements.txt

rem --- chay app trong cua so rieng (xem log + mat khau admin o do) ---
start "Cao binh luan - APP (giu mo)" cmd /k python app.py --no-browser

echo Dang khoi dong app, cho vai giay...
timeout /t 6 >nul

echo.
echo ===============  LINK CO DINH (chia se)  ===============
echo  Dong "https://....ts.net" ben duoi la LINK CO DINH cua ban.
echo  Gui cho nguoi dung. Trang quan tri: them /admin vao cuoi link.
echo  Link nay KHONG DOI du roi mang/khoi dong lai.
echo  Neu bao "Funnel is not enabled": mo link no in ra de bat, roi
echo  chay lai file nay (chi lam 1 lan).
echo ========================================================
echo.

:funnel
tailscale funnel 8765
echo.
echo [!] Funnel dung luc %date% %time%. Dang chay lai sau 3 giay...
timeout /t 3 >nul
goto funnel
