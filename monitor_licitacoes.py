# -*- coding: utf-8 -*-
"""
Monitor de Licitações v2 - PNCP (Portal Nacional de Contratações Públicas)
---------------------------------------------------------------------------
Busca licitações com propostas ABERTAS no PNCP, filtra pelas suas
palavras-chave / estados / valores e gera:
  - dashboard.html      -> painel com filtros, gráficos e prazos
  - oportunidades.csv   -> planilha (abre no Excel)
  - aviso no Telegram   -> quando aparecer oportunidade nova (opcional)

Como economiza requisições:
  * 1ª execução (ou 1x a cada N dias): VARREDURA COMPLETA de tudo que está aberto.
  * Demais execuções: modo INCREMENTAL, que só busca o que foi PUBLICADO desde
    a última execução. Normalmente são poucas páginas, em vez de centenas.
  * Respeita o limite do PNCP (erro 429) e salva o progresso a cada etapa,
    então uma execução interrompida não perde o que já foi buscado.

Comandos:
  python monitor_licitacoes.py                  roda (completa ou incremental, automático)
  python monitor_licitacoes.py --abrir          roda e abre o dashboard
  python monitor_licitacoes.py --completa       força varredura completa
  python monitor_licitacoes.py --so-dashboard   só refaz o dashboard com o que já tem
  python monitor_licitacoes.py --descobrir-chat-id   configura o Telegram
  python monitor_licitacoes.py --testar-telegram     manda mensagem de teste
"""
import csv
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_CONFIG = os.path.join(PASTA, "config.json")
ARQ_BANCO = os.path.join(PASTA, "banco_oportunidades.json")
ARQ_ESTADO = os.path.join(PASTA, "estado_execucao.json")
ARQ_HTML = os.path.join(PASTA, "dashboard.html")
ARQ_CSV = os.path.join(PASTA, "oportunidades.csv")
ARQ_LOG = os.path.join(PASTA, "monitor.log")

BASE = "https://pncp.gov.br/api/consulta/v1/contratacoes"
API_ABERTAS = BASE + "/proposta"      # tudo com proposta aberta
API_PUBLICADAS = BASE + "/publicacao"  # publicadas num intervalo de datas

MODALIDADES = {
    1: "Leilão eletrônico", 2: "Diálogo competitivo", 3: "Concurso",
    4: "Concorrência eletrônica", 5: "Concorrência presencial",
    6: "Pregão eletrônico", 7: "Pregão presencial", 8: "Dispensa de licitação",
    9: "Inexigibilidade", 10: "Manifestação de interesse",
    11: "Pré-qualificação", 12: "Credenciamento", 13: "Leilão presencial",
}


# ----------------------------------------------------------------- utilidades
def log(msg):
    linha = f"[{datetime.now():%d/%m/%Y %H:%M:%S}] {msg}"
    print(linha, flush=True)
    try:
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def normalizar(txt):
    """minúsculas e sem acento, pra 'Manutenção' casar com 'manutencao'."""
    txt = unicodedata.normalize("NFKD", str(txt or ""))
    return "".join(c for c in txt if not unicodedata.combining(c)).lower()


def carregar_json(caminho, padrao):
    if os.path.exists(caminho):
        try:
            with open(caminho, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            log(f"Aviso: não consegui ler {os.path.basename(caminho)}, começando do zero.")
    return padrao


def salvar_json(caminho, dados):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(tmp, caminho)


def parse_data(iso):
    try:
        return datetime.fromisoformat(str(iso)[:19])
    except (ValueError, TypeError):
        return None


def dias_restantes(iso):
    d = parse_data(iso)
    if not d:
        return 999
    return (d - datetime.now()).total_seconds() / 86400


# --------------------------------------------------------------------- HTTP
class ClientePNCP:
    """Faz as chamadas com pausa entre elas e trata o limite (429) do PNCP."""

    def __init__(self, cfg):
        self.pausa = float(cfg.get("pausa_entre_requisicoes", 1.0))
        self.tentativas = int(cfg.get("tentativas", 6))
        self.requisicoes = 0
        self._ultima = 0.0

    def get(self, url):
        for i in range(self.tentativas):
            espera = self.pausa - (time.time() - self._ultima)
            if espera > 0:
                time.sleep(espera)
            self._ultima = time.time()
            self.requisicoes += 1
            try:
                req = urllib.request.Request(url, headers={
                    "Accept": "application/json",
                    "User-Agent": "monitor-licitacoes/2.0",
                })
                with urllib.request.urlopen(req, timeout=60) as r:
                    if r.status == 204:
                        return None
                    corpo = r.read()
                    return json.loads(corpo) if corpo else None
            except urllib.error.HTTPError as e:
                if e.code == 204:
                    return None
                if e.code in (400, 404, 422):
                    log(f"  API respondeu {e.code} (sem dados para esse filtro)")
                    return None
                if e.code == 429:
                    retry = e.headers.get("Retry-After") if e.headers else None
                    try:
                        espera = max(int(retry), 10)
                    except (TypeError, ValueError):
                        espera = 20 * (i + 1)
                    # depois de um 429, fica mais devagar no resto da execução
                    self.pausa = min(self.pausa * 1.5 + 0.5, 6)
                    log(f"  PNCP pediu calma (429). Esperando {espera}s; "
                        f"pausa entre chamadas agora {self.pausa:.1f}s")
                    time.sleep(espera)
                    continue
                espera = 5 * (i + 1)
                log(f"  Erro HTTP {e.code}, tentando de novo em {espera}s...")
                time.sleep(espera)
            except Exception as e:  # rede instável, timeout etc.
                espera = 5 * (i + 1)
                log(f"  Falha de conexão ({e}), tentando de novo em {espera}s...")
                time.sleep(espera)
        log(f"  Desisti desta página depois de {self.tentativas} tentativas.")
        return None


# ------------------------------------------------------------------- busca
def paginar(cliente, url_base, params, rotulo, max_paginas):
    """Percorre todas as páginas de uma consulta e devolve a lista de itens."""
    itens, pagina = [], 1
    while True:
        p = dict(params, pagina=pagina, tamanhoPagina=50)
        dados = cliente.get(url_base + "?" + urllib.parse.urlencode(p))
        if not dados or not dados.get("data"):
            if pagina == 1:
                log(f"  {rotulo}: nada encontrado")
            break
        itens.extend(dados["data"])
        total_pag = dados.get("totalPaginas") or 1
        if pagina == 1:
            log(f"  {rotulo}: {dados.get('totalRegistros', '?')} registros ({total_pag} páginas)")
        if pagina >= total_pag or pagina >= max_paginas:
            break
        pagina += 1
    return itens


def consultas(cfg, modo, desde):
    """Gera (rótulo, url, parâmetros) de cada consulta a fazer."""
    hoje = datetime.now()
    ufs = [u.upper() for u in cfg.get("estados", [])] or [None]
    for mod in cfg.get("modalidades", [8, 6]):
        for uf in ufs:
            rotulo = f"{MODALIDADES.get(mod, mod)} / {uf or 'Brasil'}"
            if modo == "completa":
                params = {"dataFinal": (hoje + timedelta(days=int(cfg.get("dias_a_frente", 60)))).strftime("%Y%m%d"),
                          "codigoModalidadeContratacao": mod}
                url = API_ABERTAS
            else:
                params = {"dataInicial": desde.strftime("%Y%m%d"),
                          "dataFinal": hoje.strftime("%Y%m%d"),
                          "codigoModalidadeContratacao": mod}
                url = API_PUBLICADAS
            if uf:
                params["uf"] = uf
            yield rotulo, url, params


def combina(item, cfg):
    """Devolve a lista de palavras encontradas, ou None se o item não interessa."""
    texto = normalizar(" ".join([
        item.get("objetoCompra", "") or "",
        item.get("informacaoComplementar", "") or "",
    ]))
    incluir = [normalizar(p) for p in cfg.get("palavras_chave", []) if p.strip()]
    excluir = [normalizar(p) for p in cfg.get("palavras_excluir", []) if p.strip()]

    achou = [p for p in incluir if p in texto]
    if incluir and not achou:
        return None
    if any(p in texto for p in excluir):
        return None

    valor = item.get("valorTotalEstimado") or 0
    vmin = cfg.get("valor_minimo") or 0
    vmax = cfg.get("valor_maximo") or 0
    # valor 0 = sigiloso/não informado: deixa passar
    if valor and vmin and valor < vmin:
        return None
    if valor and vmax and valor > vmax:
        return None

    cidades = [normalizar(c) for c in cfg.get("cidades", []) if c.strip()]
    if cidades:
        mun = normalizar((item.get("unidadeOrgao") or {}).get("municipioNome", ""))
        if mun not in cidades:
            return None
    return achou


def resumir(item, palavras):
    org = item.get("orgaoEntidade") or {}
    un = item.get("unidadeOrgao") or {}
    cnpj = org.get("cnpj", "")
    ano = item.get("anoCompra", "")
    seq = item.get("sequencialCompra", "")
    return {
        "id": item.get("numeroControlePNCP"),
        "objeto": (item.get("objetoCompra") or "").strip(),
        "orgao": org.get("razaoSocial", ""),
        "cidade": un.get("municipioNome", ""),
        "uf": un.get("ufSigla", ""),
        "modalidade": item.get("modalidadeNome") or MODALIDADES.get(item.get("modalidadeId"), ""),
        "valor": item.get("valorTotalEstimado") or 0,
        "abertura": item.get("dataAberturaProposta") or "",
        "encerramento": item.get("dataEncerramentoProposta") or "",
        "publicacao": item.get("dataPublicacaoPncp") or "",
        "link_pncp": f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}" if cnpj else "",
        "link_sistema": item.get("linkSistemaOrigem") or "",
        "palavras": ", ".join(palavras),
    }


def escolher_modo(cfg, estado, forcar_completa):
    if forcar_completa or not estado.get("ultima_completa"):
        return "completa", None
    ultima_completa = parse_data(estado["ultima_completa"])
    dias = float(cfg.get("varredura_completa_a_cada_dias", 7))
    if not ultima_completa or datetime.now() - ultima_completa > timedelta(days=dias):
        return "completa", None
    ultima = parse_data(estado.get("ultima_execucao")) or ultima_completa
    # volta 1 dia de margem, porque órgãos às vezes publicam com atraso
    return "incremental", ultima - timedelta(days=1)


def executar(cfg, forcar_completa=False):
    banco = carregar_json(ARQ_BANCO, {})
    estado = carregar_json(ARQ_ESTADO, {})
    modo, desde = escolher_modo(cfg, estado, forcar_completa)
    inicio = datetime.now()
    log(f"Iniciando varredura {modo.upper()}"
        + (f" (publicadas desde {desde:%d/%m/%Y})" if desde else ""))

    cliente = ClientePNCP(cfg)
    ja_conhecidos = set(banco)
    max_pag = int(cfg.get("max_paginas_por_busca", 200))
    recebidos = 0

    for rotulo, url, params in consultas(cfg, modo, desde):
        for item in paginar(cliente, url, params, rotulo, max_pag):
            recebidos += 1
            if dias_restantes(item.get("dataEncerramentoProposta")) <= 0:
                continue  # já encerrou (ou não recebe proposta)
            palavras = combina(item, cfg)
            if palavras is None:
                continue
            o = resumir(item, palavras)
            if not o["id"]:
                continue
            antigo = banco.get(o["id"], {})
            o["primeira_vez"] = antigo.get("primeira_vez") or datetime.now().isoformat(timespec="seconds")
            o["novo"] = o["id"] not in ja_conhecidos
            banco[o["id"]] = o
        salvar_json(ARQ_BANCO, banco)  # checkpoint: não perde nada se for interrompido

    # tira as que já encerraram
    banco = {k: o for k, o in banco.items() if dias_restantes(o.get("encerramento")) > 0}
    novas = [o for k, o in banco.items() if k not in ja_conhecidos]
    for k, o in banco.items():
        o["novo"] = k not in ja_conhecidos
    salvar_json(ARQ_BANCO, banco)

    estado["ultima_execucao"] = inicio.isoformat(timespec="seconds")
    if modo == "completa":
        estado["ultima_completa"] = inicio.isoformat(timespec="seconds")
    estado["ultimo_modo"] = modo
    estado["ultimas_requisicoes"] = cliente.requisicoes
    estado["ultimos_recebidos"] = recebidos
    salvar_json(ARQ_ESTADO, estado)

    log(f"Requisições feitas: {cliente.requisicoes} | registros analisados: {recebidos}")
    return banco, novas, estado


# ------------------------------------------------------------------ saídas
def brl(v):
    if not v:
        return "Não informado"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def data_br(iso):
    d = parse_data(iso)
    return d.strftime("%d/%m/%Y %H:%M") if d else "-"


def ordenar(banco):
    return sorted(banco.values(), key=lambda o: (not o.get("novo"), o.get("encerramento") or "9"))


def gerar_csv(lista):
    with open(ARQ_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Novo?", "Encerra em", "Objeto", "Órgão", "Cidade", "UF",
                    "Modalidade", "Valor estimado", "Palavras", "Link PNCP", "Link sistema"])
        for o in lista:
            w.writerow(["SIM" if o.get("novo") else "", data_br(o["encerramento"]), o["objeto"],
                        o["orgao"], o["cidade"], o["uf"], o["modalidade"], brl(o["valor"]),
                        o["palavras"], o["link_pncp"], o["link_sistema"]])


def gerar_dashboard(lista, cfg, estado):
    with open(os.path.join(PASTA, "dashboard_modelo.html"), encoding="utf-8") as f:
        modelo = f.read()
    dados = {
        "itens": lista,
        "atualizado": datetime.now().strftime("%d/%m/%Y às %H:%M"),
        "modo": estado.get("ultimo_modo", ""),
        "requisicoes": estado.get("ultimas_requisicoes", 0),
        "palavras": cfg.get("palavras_chave", []),
        "estados": cfg.get("estados", []),
        "valor_maximo": cfg.get("valor_maximo", 0),
    }
    js = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    with open(ARQ_HTML, "w", encoding="utf-8") as f:
        f.write(modelo.replace("/*__DADOS__*/null", js))


# ------------------------------------------------------------------ telegram
def telegram_api(token, metodo, params=None):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    dados = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(url, dados, timeout=30) as r:
        return json.loads(r.read())


def enviar_telegram(cfg, texto):
    tg = cfg.get("telegram") or {}
    token, chat = tg.get("bot_token", "").strip(), str(tg.get("chat_id", "")).strip()
    if not token or not chat:
        return False
    telegram_api(token, "sendMessage", {"chat_id": chat, "text": texto,
                                        "disable_web_page_preview": "true"})
    return True


def avisar_novas(cfg, novas):
    if not novas:
        return
    tg = cfg.get("telegram") or {}
    if not tg.get("bot_token") or not tg.get("chat_id"):
        return
    msgs, bloco = [], f"🔔 {len(novas)} licitação(ões) nova(s) no seu filtro:\n"
    for o in sorted(novas, key=lambda x: x["encerramento"])[:30]:
        item = (f"\n• {o['objeto'][:180]}\n  {o['orgao']} — {o['cidade']}/{o['uf']}\n"
                f"  {o['modalidade']} | {brl(o['valor'])} | encerra {data_br(o['encerramento'])}\n"
                f"  {o['link_pncp']}\n")
        if len(bloco) + len(item) > 3800:
            msgs.append(bloco)
            bloco = ""
        bloco += item
    if len(novas) > 30:
        bloco += f"\n...e mais {len(novas) - 30}. Veja o dashboard."
    msgs.append(bloco)
    try:
        for m in msgs:
            enviar_telegram(cfg, m)
        log("Aviso enviado no Telegram.")
    except Exception as e:
        log(f"Não consegui enviar ao Telegram: {e}")


def descobrir_chat_id(cfg):
    token = (cfg.get("telegram") or {}).get("bot_token", "").strip()
    if not token:
        print("Primeiro cole o bot_token no config.json (veja o LEIA-ME).")
        return
    try:
        resp = telegram_api(token, "getUpdates")
    except Exception as e:
        print(f"Erro falando com o Telegram: {e}. Confira o token.")
        return
    chats = {}
    for u in resp.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("Nenhuma mensagem encontrada. Mande um 'oi' para o seu bot no Telegram e rode de novo.")
        return
    chat_id = list(chats)[-1]
    cfg.setdefault("telegram", {})["chat_id"] = str(chat_id)
    salvar_json(ARQ_CONFIG, cfg)
    print(f"Pronto! chat_id {chat_id} ({chats[chat_id]}) salvo no config.json.")
    try:
        enviar_telegram(cfg, "✅ Monitor de Licitações conectado! Você vai receber as oportunidades novas aqui.")
        print("Mandei uma mensagem de teste, confira o Telegram.")
    except Exception as e:
        print(f"Salvei o chat_id, mas o envio de teste falhou: {e}")


# -------------------------------------------------------------------- main
def main():
    if not os.path.exists(ARQ_CONFIG):
        log("Não achei o config.json na mesma pasta do script.")
        sys.exit(1)
    cfg = carregar_json(ARQ_CONFIG, {})
    args = set(sys.argv[1:])

    if "--descobrir-chat-id" in args:
        descobrir_chat_id(cfg)
        return
    if "--testar-telegram" in args:
        try:
            ok = enviar_telegram(cfg, "🧪 Teste do Monitor de Licitações: está funcionando!")
            print("Mensagem enviada!" if ok else "Telegram não configurado no config.json.")
        except Exception as e:
            print(f"Falhou: {e}")
        return

    if "--so-dashboard" in args:
        banco = carregar_json(ARQ_BANCO, {})
        estado = carregar_json(ARQ_ESTADO, {})
        novas = []
    else:
        banco, novas, estado = executar(cfg, forcar_completa="--completa" in args)

    lista = ordenar(banco)
    gerar_csv(lista)
    gerar_dashboard(lista, cfg, estado)
    avisar_novas(cfg, novas)
    log(f"Pronto: {len(lista)} oportunidades abertas no filtro, {len(novas)} novas. Abra o dashboard.html")

    if "--abrir" in args:
        webbrowser.open("file://" + ARQ_HTML)


if __name__ == "__main__":
    main()
