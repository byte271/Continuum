@echo off
setlocal
call "%~dp0..\lib\continuum-windows-x86_64\bin\continuum.cmd" %*
exit /b %ERRORLEVEL%
