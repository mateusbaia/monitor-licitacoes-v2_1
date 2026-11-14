# -*- coding: utf-8 -*-
"""Servidor local do dashboard: serve o front-end e uma API JSON.

Uso:  python monitor_licitacoes.py --servir
Só escuta em 127.0.0.1 (não fica exposto na rede).
"""
import subprocess
import webbrowser

PORTA_PADRAO = 8000


def url_local(porta=PORTA_PADRAO):
    return f"http://localhost:{int(porta)}/"


def detectar_wsl():
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def comando_abrir(url, em_wsl):
    """Comando para abrir a URL no navegador do Windows (WSL); None = usar o padrão."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(f"URL inválida para abrir: {url!r}")
    if em_wsl:
        return ["cmd.exe", "/c", "start", "", url]
    return None


def abrir_navegador(url):
    """Abre o navegador sem nunca derrubar o servidor. Devolve True se conseguiu disparar."""
    try:
        cmd = comando_abrir(url, detectar_wsl())
        if cmd:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        return bool(webbrowser.open(url))
    except (OSError, ValueError):
        return False
