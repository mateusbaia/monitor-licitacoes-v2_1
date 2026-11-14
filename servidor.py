# -*- coding: utf-8 -*-
"""Servidor local do dashboard: serve o front-end e uma API JSON.

Uso:  python monitor_licitacoes.py --servir
Só escuta em 127.0.0.1 (não fica exposto na rede).
"""
import json
import os
import subprocess
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor_licitacoes as m

PORTA_PADRAO = 8000
PASTA = os.path.dirname(os.path.abspath(__file__))
LIMITE_CORPO = 64 * 1024
STATUS_VALIDOS = ("interessante", "descartada")
HOSTS_OK = ("localhost", "127.0.0.1", "[::1]")


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


class Armazem:
    """Lê/grava os dados numa pasta (injetável, para os testes usarem uma temporária)."""

    def __init__(self, pasta, cfg=None):
        self.pasta = pasta
        self.cfg = cfg or {}
        self.arq_banco = os.path.join(pasta, "banco_oportunidades.json")
        self.arq_estado = os.path.join(pasta, "estado_execucao.json")
        self.arq_marc = os.path.join(pasta, "marcacoes.json")
        self.trava = threading.Lock()

    def banco(self):
        return m.carregar_json(self.arq_banco, {})

    def oportunidades(self):
        estado = m.carregar_json(self.arq_estado, {})
        return {
            "itens": m.ordenar(self.banco()),
            "meta": {
                "ultima_execucao": estado.get("ultima_execucao"),
                "modo": estado.get("ultimo_modo", ""),
                "requisicoes": estado.get("ultimas_requisicoes", 0),
                "palavras": self.cfg.get("palavras_chave", []),
                "estados": self.cfg.get("estados", []),
                "valor_maximo": self.cfg.get("valor_maximo", 0),
            },
        }

    def marcacoes(self):
        dados = m.carregar_json(self.arq_marc, {})
        return dados if isinstance(dados, dict) else {}

    def marcar(self, id_, status):
        """Devolve False se o id não existe. status None remove a marcação."""
        with self.trava:
            if id_ not in self.banco():
                return False
            marc = self.marcacoes()
            if status is None:
                marc.pop(id_, None)
            else:
                marc[id_] = status
            m.salvar_json(self.arq_marc, marc)
            return True


class Atualizador:
    """Roda a varredura em segundo plano, uma de cada vez."""

    def __init__(self, fn):
        self.fn = fn
        self.atualizando = False
        self.ultimo_erro = None
        self.thread = None
        self.trava = threading.Lock()

    def iniciar(self):
        with self.trava:
            if self.atualizando:
                return False
            self.atualizando = True
            self.ultimo_erro = None
            self.thread = threading.Thread(target=self._rodar, daemon=True)
            self.thread.start()
            return True

    def _rodar(self):
        try:
            self.fn()
        except Exception as e:  # noqa: BLE001 - qualquer falha vai para o status
            self.ultimo_erro = f"{type(e).__name__}: {e}"
        finally:
            self.atualizando = False

    def status(self):
        return {"atualizando": self.atualizando, "ultimo_erro": self.ultimo_erro}


class _Erro(Exception):
    def __init__(self, codigo, msg):
        super().__init__(msg)
        self.codigo, self.msg = codigo, msg


def criar_servidor(armazem, atualizador, porta=PORTA_PADRAO, pasta_front=PASTA):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silencia o log padrão no terminal
            pass

        # ---- respostas
        def _json(self, codigo, obj):
            corpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _html(self, caminho):
            try:
                with open(caminho, "rb") as f:
                    corpo = f.read()
            except OSError:
                raise _Erro(404, "front-end não encontrado")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        # ---- proteções
        def _validar_origem(self, muta):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if ":" in (self.headers.get("Host") or "") \
                else (self.headers.get("Host") or "")
            if host.lower() not in HOSTS_OK:
                raise _Erro(403, "host não permitido")
            origem = self.headers.get("Origin")
            if muta and origem:
                o = urllib.parse.urlsplit(origem)
                if o.scheme != "http" or (o.hostname or "").lower() not in ("localhost", "127.0.0.1", "::1"):
                    raise _Erro(403, "origem não permitida")

        def _corpo_json(self):
            tipo = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if tipo != "application/json":
                raise _Erro(415, "use Content-Type: application/json")
            try:
                tam = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _Erro(400, "Content-Length inválido")
            if tam > LIMITE_CORPO:
                raise _Erro(413, "corpo grande demais")
            try:
                dados = json.loads(self.rfile.read(tam).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                raise _Erro(400, "JSON inválido")
            if not isinstance(dados, dict):
                raise _Erro(400, "o corpo deve ser um objeto JSON")
            return dados

        # ---- roteamento
        def _despachar(self, metodo):
            try:
                self._validar_origem(metodo != "GET")
                caminho = urllib.parse.urlsplit(self.path).path
                if caminho in ("/", "/index.html"):
                    if metodo != "GET":
                        raise _Erro(405, "método não permitido")
                    return self._html(os.path.join(pasta_front, "dashboard_modelo.html"))
                if not caminho.startswith("/api/"):
                    raise _Erro(404, "não encontrado")
                self._api(metodo, caminho)
            except _Erro as e:
                self._json(e.codigo, {"erro": e.msg})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"erro": f"erro interno: {type(e).__name__}"})

        def _api(self, metodo, caminho):
            if caminho == "/api/oportunidades":
                return self._so(metodo, "GET", lambda: self._json(200, armazem.oportunidades()))
            if caminho == "/api/marcacoes":
                return self._so(metodo, "GET", lambda: self._json(200, armazem.marcacoes()))
            if caminho == "/api/status":
                return self._so(metodo, "GET", lambda: self._json(200, atualizador.status()))
            if caminho == "/api/atualizar":
                return self._so(metodo, "POST", self._atualizar)
            if caminho.startswith("/api/marcacoes/"):
                id_ = urllib.parse.unquote(caminho[len("/api/marcacoes/"):])
                return self._so(metodo, "PUT", lambda: self._marcar(id_))
            raise _Erro(404, "não encontrado")

        def _so(self, metodo, esperado, fn):
            if metodo != esperado:
                raise _Erro(405, "método não permitido")
            return fn()

        def _atualizar(self):
            self._corpo_json()
            if not atualizador.iniciar():
                raise _Erro(409, "já existe uma atualização em andamento")
            self._json(202, {"ok": True})

        def _marcar(self, id_):
            dados = self._corpo_json()
            status = dados.get("status")
            if status is not None and status not in STATUS_VALIDOS:
                raise _Erro(400, "status deve ser 'interessante', 'descartada' ou null")
            if not armazem.marcar(id_, status):
                raise _Erro(404, "oportunidade não encontrada")
            self._json(200, {"ok": True})

        def do_GET(self): self._despachar("GET")
        def do_POST(self): self._despachar("POST")
        def do_PUT(self): self._despachar("PUT")
        def do_DELETE(self): self._despachar("DELETE")

    return ThreadingHTTPServer(("127.0.0.1", int(porta)), Handler)


def validar_porta(valor):
    try:
        porta = int(valor)
    except (TypeError, ValueError):
        raise ValueError(f"porta inválida: {valor!r}")
    if not 1 <= porta <= 65535:
        raise ValueError(f"porta fora do intervalo: {porta}")
    return porta


def fn_atualizar(cfg):
    """Função usada pelo botão 'Atualizar': varredura + CSV + aviso no Telegram."""
    def _rodar():
        banco, novas, _estado = m.executar(cfg)
        m.gerar_csv(m.ordenar(banco))
        m.avisar_novas(cfg, novas)
    return _rodar


def servir(cfg, porta=PORTA_PADRAO, abrir=True):
    """Sobe o servidor (bloqueia até Ctrl+C)."""
    porta = validar_porta(porta)
    srv = criar_servidor(Armazem(PASTA, cfg), Atualizador(fn_atualizar(cfg)), porta=porta)
    url = url_local(porta)
    m.log(f"Dashboard em {url}  (Ctrl+C para parar)")
    if abrir:
        abrir_navegador(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        m.log("Servidor encerrado.")
    finally:
        srv.server_close()
