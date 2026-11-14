# -*- coding: utf-8 -*-
"""Testes do servidor local do dashboard (sem internet, sem tocar nos dados reais).
Rode com:  python -m unittest test_servidor -v
"""
import unittest
from unittest import mock

import servidor as s


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


if __name__ == "__main__":
    unittest.main()
