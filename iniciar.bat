@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo O ambiente virtual .venv nao foi encontrado.
    echo Rode "instalar.bat" primeiro.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m streamlit run app.py
