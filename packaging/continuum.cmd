@echo off
setlocal
for %%I in ("%~dp0..") do set "CONTINUUM_BUNDLE_ROOT=%%~fI"
set "PYTHONHOME=%CONTINUUM_BUNDLE_ROOT%\runtime"
set "PYTHONPATH=%CONTINUUM_BUNDLE_ROOT%\app"
set "CONTINUUM_BUNDLE_MANIFEST=%CONTINUUM_BUNDLE_ROOT%\runtime-manifest.json"
"%CONTINUUM_BUNDLE_ROOT%\runtime\python.exe" -m continuum %*
exit /b %ERRORLEVEL%
