@echo off
REM Cria uma tarefa no Agendador do Windows que roda o monitor a cada 2 horas, 24h por dia.
REM Clique com o botao direito > Executar como administrador (se pedir).
cd /d "%~dp0"
for /f "delims=" %%i in ('where pythonw 2^>nul') do (set PY=%%i & goto achou)
for /f "delims=" %%i in ('where python 2^>nul') do (set PY=%%i & goto achou)
echo Python nao encontrado. Instale em https://www.python.org/downloads/ marcando "Add Python to PATH".
pause
exit /b 1
:achou
set PY=%PY: =%
schtasks /Create /F /SC HOURLY /MO 2 /TN "MonitorLicitacoes" /TR "\"%PY%\" \"%~dp0monitor_licitacoes.py\""
echo.
echo Pronto! O monitor vai rodar a cada 2 horas. Abra o relatorio.html nesta pasta para ver as oportunidades.
echo Para remover: schtasks /Delete /TN "MonitorLicitacoes" /F
pause
