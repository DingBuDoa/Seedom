@echo off
setlocal
cd /d "%~dp0"

rem Development launcher: reuse the archived Qt/Python runtime while the
rem Seedom 2.0 runtime bundle is being assembled. All files and output stay
rem in this checkout.
set "DEV_PY=%~dp0..\..\archive\legacy-1.x\runtime\python\python.exe"
if not exist "%DEV_PY%" (
    echo Missing development Python runtime:
    echo %DEV_PY%
    pause
    exit /b 1
)
set PYTHONHOME=
set PYTHONNOUSERSITE=1
rem The archived embedded Python keeps the 1.x app directory on sys.path.
rem Put the current 2.0 app first so this launcher never imports legacy modules.
set "PATH=%~dp0..\..\archive\legacy-1.x\runtime\python;%~dp0..\..\archive\legacy-1.x\runtime\python\DLLs;%PATH%"
if not exist "logs" mkdir "logs"
rem The embedded interpreter ignores PYTHONPATH, so inject the current app
rem directory before executing launch.py.
"%DEV_PY%" -c "import runpy,sys; sys.path.insert(0, r'%~dp0app'); runpy.run_path(r'%~dp0app\launch.py', run_name='__main__')" %*
endlocal
