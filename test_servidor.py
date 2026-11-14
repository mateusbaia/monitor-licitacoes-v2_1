# -*- coding: utf-8 -*-
"""Testes do servidor local do dashboard (sem internet, sem tocar nos dados reais).
Rode com:  python -m unittest test_servidor -v
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import servidor as s


def limpar_tmp(pasta):
    """Única remoção do teste: só apaga o diretório temporário criado por ele."""
    if not pasta or not os.path.isabs(pasta) or pasta in ("/", "."):
        raise RuntimeError(f"recusei apagar {pasta!r}")
    base = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(pasta)
    if not real.startswith(base + os.sep) or not os.path.basename(real).startswith("dash_test_"):
        raise RuntimeError(f"recusei apagar fora do tmp: {pasta!r}")
    for nome in os.listdir(real):
        os.remove(os.path.join(real, nome))
    os.rmdir(real)


class TestAbrirNavegador(unittest.TestCase):
    """Regressão: no WSL, file:///home/... não abria em nenhum navegador."""

    def test_url_e_http_local_nunca_file(self):
        url = s.url_local(8000)
        self.assertEqual(url, "http://localhost:8000/")
        self.assertFalse(url.startswith("file:"))

    def test_wsl_usa_navegador_do_windows(self):
        cmd = s.comando_abrir("http://localhost:8000/", em_wsl=True)
        self.assertEqual(cmd[0], "cmd.exe")
        self.assertIn("http://localhost:8000/", cmd)

    def test_fora_do_wsl_nao_tem_comando_especial(self):
        self.assertIsNone(s.comando_abrir("http://localhost:8000/", em_wsl=False))

    def test_rejeita_url_nao_http(self):
        for ruim in ("file:///home/x/dashboard.html", "javascript:alert(1)", "", None):
            with self.assertRaises(ValueError):
                s.comando_abrir(ruim, em_wsl=True)

    def test_detecta_wsl_pelo_proc_version(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="Linux ... microsoft-standard-WSL2")):
            self.assertTrue(s.detectar_wsl())
        with mock.patch("builtins.open", mock.mock_open(read_data="Linux version 6.1 generic")):
            self.assertFalse(s.detectar_wsl())
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertFalse(s.detectar_wsl())

    def test_abrir_usa_subprocess_no_wsl(self):
        with mock.patch.object(s, "detectar_wsl", return_value=True), \
             mock.patch("subprocess.Popen") as popen:
            self.assertTrue(s.abrir_navegador("http://localhost:8000/"))
        popen.assert_called_once()

    def test_abrir_falha_sem_quebrar(self):
        with mock.patch.object(s, "detectar_wsl", return_value=True), \
             mock.patch("subprocess.Popen", side_effect=OSError):
            self.assertFalse(s.abrir_navegador("http://localhost:8000/"))


BANCO = {
    "a1": {"id": "a1", "objeto": "Limpeza", "encerramento": "2099-01-02T10:00:00", "novo": False},
    "b2": {"id": "b2", "objeto": "Eventos", "encerramento": "2099-01-01T10:00:00", "novo": True},
}


class BaseApi(unittest.TestCase):
    def setUp(self):
        self.dir = None
        self.srv = None
        self.dir = tempfile.mkdtemp(prefix="dash_test_")
        with open(os.path.join(self.dir, "banco_oportunidades.json"), "w", encoding="utf-8") as f:
            json.dump(BANCO, f)
        with open(os.path.join(self.dir, "dashboard_modelo.html"), "w", encoding="utf-8") as f:
            f.write("<html>front</html>")
        self.chamadas = []
        arm = s.Armazem(self.dir, cfg={"palavras_chave": ["limpeza"], "estados": ["ES"], "valor_maximo": 300000})
        self.atual = s.Atualizador(lambda: self.chamadas.append(1))
        self.srv = s.criar_servidor(arm, self.atual, porta=0, pasta_front=self.dir)
        self.porta = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        if self.dir:
            limpar_tmp(self.dir)

    def req(self, metodo, caminho, corpo=None, headers=None, bruto=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        dados = bruto if bruto is not None else (json.dumps(corpo).encode() if corpo is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.porta}{caminho}", data=dados, method=metodo, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")


class TestApi(BaseApi):
    def test_front_servido_na_raiz(self):
        st, corpo = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("front", corpo)

    def test_oportunidades_ordenadas_novas_primeiro(self):
        st, corpo = self.req("GET", "/api/oportunidades")
        self.assertEqual(st, 200)
        d = json.loads(corpo)
        self.assertEqual([o["id"] for o in d["itens"]], ["b2", "a1"])
        self.assertEqual(d["meta"]["estados"], ["ES"])

    def test_marcar_e_listar(self):
        st, _ = self.req("PUT", "/api/marcacoes/a1", {"status": "interessante"})
        self.assertEqual(st, 200)
        _, corpo = self.req("GET", "/api/marcacoes")
        self.assertEqual(json.loads(corpo), {"a1": "interessante"})

    def test_marcacao_persiste_em_disco(self):
        self.req("PUT", "/api/marcacoes/b2", {"status": "descartada"})
        with open(os.path.join(self.dir, "marcacoes.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"b2": "descartada"})

    def test_desmarcar_com_null(self):
        self.req("PUT", "/api/marcacoes/a1", {"status": "interessante"})
        st, _ = self.req("PUT", "/api/marcacoes/a1", {"status": None})
        self.assertEqual(st, 200)
        _, corpo = self.req("GET", "/api/marcacoes")
        self.assertEqual(json.loads(corpo), {})

    def test_atualizar_dispara_e_status(self):
        st, _ = self.req("POST", "/api/atualizar", {})
        self.assertEqual(st, 202)
        self.atual.thread.join(5)
        self.assertEqual(self.chamadas, [1])
        _, corpo = self.req("GET", "/api/status")
        self.assertFalse(json.loads(corpo)["atualizando"])


class TestApiNegativos(BaseApi):
    def test_id_inexistente(self):
        st, _ = self.req("PUT", "/api/marcacoes/nao-existe", {"status": "interessante"})
        self.assertEqual(st, 404)

    def test_status_invalido(self):
        st, _ = self.req("PUT", "/api/marcacoes/a1", {"status": "hackeado"})
        self.assertEqual(st, 400)

    def test_json_malformado(self):
        st, _ = self.req("PUT", "/api/marcacoes/a1", bruto=b"{nao e json")
        self.assertEqual(st, 400)

    def test_corpo_nao_objeto(self):
        st, _ = self.req("PUT", "/api/marcacoes/a1", ["x"])
        self.assertEqual(st, 400)

    def test_corpo_grande_demais(self):
        st, _ = self.req("PUT", "/api/marcacoes/a1", bruto=b"{" + b" " * 70000 + b"}")
        self.assertEqual(st, 413)

    def test_exige_content_type_json(self):
        st, _ = self.req("PUT", "/api/marcacoes/a1", bruto=b'{"status": null}', headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_origem_externa_bloqueada(self):
        st, _ = self.req("POST", "/api/atualizar", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertEqual(self.chamadas, [])

    def test_host_estranho_bloqueado(self):
        st, _ = self.req("GET", "/api/oportunidades", headers={"Host": "evil.example"})
        self.assertEqual(st, 403)

    def test_rota_inexistente_e_metodo_errado(self):
        self.assertEqual(self.req("GET", "/api/nada")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/oportunidades")[0], 405)
        self.assertEqual(self.req("GET", "/../../etc/passwd")[0], 404)

    def test_atualizar_ja_em_andamento(self):
        self.atual.atualizando = True
        st, _ = self.req("POST", "/api/atualizar", {})
        self.assertEqual(st, 409)

    def test_erro_na_atualizacao_fica_no_status(self):
        def falha():
            raise RuntimeError("PNCP fora do ar")
        self.atual.fn = falha
        self.req("POST", "/api/atualizar", {})
        self.atual.thread.join(5)
        _, corpo = self.req("GET", "/api/status")
        d = json.loads(corpo)
        self.assertFalse(d["atualizando"])
        self.assertIn("PNCP fora do ar", d["ultimo_erro"])

    def test_limpeza_recusa_caminho_perigoso(self):
        for ruim in ("", "/", ".", "/home"):
            with self.assertRaises(RuntimeError):
                limpar_tmp(ruim)


class TestAtualizacaoReal(unittest.TestCase):
    def test_fn_atualizar_roda_varredura_csv_e_aviso(self):
        cfg = {"x": 1}
        banco = {"a": {"id": "a", "novo": True}}
        with mock.patch.object(s.m, "executar", return_value=(banco, [banco["a"]], {})) as ex, \
             mock.patch.object(s.m, "gerar_csv") as csv, \
             mock.patch.object(s.m, "avisar_novas") as av:
            s.fn_atualizar(cfg)()
        ex.assert_called_once_with(cfg)
        csv.assert_called_once()
        av.assert_called_once_with(cfg, [banco["a"]])

    def test_porta_invalida_rejeitada(self):
        for ruim in ("abc", 0, 70000, -1, None):
            with self.assertRaises(ValueError):
                s.validar_porta(ruim)
        self.assertEqual(s.validar_porta("8123"), 8123)


if __name__ == "__main__":
    unittest.main()
