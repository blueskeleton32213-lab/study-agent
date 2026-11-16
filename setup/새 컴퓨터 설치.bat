@echo off
rem Runs setup.ps1 next to this file (new PC setup for the study tutor app)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
