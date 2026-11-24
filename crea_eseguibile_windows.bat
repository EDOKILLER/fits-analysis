@echo off
REM ===========================================================================
REM Crea AnalisiFITS.exe per Windows 11 (da fare UNA sola volta).
REM L'eseguibile finale funziona senza Python, senza JupyterLab e senza pacchetti.
REM
REM Prima serve Python per la sola compilazione: scaricalo da python.org e
REM durante l'installazione spunta "Add python.exe to PATH".
REM
REM Uso: metti questo file nella stessa cartella di analisi_fits.py e fai
REM      doppio clic su crea_eseguibile_windows.bat
REM ===========================================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist analisi_fits.py (
    echo ERRORE: analisi_fits.py non e' in questa cartella: %cd%
    pause
    exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo ERRORE: Python non trovato. Installalo da https://www.python.org
    echo spuntando "Add python.exe to PATH", poi riavvia questo file.
    pause
    exit /b 1
)

echo === 1/3  Ambiente Python temporaneo per la compilazione ===
if not exist .venv-build (%PY% -m venv .venv-build || goto errore)
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip || goto errore
python -m pip install numpy scipy matplotlib astropy photutils psutil pyinstaller || goto errore

echo === 2/3  Creazione dell'eseguibile (qualche minuto) ===
pyinstaller --noconfirm --clean --onefile --windowed ^
    --name AnalisiFITS ^
    --collect-all astropy ^
    --collect-all photutils ^
    --collect-all erfa ^
    --collect-submodules scipy ^
    --collect-binaries scipy ^
    --collect-submodules matplotlib ^
    --collect-data astropy_iers_data ^
    --copy-metadata scipy ^
    --copy-metadata numpy ^
    --copy-metadata astropy ^
    --copy-metadata photutils ^
    --copy-metadata matplotlib ^
    --copy-metadata pyerfa ^
    --copy-metadata astropy-iers-data ^
    --copy-metadata packaging ^
    --copy-metadata pyyaml ^
    --copy-metadata psutil ^
    --copy-metadata pillow ^
    --hidden-import matplotlib.backends.backend_tkagg ^
    --hidden-import PIL._tkinter_finder ^
    analisi_fits.py || goto errore
call deactivate

echo === Verifica dell'eseguibile (autotest) ===
del /q autotest_risultato.txt 2>nul
dist\AnalisiFITS.exe --autotest > autotest.txt 2>&1
if exist autotest_risultato.txt type autotest_risultato.txt >> autotest.txt
type autotest.txt
findstr /c:"AUTOTEST SUPERATO" autotest.txt >nul || (
    echo.
    echo L'autotest e' FALLITO: i dettagli sono in %cd%\autotest.txt
    goto errore
)

echo === 3/3  Collegamento sul Desktop ===
copy /y dist\AnalisiFITS.exe "%cd%\AnalisiFITS.exe" >nul
powershell -NoProfile -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Analisi FITS.lnk\"); $s.TargetPath='%cd%\AnalisiFITS.exe'; $s.WorkingDirectory='%cd%'; $s.Save()"

rmdir /s /q .venv-build build 2>nul
del /q AnalisiFITS.spec 2>nul

echo.
echo FATTO.
echo   Eseguibile: %cd%\AnalisiFITS.exe
echo   Sul Desktop trovi il collegamento "Analisi FITS".
echo   Puoi copiare AnalisiFITS.exe su qualsiasi PC Windows: non serve altro.
pause
exit /b 0

:errore
echo.
echo Si e' verificato un errore: copia i messaggi qui sopra e inviameli.
pause
exit /b 1
