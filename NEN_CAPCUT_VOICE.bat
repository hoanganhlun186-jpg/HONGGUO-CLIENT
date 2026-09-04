@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo ==========================================
echo   DONG GOI CAPCUT VOICE SOURCE
echo ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python trong PATH.
    echo Hay mo CMD tai thu muc nay va thu: python --version
    pause
    exit /b 1
)

set "OUT=%~dp0capcut_voice_source.zip"

if exist "%OUT%" del /f /q "%OUT%"

python -c "import os,zipfile; root=os.getcwd(); items=['capcut_tts_api','voice_samples','capcut_widget.py','honggou_tab.py']; out=os.path.join(root,'capcut_voice_source.zip'); z=zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED); [(lambda p: [z.write(os.path.join(dp,f),os.path.relpath(os.path.join(dp,f),root)) for dp,ds,fs in os.walk(p) for f in fs if '__pycache__' not in dp and not f.endswith(('.pyc','.pyo'))])(os.path.join(root,x)) if os.path.isdir(os.path.join(root,x)) else z.write(os.path.join(root,x),x) if os.path.isfile(os.path.join(root,x)) else print('[CANH BAO] Khong tim thay:',x) for x in items]; z.close(); print('DA TAO:',out)"

if errorlevel 1 (
    echo.
    echo [LOI] Nen that bai.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo THANH CONG
echo File: capcut_voice_source.zip
echo ==========================================
echo.
pause
