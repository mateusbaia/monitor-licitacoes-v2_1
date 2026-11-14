@echo off
REM Depois de colar o bot_token no config.json e mandar um "oi" pro seu bot,
REM rode este arquivo: ele descobre seu chat_id, salva e manda uma mensagem de teste.
cd /d "%~dp0"
python monitor_licitacoes.py --descobrir-chat-id
if errorlevel 1 py monitor_licitacoes.py --descobrir-chat-id
pause
