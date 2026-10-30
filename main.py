import argparse
import os
import re
import sys
import hashlib
import html
import json
import time
import requests
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import gupy
from http_proxy import scraper_proxies, warp_is_on
import inhire_discovery
import job_details  # module import, so tests can patch main.job_details.fetch_details
import linkedin_login
import linkedin_posts
import programathor
import remotar
import web_search
from store import Source, Store, StoreError, StoreFailure, from_env

try:
    from bs4 import BeautifulSoup
    BS4_DISPONIVEL = True
except ImportError:
    BS4_DISPONIVEL = False
    print("⚠️  beautifulsoup4 não instalado — LinkedIn desativado. Rode: pip install beautifulsoup4")

AVISO_SEM_BS4 = "LinkedIn desligado: falta instalar o beautifulsoup4 (pip install beautifulsoup4)."

# --- 1. CONFIG ---
DIRETORIO_ATUAL = os.path.dirname(os.path.abspath(__file__))

def carregar_env():
    caminho = os.path.join(DIRETORIO_ATUAL, '.env')
    if not os.path.exists(caminho):
        return
    with open(caminho) as f:
        for linha in f:
            linha = linha.strip()
            if linha and not linha.startswith('#') and '=' in linha:
                chave, valor = linha.split('=', 1)
                os.environ.setdefault(chave.strip(), valor.strip())

carregar_env()

TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID_GRUPO")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY")
# li_at cookie of a logged-in LinkedIn session: posts come from LinkedIn's own search
LINKEDIN_LI_AT = os.getenv("LINKEDIN_LI_AT")
# Set by the workflow to WARP's local SOCKS proxy: Cloudflare blocks the runners' datacenter IPs
SCRAPER_PROXY = os.getenv("SCRAPER_PROXY")
# "true" on the VPS: without the proxy no source runs, so no job site sees the VPS's own IP
ENV_REQUIRE_SCRAPER_PROXY = "REQUIRE_SCRAPER_PROXY"

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

# ════════════════════════════════════════════════════════════════════════════════
# 2. CONFIGURAÇÕES DO USUÁRIO
# Tudo que você precisa alterar para adaptar o bot ao seu perfil está aqui.
# ════════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────────────
# A. BUSCAS — o que procurar em cada plataforma
# ──────────────────────────────────────────────────────────────────────────────
# "nome" é o label exibido no alerta do Telegram.

# Gupy: busca por cargo (jobName) e modalidade (workplaceType).
# workplaceType válidos: 'remote' | 'hybrid' | 'on-site'
FILTROS_GUPY = [
    {"nome": "FLUTTER · REMOTO", "params": {'workplaceType': 'remote', 'jobName': 'flutter', 'limit': 15}},
    # {"nome": "MOBILE · REMOTO",  "params": {'workplaceType': 'remote', 'jobName': 'mobile',  'limit': 10}},
]

# ProgramaThor: busca por termo + slug de localidade da URL.
# local_filtro é o slug exato usado na URL do ProgramaThor:
#   'remoto'     → /jobs-flutter/remoto
#   'sao-paulo'  → /jobs-flutter/sao-paulo
#   ''           → /jobs-flutter  (sem filtro de local)
FILTROS_PROGRAMATHOR = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter", "local_filtro": "remoto"},
    # {"nome": "MOBILE · REMOTO",  "termo": "mobile",  "local_filtro": "remoto"},
]

# LinkedIn (API guest): keywords + localização + filtros de data e modalidade.
# f_WT=2 → remoto | f_TPR=r259200 → últimos 3 dias
# Para outros países, altere o campo "location".
FILTROS_LINKEDIN = [
    {"nome": "FLUTTER · REMOTO", "params": {"keywords": "flutter", "location": "Brazil", "f_WT": "2", "f_TPR": "r259200", "start": 0}},
    # {"nome": "MOBILE · REMOTO",  "params": {"keywords": "mobile developer", "location": "Brazil", "f_WT": "2", "f_TPR": "r259200", "start": 0}},
]
PAGINAS_LINKEDIN = 5  # a API guest devolve ~10 vagas por página
# On 429, retry after growing pauses (or Retry-After, capped at the largest one), up to a total per run.
PAUSAS_NOVA_TENTATIVA_LINKEDIN = (10, 30, 60)
PAUSA_ENTRE_PAGINAS_LINKEDIN = 2
PAUSA_TOTAL_MAXIMA_LINKEDIN = 180  # sum of the 429 pauses in one run

# LinkedIn — publicações das últimas 24h (busca via Brave Search, requer BRAVE_API_KEY).
# "termo" é pesquisado em site:linkedin.com/posts. Cada filtro × página = 1 consulta
# (até 20 posts); o plano grátis da Brave dá ~1.000 consultas/mês (US$ 5 de crédito).
# "OR" (maiúsculo) é operador da Brave: acha posts com "remoto" ou com "remota".
FILTROS_POSTS_LINKEDIN = [
    {"nome": "FLUTTER · VAGA · REMOTO", "termo": "flutter vaga remoto OR remota"},
]
PAGINAS_POSTS_LINKEDIN = 1
# Intervalo mínimo entre buscas de publicações. O bot roda a cada 30 min, mas a
# cota da Brave (~1.000/mês) só aguenta ~500 buscas: de hora em hora gasta ~360.
INTERVALO_POSTS_LINKEDIN_MIN = 60

# Com LINKEDIN_LI_AT (cookie de login), as publicações vêm da busca do próprio
# LinkedIn (código em linkedin_login.py) e a Brave vira reserva: só é usada sem
# cookie ou com o cookie caído. É a mesma busca da aba "Publicações" do site.
# O termo pode ser mais amplo que o da Brave: com login vem o texto inteiro, e
# TERMOS_OBRIGATORIOS_POSTS (flutter + vaga + remoto) filtra depois.
FILTROS_POSTS_LINKEDIN_LOGIN = [
    {"nome": "FLUTTER · VAGA · REMOTO", "termo": "flutter vaga"},
]
# Período da busca logada: "past-24h" | "past-week" | "past-month".
# Como a página só mostra tempo relativo ("2 d"), a data da mensagem é a do dia
# em que o bot achou a publicação.
PERIODO_POSTS_LINKEDIN_LOGIN = "past-24h"
# Cada rolagem carrega mais um lote de publicações na página de resultados.
ROLAGENS_POSTS_LINKEDIN = 2
# Intervalo mínimo entre buscas logadas: buscar a cada 30 min chama atenção do LinkedIn.
INTERVALO_POSTS_LINKEDIN_LOGIN_MIN = 60
# Com o cookie caído, o bot para de abrir o LinkedIn até o secret mudar. A cada
# INTERVALO_LEMBRETE_COOKIE_HORAS ele tenta uma vez: se o mesmo cookie voltou a
# valer (ex.: verificação de segurança resolvida no navegador), a busca volta
# sozinha; se não, repete o aviso.
INTERVALO_LEMBRETE_COOKIE_HORAS = 24
AVISO_COOKIE_LINKEDIN = "O login do LinkedIn expirou. Atualize o cookie (secret LINKEDIN_LI_AT)."

# Web toda — páginas das últimas 24h em qualquer site (blogs, portais de vagas,
# sites de empresas...), também via Brave Search (código em web_search.py).
# Usa TERMOS_OBRIGATORIOS_WEB (abaixo) e EMPRESAS_IGNORADAS (comparado com o domínio).
# Cada filtro × página também gasta 1 consulta da cota da Brave.
FILTROS_WEB = [
    # Sem "vaga" nem OR: muitas páginas de vaga só trazem o cargo, e o OR da
    # Brave separa o "flutter" do resto (vêm notícias de home office em geral)
    {"nome": "FLUTTER · REMOTO", "termo": "flutter remoto"},
]
PAGINAS_WEB = 1
# Domínios fora da busca na web (subdomínios inclusos). LinkedIn e Remotar já têm
# busca própria, então ficariam repetidos (o link da web é outro e passaria como vaga nova).
# GitHub: só traz repositórios e listas de vagas, não a vaga em si.
SITES_EXCLUIDOS_WEB = ["linkedin.com", "remotar.com.br", "github.com"]
# Mesma regra de TERMOS_OBRIGATORIOS_POSTS, sem exigir "vaga": páginas de vaga
# costumam ter só o cargo no título ("Desenvolvedor Flutter · Remoto").
TERMOS_OBRIGATORIOS_WEB = [
    ["flutter"],
    ["remoto", "remota", "home office", "home-office", "homeoffice"],
]
# A página é descartada se tiver qualquer um destes termos (palavra inteira).
# Atenção: "100% remoto, sem presencial" também cai fora.
TERMOS_BLOQUEADOS_WEB = ["híbrido", "hibrido", "híbrida", "hibrida", "presencial"]
# Até quantos dias atrás buscar. Na web quase nada sobre Flutter aparece em
# 24h; uma vaga nunca é enviada duas vezes, então olhar a semana não repete.
# A Brave só filtra por dia, semana, mês ou ano: 7 = última semana.
DIAS_WEB = 7
# Intervalo mínimo entre buscas na web. Como ela olha a semana toda, rodar a
# cada 30 min quase não acha nada novo: a cada 3h gasta ~130 consultas/mês.
INTERVALO_WEB_MIN = 180
# Folga no intervalo da Brave: o disparo das 10h pode chegar segundos antes de
# completar 1h do das 9h e, sem folga, pularia a busca.
FOLGA_INTERVALO_BRAVE_MIN = 10

# A publicação só é enviada se o texto tiver ao menos um termo de CADA grupo
# (palavra inteira, case-insensitive): flutter E vaga(s) E (remoto OU remota).
TERMOS_OBRIGATORIOS_POSTS = [
    ["flutter"],
    ["vaga", "vagas"],
    ["remoto", "remota"],
]
# A publicação é descartada se o texto, o autor ou o link tiver um destes termos
# (parcial, case-insensitive): "netvagas" também pega "NetVagas Brasil" e o
# link "/posts/netvagas_..." quando a Brave não traz o nome do autor. Na Brave os
# termos também vão na busca ("-netvagas"), para não ocuparem os resultados.
TERMOS_BLOQUEADOS_POSTS = ["netvagas"]

# Inhire: busca por termo no título. Só entram vagas remotas.
# As empresas ficam na tabela inhire_tenants do banco (ver descoberta abaixo).
FILTROS_INHIRE = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter"},
    # {"nome": "MOBILE · REMOTO",  "termo": "mobile"},
]

# Inhire — descoberta automática de empresas. A Inhire não publica a lista de
# empresas, então o bot pesquisa páginas *.inhire.app no Yahoo (grátis) e,
# opcionalmente na Brave (USAR_BRAVE_DESCOBERTA_INHIRE).
# As empresas achadas ficam salvas no banco e a lista cresce a cada rodada.
# Mais termos = mais empresas encontradas.
TERMOS_DESCOBERTA_INHIRE = [
    "site:inhire.app vagas",
    "site:inhire.app flutter",
    "site:inhire.app mobile",
    "site:inhire.app desenvolvedor",
    "site:inhire.app engenheiro de software",
    "site:inhire.app tecnologia",
    "site:inhire.app remoto",
    "site:inhire.app estágio",
    "site:inhire.app analista",
    "site:inhire.app trabalhe conosco",
    "site:inhire.app front-end",
    "site:inhire.app back-end",
    "site:inhire.app react native",
    "site:inhire.app dados",
    "site:inhire.app produto",
    "site:inhire.app pleno",
    "site:inhire.app sênior",
    "site:inhire.app júnior",
    "site:inhire.app híbrido",
    "site:inhire.app são paulo",
]
# Intervalo entre descobertas (a busca por vagas continua em toda execução).
DIAS_DESCOBERTA_INHIRE = 7
# Brave acha mais empresas, mas gasta até 10 consultas da cota por termo a cada
# descoberta (20 termos ≈ 800 consultas/mês). Requer BRAVE_API_KEY.
USAR_BRAVE_DESCOBERTA_INHIRE = False
# Máximo de itens (empresas, vagas) citados numa linha do aviso de erros (o resto vira "e mais N").
LIMITE_ITENS_NO_AVISO = 10
# Segundos de espera antes de repetir uma consulta que falhou na Inhire.
PAUSA_NOVA_TENTATIVA_INHIRE = 2
# Máximo de empresas apagadas numa execução quando a Inhire diz que não existem.
# Se passar disso, é mais provável uma mudança na Inhire do que empresas saindo:
# nada é apagado e chega um aviso no Telegram.
MAX_REMOCOES_INHIRE = 10

# ──────────────────────────────────────────────────────────────────────────────
# AVISOS NO TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────
# Os problemas de todas as fontes vão numa mensagem só, no fim da execução, e no
# máximo uma a cada INTERVALO_AVISOS_MIN minutos. Dentro do intervalo eles ficam
# guardados no banco e vão junto no próximo aviso.
INTERVALO_AVISOS_MIN = 20
# O Telegram aceita até 20 mensagens por minuto num grupo: 3s entre vagas respeita isso.
PAUSA_ENTRE_VAGAS = 3
# Quando o Telegram pede para esperar (limite de mensagens), espera até isso e tenta de novo.
MAX_ESPERA_TELEGRAM = 60
# O Telegram corta mensagens acima de 4096 caracteres: o aviso para antes disso.
TAMANHO_MAX_AVISO = 3800

# Solides: busca pela página pública vagas.solides.com.br/vagas/<modalidade>/<termo>.
# 'caminho' = "<modalidade>/<termo>". A modalidade no caminho já filtra as vagas
# (ex: "remoto/flutter" só traz vagas remotas). Atenção: "todos/<termo>" ignora o termo.
FILTROS_SOLIDES = [
    {"nome": "FLUTTER · REMOTO", "caminho": "remoto/flutter"},
    # {"nome": "MOBILE · REMOTO",  "caminho": "remoto/mobile"},
]
# Seconds to wait before retrying a Solides page that came without the job list.
PAUSA_NOVA_TENTATIVA_SOLIDES = 5

# Remotar: busca pela API pública (api.remotar.com.br). A busca também olha a
# descrição, mas só vai para o grupo quem tiver TERMO_OBRIGATORIO_TITULO no título.
# 'modalidades': "remote" | "hybrid" | "on-site" (lista vazia = qualquer uma).
FILTROS_REMOTAR = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter", "modalidades": ["remote"]},
]
# Ignora vagas remotas de empresas de fora do Brasil (salário em dólar/euro ou marcadas como internacionais).
IGNORAR_VAGAS_INTERNACIONAIS_REMOTAR = True

# ──────────────────────────────────────────────────────────────────────────────
# B. PERFIL — palavras-chave e empresas que bloqueiam a vaga
# ──────────────────────────────────────────────────────────────────────────────

# Período máximo de publicação aceito. Vagas mais antigas são ignoradas.
# Dica: na primeira execução, aumente os valores para preencher o histórico
# (ex: 30 dias), depois retorne ao padrão.
DIAS_BUSCA_GUPY    = 10   # Gupy    → padrão: 10 dias
DIAS_BUSCA_SOLIDES = 20  # Solides → padrão: 20 dias
DIAS_BUSCA_REMOTAR = 30  # Remotar → padrão: 30 dias

# A vaga só é enviada se o título contiver este termo (palavra inteira,
# case-insensitive). É o único filtro aplicado ao título.
TERMO_OBRIGATORIO_TITULO = "flutter"

# Vagas dessas empresas são ignoradas em todas as fontes.
# A comparação é parcial e case-insensitive: "hired" também bloqueia "Hired Feed".
EMPRESAS_IGNORADAS = [
    "hired",
    "Hired Feed",
    "Hire Feed",
    "Jobgether",
    "Quik Hire Staffing",
    # Adicione outras empresas que deseja ignorar aqui
]

# Set em memória para evitar duplicatas na mesma execução (mesma vaga, fontes/buscas diferentes)
_enviados_sessao: set = set()

def _chave_sessao(titulo: str, empresa: str) -> str:
    normalizar = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
    return normalizar(titulo)[:60] + "|" + normalizar(empresa)[:30]

def _padrao_termos(termos):
    # Palavra inteira: o termo não pode estar colado em outra letra/número
    # (evita "ios" em "negócios", "dio" em "médio", "go" em "Google")
    alternativas = "|".join(re.escape(t.lower()) for t in sorted(set(termos), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternativas})(?!\w)")

_RE_TITULO     = _padrao_termos([TERMO_OBRIGATORIO_TITULO])
_RE_POSTS      = [_padrao_termos(grupo) for grupo in TERMOS_OBRIGATORIOS_POSTS]

def post_relevante(texto):
    t = texto.lower()
    return all(padrao.search(t) for padrao in _RE_POSTS)

def post_bloqueado(texto, autor, link):
    """Retorna o termo de TERMOS_BLOQUEADOS_POSTS achado no texto, no autor ou no link, ou None."""
    alvo = f"{texto} {autor} {link}".lower()
    return next((termo for termo in TERMOS_BLOQUEADOS_POSTS if termo.lower() in alvo), None)

def titulo_relevante(titulo):
    return _RE_TITULO.search(titulo.lower()) is not None

# --- 3. BANCO E TELEGRAM ---

def enviar_telegram(mensagem):
    payload = {
        "chat_id":                  CHAT_ID,
        "text":                     mensagem,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    for tentativa in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            print(f"❌ Erro Telegram: {e}")
            return False
        if r.status_code == 200:
            return True
        print(f"⚠️  Telegram recusou: {r.text}")
        espera = _espera_pedida_telegram(r)
        if tentativa == 1 or espera is None:
            return False
        print(f"   ⏳ Limite de mensagens do Telegram: esperando {espera}s")
        time.sleep(espera)

def _espera_pedida_telegram(resp):
    """Segundos que o Telegram pede para esperar (429), ou None se não vale tentar de novo."""
    if resp.status_code != 429:
        return None
    try:
        espera = resp.json().get("parameters", {}).get("retry_after")
    except ValueError:
        return None
    return espera if isinstance(espera, int) and espera <= MAX_ESPERA_TELEGRAM else None

def escapar(valor):
    """Texto seguro para o modo HTML do Telegram: "<" ou "&" num título fariam ele recusar a mensagem."""
    return html.escape(str(valor))

# Vagas que o Telegram não entregou nesta execução (avisadas no fim, em main)
_falhas_envio = []

# Parallel jobs (--source) only queue the jobs they find; the --send-jobs step
# sends them all at the end, so the same job found by two sites goes out once
_queue_jobs = False

ALERT_SENT_NOT_SAVED = "uma vaga foi enviada mas não ficou registrada: {title}. Ela pode chegar repetida uma vez."
ALERT_DUPLICATE_NOT_SAVED = "uma vaga repetida não ficou registrada: {title}. Ela pode chegar repetida uma vez."
ALERT_DESCRIPTION_NOT_QUEUED = "a descrição de uma vaga não vai ser guardada: {title}."
ALERT_BACKFILL_NOT_DONE = "Não foi possível recuperar as descrições das vagas antigas: {error}. Rode de novo."
ALERT_QUEUE_NOT_SENT = "Não foi possível enviar as vagas da fila: {error}. Elas vão na próxima execução."

def queue_job(db: Store, link, titulo, empresa, data_f, mensagem, fonte: Source):
    """Save the job in the database for the --send-jobs step to send."""
    db.queue_job(link, titulo, empresa, data_f, mensagem, fonte)
    print(f"   📥 Na fila: {titulo[:50]}")

def send_queued_jobs(db: Store):
    """Send the queued jobs in the order they were found (dedicated sites before web).

    A job that does not reach the group stays queued for the next run.
    """
    for job in db.queued_jobs():
        if not db.is_sent(job.link):
            # Also skips the same job already sent from another site in this step
            registrar_e_enviar(db, job.link, job.title, job.company, job.published, job.message, job.source)
        if db.is_sent(job.link):
            db.delete_queued(job.id)

def guardar_descricao_pendente(db: Store, link, titulo, empresa, data_f, fonte: Source) -> None:
    """Create the description record of a sent job (a LinkedIn post is born saved with its own text)."""
    if fonte == Source.LINKEDIN_POSTS or job_details.source_from_link(link) == Source.LINKEDIN_POSTS:
        db.add_job_description(link, titulo, empresa, fonte, data_f, description=titulo)
    else:
        db.add_job_description(link, titulo, empresa, fonte, data_f)

def recuperar_descricoes_antigas(db: Store) -> int:
    """Create the description record of every sent job that has none, skipping copies of the same job."""
    ja_registrados = db.description_links()
    enviadas = db.sent_jobs()

    def chave(job):
        return (_chave_sessao(job.title, ""), job.published)

    vistas = {chave(job) for job in enviadas if job.link in ja_registrados}
    total = 0
    for job in enviadas:
        if job.link in ja_registrados or chave(job) in vistas:
            continue
        fonte = job_details.source_from_link(job.link)
        guardar_descricao_pendente(db, job.link, job.title, "", job.published, fonte)
        vistas.add(chave(job))
        total += 1
    print(f"📝 {total} vagas antigas entraram na fila de descrição.")
    return total

def registrar_e_enviar(db: Store, link, titulo, empresa, data_f, mensagem, fonte: Source):
    if _queue_jobs:
        queue_job(db, link, titulo, empresa, data_f, mensagem, fonte)
        return
    chave = _chave_sessao(titulo, empresa)
    if chave in _enviados_sessao:
        print(f"   🔁 Duplicata (sessão): {titulo[:50]}")
        # Mark this source's link as seen too: otherwise, on the next run, this
        # source finds it unsent while the first one skips it, and it goes out twice
        try:
            db.mark_sent(link, data_f, titulo)
        except StoreError:
            anotar_avisos(Source.BOT, [ALERT_DUPLICATE_NOT_SAVED.format(title=titulo[:60])])
        return
    _enviados_sessao.add(chave)
    # Só marca como enviada se chegou: senão a vaga se perderia sem ninguém ver
    if enviar_telegram(mensagem):
        # The job already went out: a database failure must not stop the run
        try:
            db.mark_sent(link, data_f, titulo)
            print(f"   ✅ {titulo[:50]}...")
        except StoreError:
            anotar_avisos(Source.BOT, [ALERT_SENT_NOT_SAVED.format(title=titulo[:60])])
        else:
            try:
                guardar_descricao_pendente(db, link, titulo, empresa, data_f, fonte)
            except StoreError:
                anotar_avisos(Source.BOT, [ALERT_DESCRIPTION_NOT_QUEUED.format(title=titulo[:60])])
    else:
        _falhas_envio.append(f"{titulo[:60]} ({fonte.label})")
    time.sleep(PAUSA_ENTRE_VAGAS)

def resumir_lista(itens):
    """Junta os itens numa linha, com no máximo LIMITE_ITENS_NO_AVISO (o resto vira "e mais N")."""
    lista = ", ".join(itens[:LIMITE_ITENS_NO_AVISO])
    if len(itens) > LIMITE_ITENS_NO_AVISO:
        lista += f" e mais {len(itens) - LIMITE_ITENS_NO_AVISO}"
    return lista

# Problemas desta execução, na ordem em que aconteceram: (fonte, texto)
_avisos: list[tuple[Source, str]] = []

def anotar_avisos(fonte: Source, erros):
    _avisos.extend((fonte, erro) for erro in erros)

def varrer_com_aviso(fonte: Source, varrer, db: Store):
    """Roda varrer(db, erros) sem deixar um erro derrubar as outras fontes.

    Os problemas que ela anotar em `erros` (e um erro inesperado) vão para o
    aviso único do fim da execução (enviar_avisos).
    """
    erros = []
    try:
        varrer(db, erros)
    except Exception as e:
        print(f"   ❌ Varredura {fonte.label} interrompida: {e}")
        erros.append(f"A varredura parou no meio por um erro inesperado: {e}")
    anotar_avisos(fonte, erros)

class FalhaFonte(RuntimeError):
    """Falha de uma fonte já descrita numa frase curta, pronta para o aviso."""

def descrever_falha(filtro, e):
    """Frase curta para o aviso quando a busca de um filtro falha com exceção."""
    if isinstance(e, requests.RequestException):
        return f"{filtro}: o site não respondeu."
    if isinstance(e, FalhaFonte):
        return f"{filtro}: {e}."
    return f"{filtro}: erro ao ler as vagas ({e})."

def montar_aviso(avisos: list[tuple[Source, str]]):
    """Uma mensagem com os problemas agrupados por fonte; repetidos viram "(3x)"."""
    por_fonte = {}
    for fonte, texto in avisos:
        contagem = por_fonte.setdefault(fonte, {})
        contagem[texto] = contagem.get(texto, 0) + 1

    mensagem = "⚠️ <b>Problemas na varredura</b>"
    restantes = sum(len(contagem) for contagem in por_fonte.values())
    for fonte, contagem in por_fonte.items():
        bloco = f"\n\n<b>{escapar(fonte.label)}</b>"
        for texto, vezes in contagem.items():
            linha = f"\n• {escapar(texto)}" + (f" ({vezes}x)" if vezes > 1 else "")
            if len(mensagem) + len(bloco) + len(linha) > TAMANHO_MAX_AVISO:
                return mensagem + bloco + f"\n\n… e mais {restantes} problemas."
            bloco += linha
            restantes -= 1
        mensagem += bloco
    return mensagem

def store_alerts(db: Store):
    """Save this run's problems in the database, for the next alert to send."""
    db.add_alerts([(fonte.api_value, texto) for fonte, texto in _avisos])
    # Only once saved: if the database fails, main still sends these directly
    _avisos.clear()

def enviar_avisos(db: Store, agora=None):
    """Manda os problemas guardados numa mensagem só, no máximo uma a cada INTERVALO_AVISOS_MIN.

    Dentro do intervalo, ou se o Telegram falhar, eles ficam no banco e vão no próximo aviso.
    """
    # UTC: the store only takes timezone-aware datetimes
    agora = agora or datetime.now(timezone.utc)
    store_alerts(db)

    pendentes = db.pending_alerts()
    if not pendentes:
        return
    ultimo = db.last_alert_at()
    if ultimo and agora - ultimo < timedelta(minutes=INTERVALO_AVISOS_MIN):
        print(f"\n⏳ {len(pendentes)} problemas guardados para o próximo aviso (um a cada {INTERVALO_AVISOS_MIN} min)")
        return
    if enviar_telegram(montar_aviso([(Source.from_api(a.label), a.text) for a in pendentes])):
        # Only the alerts read above: another job may have saved more meanwhile
        db.delete_alerts([a.id for a in pendentes])
        db.set_last_alert_at(agora)

def brave_due(db: Store, source: Source, interval_min, now=None):
    """True when `source` last used the Brave quota `interval_min` minutes ago or more.

    When due, the current time is saved as the source's last search.
    """
    # UTC: the store only takes timezone-aware datetimes
    now = now or datetime.now(timezone.utc)
    ultimo = db.brave_searched_at(source.api_value)
    wait = timedelta(minutes=interval_min - FOLGA_INTERVALO_BRAVE_MIN)
    if ultimo and now - ultimo < wait:
        print(f"\n⏳ {source.label}: busca na Brave pulada (uma a cada {interval_min} min, para caber na cota)")
        return False
    # Saved before searching: a failed search still spends quota
    db.set_brave_searched_at(source.api_value, now)
    return True

def filtros_basicos(titulo, empresa=None):
    """Retorna (bloqueada, motivo) com os filtros de perfil."""
    if not titulo_relevante(titulo):
        return True, f"🚫 Sem \"{TERMO_OBRIGATORIO_TITULO}\" no título: {titulo[:55]}"
    if empresa:
        emp = empresa.lower()
        for emp_ignorada in EMPRESAS_IGNORADAS:
            if emp_ignorada.lower() in emp:
                return True, f"🚫 Empresa ignorada: {empresa[:50]}"
    return False, ""

# --- 4. GUPY ---

def buscar_vagas_gupy(db):
    print("\n🟣 GUPY — iniciando varredura...")
    varrer_com_aviso(Source.GUPY, _varrer_gupy, db)

def _varrer_gupy(db, erros):
    gupy.search_jobs(
        db,
        filters=FILTROS_GUPY,
        max_age_days=DIAS_BUSCA_GUPY,
        user_agent=USER_AGENT,
        check=filtros_basicos,
        send=registrar_e_enviar,
        errors=erros,
        proxy=SCRAPER_PROXY,
    )

# --- 5. PROGRAMATHOR ---

def buscar_vagas_programathor(db):
    print("\n🟤 PROGRAMATHOR — iniciando varredura...")
    varrer_com_aviso(Source.PROGRAMATHOR, _varrer_programathor, db)

def _varrer_programathor(db, erros):
    if not BS4_DISPONIVEL:
        print("   ⚠️  ProgramaThor desativado: instale beautifulsoup4")
        return
    programathor.search_jobs(
        db,
        filters=FILTROS_PROGRAMATHOR,
        user_agent=USER_AGENT,
        proxy=SCRAPER_PROXY,
        check=filtros_basicos,
        send=registrar_e_enviar,
        errors=erros,
    )

# --- 6. LINKEDIN ---

def buscar_vagas_linkedin(db):
    print("\n🔷 LINKEDIN — iniciando varredura...")
    varrer_com_aviso(Source.LINKEDIN, _varrer_linkedin, db)

def _linkedin_get(url: str, params: dict, headers: dict, pausa_restante: int) -> tuple[requests.Response, int]:
    """GET on the LinkedIn guest API retrying 429 with growing pauses, within the remaining pause budget."""
    resp = requests.get(url, params=params, headers=headers, timeout=15, proxies=scraper_proxies(SCRAPER_PROXY))
    for pausa_padrao in PAUSAS_NOVA_TENTATIVA_LINKEDIN:
        if resp.status_code != 429:
            break
        retry_after = resp.headers.get("Retry-After")
        if isinstance(retry_after, str) and retry_after.strip().isdecimal():
            pausa = min(int(retry_after.strip()), max(PAUSAS_NOVA_TENTATIVA_LINKEDIN))
        else:
            pausa = pausa_padrao
        if pausa > pausa_restante:
            break
        print(f"   ⏳ LinkedIn pediu uma pausa, nova tentativa em {pausa}s")
        time.sleep(pausa)
        pausa_restante -= pausa
        resp = requests.get(url, params=params, headers=headers, timeout=15, proxies=scraper_proxies(SCRAPER_PROXY))
    return resp, pausa_restante

def _varrer_linkedin(db, erros):
    if not BS4_DISPONIVEL:
        print("   ⚠️  LinkedIn desativado: instale beautifulsoup4")
        return

    headers = {
        'User-Agent': USER_AGENT,
    }
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    pausa_restante = PAUSA_TOTAL_MAXIMA_LINKEDIN
    for filtro in FILTROS_LINKEDIN:
        print(f"\n   🔎 {filtro['nome']}...")
        for pagina in range(PAGINAS_LINKEDIN):
            params = filtro["params"].copy()
            params["start"] = pagina * 10
            if pagina > 0:
                time.sleep(PAUSA_ENTRE_PAGINAS_LINKEDIN)
            try:
                resp, pausa_restante = _linkedin_get(url, params, headers, pausa_restante)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    erros.append(f"{filtro['nome']}: o LinkedIn recusou a busca ({resp.status_code}).")
                    break

                soup  = BeautifulSoup(resp.text, 'html.parser')
                cards = soup.find_all('div', class_='base-card')

                if not cards:
                    print("   🔚 Nenhuma vaga ou resposta bloqueada.")
                    break

                for card in cards:
                    titulo_el  = card.find(class_=lambda c: c and 'title' in c)
                    empresa_el = card.find(class_=lambda c: c and 'subtitle' in c)
                    link_el    = card.find('a', href=True)
                    data_el    = card.find('time')

                    titulo  = titulo_el.get_text(strip=True)  if titulo_el  else "Título Indisponível"
                    empresa = empresa_el.get_text(strip=True) if empresa_el else "Empresa não informada"
                    link    = link_el['href'].split('?')[0]   if link_el    else ''

                    if not link:
                        continue

                    try:
                        data_iso = data_el.get('datetime', '') if data_el else ''
                        data_pub = datetime.strptime(data_iso, "%Y-%m-%d")
                        data_f   = data_pub.strftime("%d/%m/%Y")
                        hora_f   = "--:--"
                    except Exception:
                        data_f, hora_f = "Sem data", "--:--"

                    bloqueada, motivo = filtros_basicos(titulo, empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue

                    if db.is_sent(link):
                        continue

                    mensagem = (
                        f"🔷 <b>LINKEDIN — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {escapar(titulo)}\n"
                        f"🏢 <b>Empresa:</b> {escapar(empresa)}\n"
                        f"📅 <b>Data:</b> {data_f}\n\n"
                        f"🔗 <a href='{escapar(link)}'>Aplicar no LinkedIn</a>"
                    )
                    registrar_e_enviar(db, link, titulo, empresa, data_f, mensagem, Source.LINKEDIN)

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                erros.append(descrever_falha(filtro['nome'], e))
                break

# --- 6b. LINKEDIN (PUBLICAÇÕES) ---

def buscar_posts_linkedin(db):
    if not LINKEDIN_LI_AT and not BRAVE_API_KEY:
        print("\n⚠️  Publicações do LinkedIn desativadas: defina LINKEDIN_LI_AT ou BRAVE_API_KEY no .env")
        return

    print("\n📝 LINKEDIN PUBLICAÇÕES — iniciando varredura...")
    varrer_com_aviso(Source.LINKEDIN_POSTS, _varrer_posts_linkedin, db)

def _varrer_posts_linkedin(db, erros):
    if LINKEDIN_LI_AT:
        estado = estado_login_linkedin(db, LINKEDIN_LI_AT)
        if estado == "espera":
            return
        if estado == "ok" and _posts_com_login(db, erros):
            return
    # Sem cookie, com o cookie caído ou sem Playwright: a Brave é a reserva
    if BRAVE_API_KEY and brave_due(db, Source.LINKEDIN_POSTS, INTERVALO_POSTS_LINKEDIN_MIN):
        _posts_pela_brave(db, erros)

def _hash_cookie(cookie):
    # Only a fingerprint goes to the database: the cookie is a secret
    return hashlib.sha256(cookie.encode()).hexdigest()[:16]

def estado_login_linkedin(db: Store, cookie, agora=None):
    """"ok" (pode buscar agora), "espera" (dentro do intervalo) ou "expirado" (pausado).

    Com o cookie caído, devolve "ok" uma vez a cada INTERVALO_LEMBRETE_COOKIE_HORAS
    para tentar de novo: se falhar, marcar_login_expirado repete o aviso.
    Um cookie novo (secret trocado) recomeça do zero.
    """
    # UTC: the store only takes timezone-aware datetimes
    agora = agora or datetime.now(timezone.utc)
    chave = _hash_cookie(cookie)
    db.delete_linkedin_sessions_except(chave)
    sessao = db.linkedin_session(chave)
    folga = timedelta(minutes=FOLGA_INTERVALO_BRAVE_MIN)

    if sessao and sessao.expired_at:
        lembrete = timedelta(hours=INTERVALO_LEMBRETE_COOKIE_HORAS) - folga
        if sessao.reminded_at and agora - sessao.reminded_at < lembrete:
            print("\n🔒 LinkedIn: cookie de login expirado, busca logada pausada até o secret mudar")
            return "expirado"
        print(f"\n🔁 LinkedIn: cookie expirado há {INTERVALO_LEMBRETE_COOKIE_HORAS}h, tentando de novo")
        # Saved before searching: a retry that fails for another reason waits another day
        db.save_linkedin_session(chave, searched_at=agora, reminded_at=agora)
        return "ok"

    espera = timedelta(minutes=INTERVALO_POSTS_LINKEDIN_LOGIN_MIN) - folga
    if sessao and sessao.searched_at and agora - sessao.searched_at < espera:
        print(f"\n⏳ LinkedIn: busca logada pulada (uma a cada {INTERVALO_POSTS_LINKEDIN_LOGIN_MIN} min)")
        return "espera"
    # Saved before searching, like brave_due: a failed search still counts as an access
    db.save_linkedin_session(chave, searched_at=agora)
    return "ok"

def marcar_login_expirado(db: Store, cookie, erros, agora=None):
    agora = agora or datetime.now(timezone.utc)
    db.save_linkedin_session(_hash_cookie(cookie), expired_at=agora, reminded_at=agora)
    erros.append(AVISO_COOKIE_LINKEDIN)

def marcar_login_valido(db: Store, cookie):
    """LinkedIn aceitou o cookie: tira a pausa, se havia (o mesmo cookie voltou a valer)."""
    db.save_linkedin_session(_hash_cookie(cookie), expired_at=None, reminded_at=None)

def _posts_com_login(db, erros):
    """Busca logada. False quando ela não pôde rodar (cookie caído, sem Playwright): vale a reserva."""
    if not BS4_DISPONIVEL:
        return False
    for filtro in FILTROS_POSTS_LINKEDIN_LOGIN:
        print(f"\n   🔎 {filtro['nome']} (logado)...")
        try:
            # No USER_AGENT here: linkedin_login builds one that matches the browser it launches
            posts = linkedin_login.search_posts(LINKEDIN_LI_AT, filtro["termo"], ROLAGENS_POSTS_LINKEDIN,
                                                period=PERIODO_POSTS_LINKEDIN_LOGIN, proxy=SCRAPER_PROXY)
        except linkedin_login.LoginExpired as e:
            print(f"   🔒 O LinkedIn pediu login de novo ({e}): cookie expirado")
            marcar_login_expirado(db, LINKEDIN_LI_AT, erros)
            return False
        except ImportError:
            print("   ⚠️  Busca logada desativada: instale o playwright (pip install playwright)")
            return False
        except linkedin_login.SearchBlocked as e:
            print(f"   🛑 HTTP {e.status}")
            erros.append(f"{filtro['nome']}: o LinkedIn bloqueou a busca de publicações (código {e.status}).")
            continue
        except linkedin_login.ResultsNotFound:
            print("   🛑 Nem publicações nem \"nenhum resultado\" na página")
            erros.append(f"{filtro['nome']}: a página de publicações do LinkedIn não carregou ou mudou de formato.")
            continue
        except Exception as e:
            print(f"   ⚠️  Erro: {e}")
            erros.append(descrever_falha(filtro['nome'], e))
            continue
        # Only a search that went through proves the cookie is valid again
        marcar_login_valido(db, LINKEDIN_LI_AT)
        print(f"   📄 {len(posts)} publicações na página")
        _enviar_posts_linkedin(db, filtro, posts)
    return True

def _posts_pela_brave(db, erros):
    for filtro in FILTROS_POSTS_LINKEDIN:
        print(f"\n   🔎 {filtro['nome']}...")
        falhas_brave = []
        try:
            posts = linkedin_posts.search_posts(BRAVE_API_KEY, filtro["termo"], PAGINAS_POSTS_LINKEDIN,
                                                errors=falhas_brave, excluded_terms=TERMOS_BLOQUEADOS_POSTS)
        except Exception as e:
            print(f"   ⚠️  Erro: {e}")
            erros.append(descrever_falha(filtro['nome'], e))
            continue
        # 429 = cota mensal da Brave esgotada ou buscas demais
        erros += [f"{filtro['nome']}: a Brave recusou a busca ({falha})." for falha in falhas_brave]
        _enviar_posts_linkedin(db, filtro, posts)

def _enviar_posts_linkedin(db, filtro, posts):
    for post in posts:
        texto, autor, link = post["text"], post["author"], post["link"]

        if not post_relevante(texto):
            print(f"   🚫 Sem flutter + vaga(s) + remoto/remota: {texto[:55]}")
            continue

        if termo := post_bloqueado(texto, autor, link):
            print(f"   🚫 Termo bloqueado \"{termo}\": {autor[:50]}")
            continue

        bloqueada, motivo = filtros_basicos(texto, autor)
        if bloqueada:
            print(f"   {motivo}")
            continue

        if db.is_sent(link):
            continue

        resumo = texto if len(texto) <= 300 else texto[:300].rsplit(" ", 1)[0] + "…"

        mensagem = (
            f"📝 <b>LINKEDIN PUBLICAÇÃO — {filtro['nome']}</b>\n\n"
            f"👤 <b>Autor:</b> {escapar(autor)}\n"
            f"📅 <b>Data:</b> {escapar(post['date'])}\n\n"
            f"💬 {escapar(resumo)}\n\n"
            f"🔗 <a href='{escapar(link)}'>Ver publicação</a>"
        )
        registrar_e_enviar(db, link, texto, autor, post["date"], mensagem, Source.LINKEDIN_POSTS)

# --- 7. INHIRE ---

def buscar_vagas_inhire(db):
    print("\n🟣 INHIRE — iniciando varredura...")
    varrer_com_aviso(Source.INHIRE, _varrer_inhire, db)

def _get_inhire(url, headers):
    """GET na API da Inhire com uma nova tentativa: ela às vezes falha e volta logo em seguida."""
    for tentativa in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=15, proxies=scraper_proxies(SCRAPER_PROXY))
        except requests.RequestException:
            if tentativa == 1:
                raise
        else:
            if resp.status_code == 200 or "Tenant not found" in resp.text or tentativa == 1:
                return resp
        time.sleep(PAUSA_NOVA_TENTATIVA_INHIRE)

def _varrer_inhire(db, erros):
    headers = {
        'User-Agent': USER_AGENT,
    }
    url_base = "https://api.inhire.app/job-posts/public/pages"

    empresas = inhire_discovery.discover(
        db,
        queries=TERMOS_DESCOBERTA_INHIRE,
        brave_api_key=BRAVE_API_KEY if USAR_BRAVE_DESCOBERTA_INHIRE else None,
        every=timedelta(days=DIAS_DESCOBERTA_INHIRE),
        errors=erros,
        proxy=SCRAPER_PROXY,
    )
    print(f"   📋 {len(empresas)} empresas para verificar")
    filtros = [(filtro, _padrao_termos([filtro['termo']])) for filtro in FILTROS_INHIRE]
    sem_resposta = []
    erro_leitura = []
    inexistentes = []

    for empresa_slug in empresas:
        print(f"\n   🏢 {empresa_slug.upper()}...")
        headers['X-Tenant'] = empresa_slug
        
        try:
            resp = _get_inhire(url_base, headers)
            # Só conta com a resposta exata da Inhire: um 404 genérico (API fora
            # do ar ou endereço mudado) não quer dizer que a empresa saiu
            if resp.status_code == 404 and "Tenant not found" in resp.text:
                print("   🗑️  Empresa não existe na Inhire")
                inexistentes.append(empresa_slug)
                continue
            if resp.status_code != 200:
                print(f"   🛑 HTTP {resp.status_code}")
                sem_resposta.append(f"{empresa_slug} ({resp.status_code})")
                continue
                
            dados = resp.json()
            jobs = dados.get('jobsPage', [])
            nome_empresa = dados.get('tenantName', empresa_slug.capitalize())
            
            if not jobs:
                print("   🔚 Nenhuma vaga encontrada.")
                continue

            for filtro, padrao_termo in filtros:
                for job in jobs:
                    if job.get('status') != 'published':
                        continue

                    # Fields may come as null: .get(key, default) does not cover that
                    titulo = job.get('displayName') or 'Título Indisponível'
                    if not padrao_termo.search(titulo.lower()):
                        continue

                    if (job.get('workplaceType') or '').lower() != 'remote':
                        continue
                        
                    job_id = job.get('jobId')
                    titulo_slug = unicodedata.normalize('NFKD', titulo).encode('ascii', 'ignore').decode('utf-8')
                    titulo_slug = re.sub(r'[^\w\s-]', '', titulo_slug).strip().lower()
                    titulo_slug = re.sub(r'[-\s]+', '-', titulo_slug)
                    link = f"https://{empresa_slug}.inhire.app/vagas/{job_id}/{titulo_slug}"
                    
                    bloqueada, motivo = filtros_basicos(titulo, nome_empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue
                        
                    if db.is_sent(link):
                        continue
                        
                    local = job.get('location') or 'Não informado'
                    data_f = datetime.now().strftime("%d/%m/%Y")
                    
                    mensagem = (
                        f"🟣 <b>INHIRE — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {escapar(titulo)}\n"
                        f"🏢 <b>Empresa:</b> {escapar(nome_empresa)}\n"
                        f"📍 <b>Local:</b> {escapar(local)}\n"
                        f"💻 <b>Modelo:</b> Remoto\n"
                        f"📅 <b>Data (Descoberta):</b> {data_f}\n\n"
                        f"🔗 <a href='{escapar(link)}'>Aplicar na Inhire</a>"
                    )
                    registrar_e_enviar(db, link, titulo, nome_empresa, data_f, mensagem, Source.INHIRE)
                    
        except requests.RequestException as e:
            print(f"   ⚠️  Inhire não respondeu para {empresa_slug}: {e}")
            sem_resposta.append(f"{empresa_slug} (sem resposta)")
        except Exception as e:
            print(f"   ⚠️  Erro ao ler vagas de {empresa_slug}: {e}")
            erro_leitura.append(empresa_slug)

    # Apaga no fim, e só se forem poucas: muitas de uma vez indica mudança na
    # Inhire, e apagar esvaziaria o banco
    if len(inexistentes) > MAX_REMOCOES_INHIRE:
        erros.append(f"A Inhire disse que {len(inexistentes)} de {len(empresas)} empresas não existem. "
                     "Parece uma mudança na Inhire, então nenhuma foi apagada.")
    else:
        for empresa_slug in inexistentes:
            inhire_discovery.remove_tenant(db, empresa_slug)
        if inexistentes:
            print(f"   🗑️  {len(inexistentes)} empresas removidas da lista: {', '.join(inexistentes)}")

    if sem_resposta:
        erros.append(f"{len(sem_resposta)} de {len(empresas)} empresas não responderam: {resumir_lista(sem_resposta)}.")
    if erro_leitura:
        erros.append(f"Não foi possível ler as vagas de {len(erro_leitura)} de {len(empresas)} empresas "
                     f"(a resposta veio num formato inesperado): {resumir_lista(erro_leitura)}.")

# --- 8. SOLIDES ---

def _solides_flight(pagina_html):
    """Junta os blocos self.__next_f.push(...) da página Next.js num texto só."""
    partes = []
    for bloco in re.findall(r'self\.__next_f\.push\((\[.*?\])\)</script>', pagina_html, re.S):
        try:
            item = json.loads(bloco)
        except ValueError:
            continue
        if len(item) > 1 and isinstance(item[1], str):
            partes.append(item[1])
    return "".join(partes)

def _solides_textos(flight):
    """Mapeia id -> texto das linhas "<id>:T<tam>,<texto>" do payload RSC.

    Essas linhas não terminam em quebra de linha: o tamanho (hex, em bytes UTF-8)
    é a única forma segura de saber onde o texto acaba.
    """
    dados = flight.encode('utf-8')
    textos, pos = {}, 0
    # O id pode vir vazio (ex: ":HL[...]", dicas de pré-carregamento)
    padrao_linha = re.compile(rb'([0-9a-f]*):(T([0-9a-f]+),)?')
    while pos < len(dados):
        m = padrao_linha.match(dados, pos)
        if m and m.group(2):
            fim = m.end() + int(m.group(3), 16)
            textos[m.group(1).decode()] = dados[m.end():fim].decode('utf-8', errors='ignore')
            pos = fim
        else:
            fim = dados.find(b'\n', pos)
            pos = len(dados) if fim == -1 else fim + 1
    return textos

def _solides_pagina(caminho, pagina, headers):
    """Retorna (vagas, total_paginas, textos) lidos da página pública da Solides."""
    url = f"https://vagas.solides.com.br/vagas/{caminho}"
    for tentativa in range(2):
        resp = requests.get(url, headers=headers, params={'page': pagina}, timeout=20,
                            proxies=scraper_proxies(SCRAPER_PROXY))
        if resp.status_code != 200:
            raise FalhaFonte(f"o site recusou a busca ({resp.status_code})")
        flight = _solides_flight(resp.text)
        inicio = flight.find('"initialData":')
        if inicio != -1:
            break
        if tentativa == 0:
            time.sleep(PAUSA_NOVA_TENTATIVA_SOLIDES)
    else:
        # initialData is still missing after the retry: the page layout changed
        m = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.S | re.I)
        titulo = re.sub(r'\s+', ' ', m.group(1)).strip()[:100] if m else ''
        titulo = titulo or '(sem título)'
        tem_push = 'sim' if 'self.__next_f.push' in resp.text else 'não'
        print(f"   🔍 Solides sem initialData: HTTP {resp.status_code}, {len(resp.text)} caracteres, "
              f"título: {titulo}, self.__next_f.push: {tem_push}")
        raise FalhaFonte("a página mudou de formato e o bot não conseguiu ler as vagas")
    dados, _ = json.JSONDecoder().raw_decode(flight, inicio + len('"initialData":'))
    return dados.get('data', []), dados.get('totalPages', 1), _solides_textos(flight)

def buscar_vagas_solides(db):
    print("\n🟢 SOLIDES — iniciando varredura...")
    varrer_com_aviso(Source.SOLIDES, _varrer_solides, db)

def _varrer_solides(db, erros):
    headers = {
        'User-Agent': USER_AGENT,
    }

    for filtro in FILTROS_SOLIDES:
        print(f"\n   🔎 {filtro['nome']}...")

        for pagina in range(1, 10):
            try:
                vagas, total_pages, textos = _solides_pagina(filtro['caminho'], pagina, headers)

                if not vagas:
                    print("   🔚 Sem mais vagas.")
                    break

                for vaga in vagas:
                    # Campos podem vir null no JSON: .get(chave, padrão) não cobre esse caso
                    titulo = (vaga.get('title') or 'Título Indisponível').strip()

                    slug     = vaga.get('slug')
                    vaga_id  = vaga.get('id')
                    if not slug or not vaga_id:
                        continue
                    link = f"https://{slug}.vagas.solides.com.br/vaga/{vaga_id}"

                    empresa = (vaga.get('companyName') or 'Empresa não informada').strip()
                    bloqueada, motivo = filtros_basicos(titulo, empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue

                    if db.is_sent(link):
                        continue

                    data_iso = vaga.get('createdAt', '')
                    if data_iso:
                        try:
                            data_pub = datetime.strptime(data_iso[:10], "%Y-%m-%d")
                            data_f = data_pub.strftime("%d/%m/%Y")
                            if datetime.now() - data_pub > timedelta(days=DIAS_BUSCA_SOLIDES):
                                print(f"   📅 Vaga antiga ({data_f}). Pulando.")
                                continue
                        except Exception:
                            data_f = data_iso
                    else:
                        data_f = "Não informado"

                    cidade_info = vaga.get('city') or {}
                    estado_info = vaga.get('state') or {}
                    local = f"{cidade_info.get('name') or ''} - {estado_info.get('code') or ''}".strip(" -")
                    if not local:
                        local = "Brasil"

                    modelo_api = (vaga.get('jobType') or '').lower()
                    modelo = modelo_api.capitalize() if modelo_api else "Não informado"

                    mensagem = (
                        f"🟢 <b>SOLIDES — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {escapar(titulo)}\n"
                        f"🏢 <b>Empresa:</b> {escapar(empresa)}\n"
                        f"📍 <b>Local:</b> {escapar(local)}\n"
                        f"💻 <b>Modelo:</b> {escapar(modelo)}\n"
                        f"📅 <b>Data:</b> {escapar(data_f)}\n\n"
                        f"🔗 <a href='{escapar(link)}'>Aplicar na Solides</a>"
                    )
                    registrar_e_enviar(db, link, titulo, empresa, data_f, mensagem, Source.SOLIDES)

                if pagina >= total_pages:
                    break

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                erros.append(descrever_falha(filtro['nome'], e))
                break

# --- 9. REMOTAR ---

def buscar_vagas_remotar(db):
    print("\n🟠 REMOTAR — iniciando varredura...")
    varrer_com_aviso(Source.REMOTAR, _varrer_remotar, db)

def _varrer_remotar(db, erros):
    remotar.search_jobs(
        db,
        filters=FILTROS_REMOTAR,
        max_age_days=DIAS_BUSCA_REMOTAR,
        ignore_foreign=IGNORAR_VAGAS_INTERNACIONAIS_REMOTAR,
        user_agent=USER_AGENT,
        check=filtros_basicos,
        send=registrar_e_enviar,
        errors=erros,
        proxy=SCRAPER_PROXY,
    )

# --- MAIN ---

def buscar_vagas_web(db):
    varrer_com_aviso(Source.WEB, _varrer_web, db)

def _varrer_web(db, erros):
    if BRAVE_API_KEY and not brave_due(db, Source.WEB, INTERVALO_WEB_MIN):
        return
    web_search.search_jobs(
        db,
        api_key=BRAVE_API_KEY,
        filters=FILTROS_WEB,
        pages=PAGINAS_WEB,
        excluded_sites=SITES_EXCLUIDOS_WEB,
        required_terms=TERMOS_OBRIGATORIOS_WEB,
        blocked_terms=TERMOS_BLOQUEADOS_WEB,
        max_age_days=DIAS_WEB,
        ignored_companies=EMPRESAS_IGNORADAS,
        send=registrar_e_enviar,
        errors=erros,
    )

def anotar_falhas_envio():
    """Anota no aviso as vagas que o Telegram não entregou nesta execução."""
    if not _falhas_envio:
        return
    anotar_avisos(Source.TELEGRAM, [
        f"{len(_falhas_envio)} vagas não chegaram ao grupo: {resumir_lista(_falhas_envio)}. "
        "Elas serão enviadas de novo na próxima execução."
    ])

# Sources the workflow runs as parallel jobs (python main.py --source <name>).
# Function names, not the functions, so tests can patch them. The order of a
# full run comes from Source.search_sources().
SOURCE_FUNCTIONS = {
    Source.GUPY: "buscar_vagas_gupy",
    Source.LINKEDIN: "buscar_vagas_linkedin",
    Source.LINKEDIN_POSTS: "buscar_posts_linkedin",
    Source.INHIRE: "buscar_vagas_inhire",
    Source.SOLIDES: "buscar_vagas_solides",
    Source.REMOTAR: "buscar_vagas_remotar",
    Source.WEB: "buscar_vagas_web",
}
# command line key -> (function name, source)
SOURCES = {s.api_value: (SOURCE_FUNCTIONS[s], s) for s in Source.search_sources()}
# Sources that skip the scan without beautifulsoup4
BS4_SOURCES = {Source.LINKEDIN.api_value}

AVISO_FONTE_INTERROMPIDA = "A busca parou antes do fim (travou ou deu erro). Os outros sites não foram afetados."
# EX_TEMPFAIL (sysexits.h): the workflow runs the source again on another runner
EXIT_STORE_UNREACHABLE = 75
# The proxy (WARP) is not usable: no source runs, so no job site sees the VPS's own IP
EXIT_PROXY_DOWN = 78
AVISO_WARP_FORA = "Não consegui ligar o WARP, nenhuma busca foi feita."

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Busca vagas e envia ao grupo do Telegram.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", choices=SOURCES, help="roda só esta fonte e guarda os avisos sem enviar")
    mode.add_argument("--failed", choices=SOURCES, help="anota que a fonte parou antes do fim")
    mode.add_argument("--send-jobs", action="store_true", help="envia as vagas guardadas pelos jobs")
    mode.add_argument("--send-alerts", action="store_true", help="envia os avisos guardados")
    mode.add_argument("--backfill-descriptions", action="store_true",
                      help="cria a fila de descrição das vagas já enviadas")
    mode.add_argument("--check-proxy", action="store_true", help="confere o WARP antes da rodada")
    parser.add_argument("--send-now", action="store_true", help="com --source: envia cada vaga assim que encontrada")
    parser.add_argument("--defer-unreachable", action="store_true",
                        help="banco fora do ar: sai com código 75, sem avisar, para outra máquina tentar")
    args = parser.parse_args(argv)
    if args.send_now and not args.source:
        parser.error("--send-now só vale com --source")
    return args

def main(argv=()):
    global _queue_jobs
    args = parse_args(argv)
    # Set on every call: a single-source run only queues (unless --send-now), the others send
    _queue_jobs = bool(args.source) and not args.send_now
    if not TOKEN or not CHAT_ID:
        print("❌ ERRO: Token do Telegram ou Chat ID não encontrados no arquivo .env!")
        return

    try:
        db = from_env()
        db.connect()
    except Exception as e:
        print(f"❌ Erro ao abrir o banco: {e}")
        # Some runners cannot reach the VPS for minutes while others can: the workflow
        # retries on a fresh runner, and that one alerts if it fails too
        if args.defer_unreachable and isinstance(e, StoreError) and e.kind == StoreFailure.UNAVAILABLE:
            sys.exit(EXIT_STORE_UNREACHABLE)
        # Sem banco não dá para guardar o aviso: vai direto
        enviar_telegram(montar_aviso([(Source.BOT, f"Não foi possível abrir o banco de vagas, nenhuma busca foi feita: {e}")]))
        return

    if args.check_proxy:
        if SCRAPER_PROXY and warp_is_on(SCRAPER_PROXY):
            print("✅ WARP ligado.")
            return 0
        print(f"❌ {AVISO_WARP_FORA}")
        anotar_avisos(Source.BOT, [AVISO_WARP_FORA])
        try:
            store_alerts(db)
        except Exception as e:
            print(f"❌ Erro ao guardar os avisos: {e}")
            enviar_telegram(montar_aviso(_avisos))
        return EXIT_PROXY_DOWN

    if args.source and os.getenv(ENV_REQUIRE_SCRAPER_PROXY) == "true" and not SCRAPER_PROXY:
        # --check-proxy already alerted at the start of the round
        print("❌ SCRAPER_PROXY vazio com REQUIRE_SCRAPER_PROXY=true: a fonte não roda.")
        sys.exit(EXIT_PROXY_DOWN)

    # Parallel jobs only save their problems: the --send-alerts step sends one alert for all
    parallel_step = bool(args.source or args.failed or args.send_jobs or args.backfill_descriptions)
    # Full run (local) by default; the workflow runs each step in its own job
    if args.source:
        sources = [args.source]
    elif parallel_step or args.send_alerts:
        sources = []
    else:
        sources = list(SOURCES)
    if not BS4_DISPONIVEL and BS4_SOURCES & set(sources):
        anotar_avisos(Source.BOT, [AVISO_SEM_BS4])
    for source in sources:
        globals()[SOURCES[source][0]](db)
    # Full run: also sends what a workflow run left queued (e.g. Telegram refused it)
    if args.send_jobs or not (parallel_step or args.send_alerts):
        try:
            send_queued_jobs(db)
        except StoreError as e:
            anotar_avisos(Source.BOT, [ALERT_QUEUE_NOT_SENT.format(error=e)])
    anotar_falhas_envio()

    if args.backfill_descriptions:
        try:
            recuperar_descricoes_antigas(db)
        except StoreError as e:
            anotar_avisos(Source.BOT, [ALERT_BACKFILL_NOT_DONE.format(error=e)])

    if args.failed:
        anotar_avisos(SOURCES[args.failed][1], [AVISO_FONTE_INTERROMPIDA])

    try:
        if parallel_step:
            store_alerts(db)
        else:
            enviar_avisos(db)
    except Exception as e:
        # Banco falhou ao guardar os avisos: manda os desta execução direto
        print(f"❌ Erro ao guardar os avisos: {e}")
        enviar_telegram(montar_aviso(_avisos + [(Source.BOT, f"Não foi possível guardar os avisos no banco: {e}")]))

    print("\n✅ Varredura completa!" if sources else "\n✅ Pronto!")

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
