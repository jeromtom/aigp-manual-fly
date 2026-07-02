@echo off
rem aigp-manual-fly launcher. TRAINING EVENTS ONLY.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First run: creating venv and installing dependencies...
    python -m venv .venv || goto :err
    .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :err
)
.venv\Scripts\python.exe manual_fly.py %*
goto :eof
:err
echo Setup failed. Install Python 3.10+ from python.org and ensure internet for the one-time install.
pause
