# -*- coding: utf-8 -*-
"""Testes do monitor, com a API do PNCP simulada (não acessa a internet).
Rode com:  python -m unittest test_monitor -v
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import monitor_licitacoes as m

FUTURO = (datetime.now() + timedelta(days=5)).isoformat(timespec="seconds")
PASSADO = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")

CFG = {
    "palavras_chave": ["limpeza", "Manutenção predial"],
    "palavras_excluir": ["medicamento"],
    "estados": ["ES"], "cidades": [], "modalidades": [8],
    "valor_minimo": 0, "valor_maximo": 300000,
    "pausa_entre_requisicoes": 0, "varredura_completa_a_cada_dias": 7,
}


def item(id_, objeto, valor=1000, enc=FUTURO, cidade="Vitória"):
    return {"numeroControlePNCP": id_, "objetoCompra": objeto, "valorTotalEstimado": valor,
            "dataEncerramentoProposta": enc, "modalidadeNome": "Dispensa",
            "orgaoEntidade": {"cnpj": "123", "razaoSocial": "PREFEITURA"},
            "unidadeOrgao": {"ufSigla": "ES", "municipioNome": cidade},
            "anoCompra": 2026, "sequencialCompra": 7}


class TestFiltros(unittest.TestCase):
    def test_acento_e_maiusculas(self):
        self.assertEqual(m.combina(item("1", "Serviço de MANUTENCAO PREDIAL"), CFG), ["manutencao predial"])

    def test_sem_palavra(self):
        self.assertIsNone(m.combina(item("1", "Aquisição de pneus"), CFG))

    def test_exclusao(self):
        self.assertIsNone(m.combina(item("1", "limpeza e medicamento"), CFG))

    def test_valor_maximo(self):
        self.assertIsNone(m.combina(item("1", "limpeza", valor=300001), CFG))
        self.assertIsNotNone(m.combina(item("1", "limpeza", valor=300000), CFG))

    def test_valor_nao_informado_passa(self):
        self.assertIsNotNone(m.combina(item("1", "limpeza", valor=0), CFG))

    def test_cidade(self):
        cfg = dict(CFG, cidades=["Vitoria"])
        self.assertIsNotNone(m.combina(item("1", "limpeza", cidade="Vitória"), cfg))
        self.assertIsNone(m.combina(item("1", "limpeza", cidade="Serra"), cfg))

    def test_resumir_link(self):
        r = m.resumir(item("1", "limpeza"), ["limpeza"])
        self.assertEqual(r["link_pncp"], "https://pncp.gov.br/app/editais/123/2026/7")

    def test_dias_restantes(self):
        self.assertGreater(m.dias_restantes(FUTURO), 4)
        self.assertLess(m.dias_restantes(PASSADO), 0)
        self.assertEqual(m.dias_restantes(None), 999)

    def test_brl(self):
        self.assertEqual(m.brl(1234.5), "R$ 1.234,50")


class TestExecucao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for nome in ("ARQ_BANCO", "ARQ_ESTADO", "ARQ_HTML", "ARQ_CSV", "ARQ_LOG"):
            setattr(self, "_" + nome, getattr(m, nome))
            setattr(m, nome, os.path.join(self.tmp, os.path.basename(getattr(m, nome))))
        self.urls = []

    def tearDown(self):
        for nome in ("ARQ_BANCO", "ARQ_ESTADO", "ARQ_HTML", "ARQ_CSV", "ARQ_LOG"):
            setattr(m, nome, getattr(self, "_" + nome))
        shutil.rmtree(self.tmp)

    def fake_get(self, dados_por_pagina):
        def get(_self, url):
            self.urls.append(url)
            pag = int(url.split("pagina=")[1].split("&")[0])
            return dados_por_pagina.get(pag)
        return get

    def test_completa_depois_incremental(self):
        pagina1 = {"totalRegistros": 3, "totalPaginas": 1, "data": [
            item("A", "limpeza"), item("B", "pneus"), item("C", "limpeza", enc=PASSADO)]}
        with mock.patch.object(m.ClientePNCP, "get", self.fake_get({1: pagina1})):
            banco, novas, estado = m.executar(CFG)
        self.assertEqual(set(banco), {"A"})          # B sem palavra, C já encerrou
        self.assertEqual(len(novas), 1)
        self.assertIn("/proposta", self.urls[0])
        self.assertEqual(estado["ultimo_modo"], "completa")

        # 2ª execução: incremental, só o que foi publicado; A não é mais "nova"
        self.urls.clear()
        pagina1 = {"totalRegistros": 1, "totalPaginas": 1, "data": [item("D", "manutencao predial")]}
        with mock.patch.object(m.ClientePNCP, "get", self.fake_get({1: pagina1})):
            banco, novas, estado = m.executar(CFG)
        self.assertIn("/publicacao", self.urls[0])
        self.assertEqual(set(banco), {"A", "D"})
        self.assertEqual([o["id"] for o in novas], ["D"])
        self.assertFalse(banco["A"]["novo"])

    def test_paginacao(self):
        paginas = {1: {"totalPaginas": 2, "data": [item("A", "limpeza")]},
                   2: {"totalPaginas": 2, "data": [item("B", "limpeza")]}}
        with mock.patch.object(m.ClientePNCP, "get", self.fake_get(paginas)):
            banco, _, _ = m.executar(CFG)
        self.assertEqual(set(banco), {"A", "B"})

    def test_dashboard_gerado(self):
        with mock.patch.object(m.ClientePNCP, "get", self.fake_get({1: {"totalPaginas": 1, "data": [item("A", "limpeza </script>")]}})):
            banco, _, estado = m.executar(CFG)
        m.gerar_dashboard(m.ordenar(banco), CFG, estado)
        with open(m.ARQ_HTML, encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("/*__DADOS__*/null", html)
        self.assertNotIn("limpeza </script>", html)  # escapado, não quebra a página
        dados = html.split("const D = ")[1].split(" || {itens")[0]
        self.assertEqual(json.loads(dados)["itens"][0]["id"], "A")


if __name__ == "__main__":
    unittest.main()
