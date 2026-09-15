@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Forecast Community - Instalacao
echo ============================================
echo.

if not exist ".venv" (
    echo Criando ambiente virtual .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERRO: nao foi possivel criar o ambiente virtual.
        echo Verifique se o Python esta instalado e disponivel no PATH.
        pause
        exit /b 1
    )
) else (
    echo Ambiente virtual .venv ja existe.
)

echo.
echo Instalando dependencias...
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERRO: falha ao instalar as dependencias. Veja a mensagem acima.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Instalacao concluida com sucesso!
echo  Use "iniciar.bat" para abrir o Forecast Community.
echo ============================================
pause
