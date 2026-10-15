@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   DANG NHAP FACEBOOK (chi lam 1 lan)
echo ==========================================
echo Mot cua so trinh duyet se mo ra.
echo Hay dang nhap Facebook trong cua so do (ca 2FA neu co).
echo Dang nhap xong, cua so se tu dong dong va luu phien.
echo.
python -c "import playwright" 2>nul || python -m pip install -q playwright
python fb_browser.py login
echo.
pause
