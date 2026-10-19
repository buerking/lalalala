@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python tools/export_yahoo_bargain_stats.py --open
if errorlevel 1 pause
