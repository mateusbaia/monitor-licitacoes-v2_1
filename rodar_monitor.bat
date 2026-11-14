@echo off
REM Roda o monitor e abre o relatorio no navegador.
cd /d "%~dp0"
python monitor_licitacoes.py --abrir
if errorlevel 1 py monitor_licitacoes.py --abrir
pause
