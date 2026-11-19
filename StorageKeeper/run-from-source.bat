@echo off
REM Run StorageKeeper without building the .exe (needs Python 3.10+ from python.org)
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
python storagekeeper.py
