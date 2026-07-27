@echo off
:: update_helper.bat
:: Usage: update_helper.bat "target_exe" "new_exe"

set TARGET_EXE=%~1
set NEW_EXE=%~2

if "%TARGET_EXE%"=="" exit /b
if "%NEW_EXE%"=="" exit /b

:: Doi app chinh tat han
timeout /t 3 /nobreak >nul

:: Thu ghi de file moi vao file cu (thu 20 lan)
set RETRY=0
:COPY_LOOP
if %RETRY% GEQ 20 goto COPY_DONE
copy /Y "%NEW_EXE%" "%TARGET_EXE%" >nul 2>&1
if %ERRORLEVEL%==0 goto COPY_DONE
set /a RETRY+=1
timeout /t 1 /nobreak >nul
goto COPY_LOOP

:COPY_DONE
:: Doi on dinh roi khoi dong lai app chinh
timeout /t 2 /nobreak >nul
start "" "%TARGET_EXE%"

:: Xoa file cap nhat rac
del /f /q "%NEW_EXE%" >nul 2>&1
