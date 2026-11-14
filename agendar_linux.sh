#!/usr/bin/env bash
# Agenda o monitor no cron (Linux/WSL/Mac) para rodar a cada 2 horas.
# Uso: bash agendar_linux.sh
PASTA="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then echo "Python não encontrado."; exit 1; fi
LINHA="0 */2 * * * cd \"$PASTA\" && \"$PY\" monitor_licitacoes.py >> \"$PASTA/cron.log\" 2>&1"
( crontab -l 2>/dev/null | grep -v "monitor_licitacoes.py"; echo "$LINHA" ) | crontab -
echo "Pronto! Agendado a cada 2 horas:"
crontab -l | grep monitor_licitacoes.py
echo "Para remover: crontab -l | grep -v monitor_licitacoes.py | crontab -"
