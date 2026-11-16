@echo off
REM ===========================================================================
REM  AgroAnalise - inicializacao no Windows (sem systemd).
REM  Abre tres janelas: o coletor, o servidor web e o alarme sonoro local
REM  (bipes no alto-falante do computador quando a cor vira Vermelho, mesmo
REM  sem celular aberto). Cada uma fica num laco: se o programa cair, ele volta
REM  sozinho em 5 segundos.
REM  Para iniciar junto com o Windows, crie um atalho deste arquivo em
REM  shell:startup (Win+R, digite shell:startup).
REM  Uso interno: iniciar_windows.bat coletor | web | alarme  (roda so um dos lacos).
REM ===========================================================================
setlocal
cd /d "%~dp0\.."
set "PYTHON=.venv\Scripts\python.exe"

if /i "%~1"=="coletor" goto laco_coletor
if /i "%~1"=="web" goto laco_web
if /i "%~1"=="alarme" goto laco_alarme

if not exist "%PYTHON%" (
    echo [AgroAnalise] Criando o ambiente virtual .venv ...
    python -m venv .venv || goto erro_python
)
echo [AgroAnalise] Instalando as dependencias ...
"%PYTHON%" -m pip install -r requirements.txt || goto erro_pip

if not exist "models\modelo_agroanalise.pkl" (
    echo [AgroAnalise] Primeira execucao: gerando banco, dataset e modelo de IA ...
    "%PYTHON%" src\database.py || goto erro_pipeline
    "%PYTHON%" src\gerador_dataset.py || goto erro_pipeline
    "%PYTHON%" src\treinamento_ia.py || goto erro_pipeline
)

start "AgroAnalise - Coletor" cmd /c ""%~f0" coletor"
start "AgroAnalise - Servidor Web" cmd /c ""%~f0" web"
start "AgroAnalise - Alarme" /min cmd /c ""%~f0" alarme"
echo [AgroAnalise] Pronto! Abra http://localhost:5000 no navegador.
goto fim

:laco_coletor
echo [AgroAnalise] Coletor iniciado em %date% %time%
"%PYTHON%" src\coletor_serial.py
echo [AgroAnalise] O coletor parou (codigo %errorlevel%). Reiniciando em 5 segundos...
timeout /t 5 /nobreak >nul
goto laco_coletor

:laco_web
echo [AgroAnalise] Servidor web iniciado em %date% %time%
"%PYTHON%" src\servidor.py
echo [AgroAnalise] O servidor parou (codigo %errorlevel%). Reiniciando em 5 segundos...
timeout /t 5 /nobreak >nul
goto laco_web

:laco_alarme
echo [AgroAnalise] Alarme local iniciado em %date% %time%
"%PYTHON%" src\alarme_local.py
echo [AgroAnalise] O alarme parou (codigo %errorlevel%). Reiniciando em 5 segundos...
timeout /t 5 /nobreak >nul
goto laco_alarme

:erro_python
echo [AgroAnalise] ERRO: Python nao encontrado. Instale o Python 3 em python.org (marque "Add to PATH").
pause
exit /b 1

:erro_pip
echo [AgroAnalise] ERRO: nao foi possivel instalar as dependencias (requirements.txt).
pause
exit /b 1

:erro_pipeline
echo [AgroAnalise] ERRO: falha ao gerar o banco, o dataset ou o modelo de IA.
pause
exit /b 1

:fim
endlocal
