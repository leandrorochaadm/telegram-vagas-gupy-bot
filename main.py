import os
import re
import html
import json
import time
import sqlite3
import requests
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import gupy
import inhire_discovery
import linkedin_posts
import programathor
import remotar
import web_search

try:
    from bs4 import BeautifulSoup
    BS4_DISPONIVEL = True
except ImportError:
    BS4_DISPONIVEL = False
    print("⚠️  beautifulsoup4 não instalado — LinkedIn desativado. Rode: pip install beautifulsoup4")

AVISO_SEM_BS4 = "ProgramaThor e LinkedIn desligados: falta instalar o beautifulsoup4 (pip install beautifulsoup4)."

# --- 1. CONFIG ---
DIRETORIO_ATUAL = os.path.dirname(os.path.abspath(__file__))
CAMINHO_BANCO   = os.path.join(DIRETORIO_ATUAL, 'vagas_gupy.db')

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
# Set by the workflow to WARP's local SOCKS proxy: Cloudflare blocks the runners' datacenter IPs
SCRAPER_PROXY = os.getenv("SCRAPER_PROXY")

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
# Quando o LinkedIn recusa por excesso de buscas (429), espera e tenta de novo uma vez.
PAUSA_NOVA_TENTATIVA_LINKEDIN = 10

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
SITES_EXCLUIDOS_WEB = ["linkedin.com", "remotar.com.br"]
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

def titulo_relevante(titulo):
    return _RE_TITULO.search(titulo.lower()) is not None

# --- 3. BANCO E TELEGRAM ---

def iniciar_banco():
    conn   = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vagas_enviadas (
            link TEXT PRIMARY KEY,
            data_publicacao TEXT,
            titulo TEXT
        )
    ''')
    conn.commit()
    return conn, cursor

def ja_enviada(cursor, link):
    cursor.execute('SELECT 1 FROM vagas_enviadas WHERE link = ?', (link,))
    return cursor.fetchone() is not None

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

def registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, fonte):
    chave = _chave_sessao(titulo, empresa)
    if chave in _enviados_sessao:
        print(f"   🔁 Duplicata (sessão): {titulo[:50]}")
        # Mark this source's link as seen too: otherwise, on the next run, this
        # source finds it unsent while the first one skips it, and it goes out twice
        cursor.execute('INSERT OR IGNORE INTO vagas_enviadas VALUES (?, ?, ?)', (link, data_f, titulo))
        conn.commit()
        return
    _enviados_sessao.add(chave)
    # Só marca como enviada se chegou: senão a vaga se perderia sem ninguém ver
    if enviar_telegram(mensagem):
        cursor.execute('INSERT OR IGNORE INTO vagas_enviadas VALUES (?, ?, ?)', (link, data_f, titulo))
        conn.commit()
        print(f"   ✅ {titulo[:50]}...")
    else:
        _falhas_envio.append(f"{titulo[:60]} ({fonte})")
    time.sleep(PAUSA_ENTRE_VAGAS)

def resumir_lista(itens):
    """Junta os itens numa linha, com no máximo LIMITE_ITENS_NO_AVISO (o resto vira "e mais N")."""
    lista = ", ".join(itens[:LIMITE_ITENS_NO_AVISO])
    if len(itens) > LIMITE_ITENS_NO_AVISO:
        lista += f" e mais {len(itens) - LIMITE_ITENS_NO_AVISO}"
    return lista

# Problemas desta execução, na ordem em que aconteceram: (fonte, texto)
_avisos = []

def anotar_avisos(fonte, erros):
    _avisos.extend((fonte, erro) for erro in erros)

def varrer_com_aviso(fonte, varrer, conn, cursor):
    """Roda varrer(conn, cursor, erros) sem deixar um erro derrubar as outras fontes.

    Os problemas que ela anotar em `erros` (e um erro inesperado) vão para o
    aviso único do fim da execução (enviar_avisos).
    """
    erros = []
    try:
        varrer(conn, cursor, erros)
    except Exception as e:
        print(f"   ❌ Varredura {fonte} interrompida: {e}")
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

def montar_aviso(avisos):
    """Uma mensagem com os problemas agrupados por fonte; repetidos viram "(3x)"."""
    por_fonte = {}
    for fonte, texto in avisos:
        contagem = por_fonte.setdefault(fonte, {})
        contagem[texto] = contagem.get(texto, 0) + 1

    mensagem = "⚠️ <b>Problemas na varredura</b>"
    restantes = sum(len(contagem) for contagem in por_fonte.values())
    for fonte, contagem in por_fonte.items():
        bloco = f"\n\n<b>{escapar(fonte)}</b>"
        for texto, vezes in contagem.items():
            linha = f"\n• {escapar(texto)}" + (f" ({vezes}x)" if vezes > 1 else "")
            if len(mensagem) + len(bloco) + len(linha) > TAMANHO_MAX_AVISO:
                return mensagem + bloco + f"\n\n… e mais {restantes} problemas."
            bloco += linha
            restantes -= 1
        mensagem += bloco
    return mensagem

def enviar_avisos(conn, cursor, agora=None):
    """Manda os problemas guardados numa mensagem só, no máximo uma a cada INTERVALO_AVISOS_MIN.

    Dentro do intervalo, ou se o Telegram falhar, eles ficam no banco e vão no próximo aviso.
    """
    # UTC: o banco vai para o repositório e roda tanto no GitHub (UTC) quanto
    # localmente (horário de Brasília); hora local faria o intervalo errar em 3h
    agora = agora or datetime.now(timezone.utc)
    cursor.execute("CREATE TABLE IF NOT EXISTS avisos_pendentes (fonte TEXT, texto TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS avisos_enviados (enviado_em TEXT)")
    cursor.executemany("INSERT INTO avisos_pendentes VALUES (?, ?)", _avisos)
    conn.commit()
    # Só depois de gravados: se o banco falhar, main ainda manda estes direto
    _avisos.clear()

    pendentes = cursor.execute("SELECT fonte, texto FROM avisos_pendentes ORDER BY rowid").fetchall()
    if not pendentes:
        return
    ultimo = cursor.execute("SELECT enviado_em FROM avisos_enviados").fetchone()
    if ultimo and agora - datetime.fromisoformat(ultimo[0]) < timedelta(minutes=INTERVALO_AVISOS_MIN):
        print(f"\n⏳ {len(pendentes)} problemas guardados para o próximo aviso (um a cada {INTERVALO_AVISOS_MIN} min)")
        return
    if enviar_telegram(montar_aviso(pendentes)):
        cursor.execute("DELETE FROM avisos_pendentes")
        cursor.execute("DELETE FROM avisos_enviados")
        cursor.execute("INSERT INTO avisos_enviados VALUES (?)", (agora.isoformat(timespec="seconds"),))
        conn.commit()

def brave_due(conn, cursor, source, interval_min, now=None):
    """True when `source` last used the Brave quota `interval_min` minutes ago or more.

    When due, the current time is saved as the source's last search.
    """
    # UTC for the same reason as enviar_avisos: the db travels between GitHub and local runs
    now = now or datetime.now(timezone.utc)
    cursor.execute("CREATE TABLE IF NOT EXISTS brave_searches (source TEXT PRIMARY KEY, searched_at TEXT)")
    row = cursor.execute("SELECT searched_at FROM brave_searches WHERE source = ?", (source,)).fetchone()
    wait = timedelta(minutes=interval_min - FOLGA_INTERVALO_BRAVE_MIN)
    if row and now - datetime.fromisoformat(row[0]) < wait:
        print(f"\n⏳ {source}: busca na Brave pulada (uma a cada {interval_min} min, para caber na cota)")
        return False
    # Saved before searching: a failed search still spends quota
    cursor.execute("INSERT OR REPLACE INTO brave_searches VALUES (?, ?)", (source, now.isoformat(timespec="seconds")))
    conn.commit()
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

def buscar_vagas_gupy(conn, cursor):
    print("\n🟣 GUPY — iniciando varredura...")
    varrer_com_aviso("GUPY", _varrer_gupy, conn, cursor)

def _varrer_gupy(conn, cursor, erros):
    gupy.search_jobs(
        conn, cursor,
        filters=FILTROS_GUPY,
        max_age_days=DIAS_BUSCA_GUPY,
        user_agent=USER_AGENT,
        check=filtros_basicos,
        send=registrar_e_enviar,
        errors=erros,
    )

# --- 5. PROGRAMATHOR ---

def buscar_vagas_programathor(conn, cursor):
    print("\n🟤 PROGRAMATHOR — iniciando varredura...")
    varrer_com_aviso("PROGRAMATHOR", _varrer_programathor, conn, cursor)

def _varrer_programathor(conn, cursor, erros):
    if not BS4_DISPONIVEL:
        print("   ⚠️  ProgramaThor desativado: instale beautifulsoup4")
        return
    programathor.search_jobs(
        conn, cursor,
        filters=FILTROS_PROGRAMATHOR,
        user_agent=USER_AGENT,
        proxy=SCRAPER_PROXY,
        check=filtros_basicos,
        send=registrar_e_enviar,
        errors=erros,
    )

# --- 6. LINKEDIN ---

def buscar_vagas_linkedin(conn, cursor):
    print("\n🔷 LINKEDIN — iniciando varredura...")
    varrer_com_aviso("LINKEDIN", _varrer_linkedin, conn, cursor)

def _varrer_linkedin(conn, cursor, erros):
    if not BS4_DISPONIVEL:
        print("   ⚠️  LinkedIn desativado: instale beautifulsoup4")
        return

    headers = {
        'User-Agent': USER_AGENT,
    }
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    for filtro in FILTROS_LINKEDIN:
        print(f"\n   🔎 {filtro['nome']}...")
        for pagina in range(PAGINAS_LINKEDIN):
            params = filtro["params"].copy()
            params["start"] = pagina * 10
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code == 429:
                    # Excesso de buscas: o LinkedIn costuma liberar logo depois
                    print(f"   ⏳ LinkedIn pediu uma pausa, nova tentativa em {PAUSA_NOVA_TENTATIVA_LINKEDIN}s")
                    time.sleep(PAUSA_NOVA_TENTATIVA_LINKEDIN)
                    resp = requests.get(url, params=params, headers=headers, timeout=15)
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

                    if ja_enviada(cursor, link):
                        continue

                    mensagem = (
                        f"🔷 <b>LINKEDIN — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {escapar(titulo)}\n"
                        f"🏢 <b>Empresa:</b> {escapar(empresa)}\n"
                        f"📅 <b>Data:</b> {data_f}\n\n"
                        f"🔗 <a href='{escapar(link)}'>Aplicar no LinkedIn</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "LINKEDIN")

                time.sleep(1)

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                erros.append(descrever_falha(filtro['nome'], e))
                break

# --- 6b. LINKEDIN (PUBLICAÇÕES) ---

def buscar_posts_linkedin(conn, cursor):
    if not BRAVE_API_KEY:
        print("\n⚠️  Publicações do LinkedIn desativadas: defina BRAVE_API_KEY no .env")
        return

    print("\n📝 LINKEDIN PUBLICAÇÕES — iniciando varredura...")
    varrer_com_aviso("LINKEDIN PUBLICAÇÕES", _varrer_posts_linkedin, conn, cursor)

def _varrer_posts_linkedin(conn, cursor, erros):
    if not brave_due(conn, cursor, "LINKEDIN PUBLICAÇÕES", INTERVALO_POSTS_LINKEDIN_MIN):
        return
    for filtro in FILTROS_POSTS_LINKEDIN:
        print(f"\n   🔎 {filtro['nome']}...")
        falhas_brave = []
        try:
            posts = linkedin_posts.search_posts(BRAVE_API_KEY, filtro["termo"], PAGINAS_POSTS_LINKEDIN,
                                                errors=falhas_brave)
        except Exception as e:
            print(f"   ⚠️  Erro: {e}")
            erros.append(descrever_falha(filtro['nome'], e))
            continue
        # 429 = cota mensal da Brave esgotada ou buscas demais
        erros += [f"{filtro['nome']}: a Brave recusou a busca ({falha})." for falha in falhas_brave]

        for post in posts:
            texto, autor, link = post["text"], post["author"], post["link"]

            if not post_relevante(texto):
                print(f"   🚫 Sem flutter + vaga(s) + remoto/remota: {texto[:55]}")
                continue

            bloqueada, motivo = filtros_basicos(texto, autor)
            if bloqueada:
                print(f"   {motivo}")
                continue

            if ja_enviada(cursor, link):
                continue

            resumo = texto if len(texto) <= 300 else texto[:300].rsplit(" ", 1)[0] + "…"

            mensagem = (
                f"📝 <b>LINKEDIN PUBLICAÇÃO — {filtro['nome']}</b>\n\n"
                f"👤 <b>Autor:</b> {escapar(autor)}\n"
                f"📅 <b>Data:</b> {escapar(post['date'])}\n\n"
                f"💬 {escapar(resumo)}\n\n"
                f"🔗 <a href='{escapar(link)}'>Ver publicação</a>"
            )
            registrar_e_enviar(conn, cursor, link, texto, autor, post["date"], mensagem, "LINKEDIN_POST")

# --- 7. INHIRE ---

def buscar_vagas_inhire(conn, cursor):
    print("\n🟣 INHIRE — iniciando varredura...")
    varrer_com_aviso("INHIRE", _varrer_inhire, conn, cursor)

def _get_inhire(url, headers):
    """GET na API da Inhire com uma nova tentativa: ela às vezes falha e volta logo em seguida."""
    for tentativa in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException:
            if tentativa == 1:
                raise
        else:
            if resp.status_code == 200 or "Tenant not found" in resp.text or tentativa == 1:
                return resp
        time.sleep(PAUSA_NOVA_TENTATIVA_INHIRE)

def _varrer_inhire(conn, cursor, erros):
    headers = {
        'User-Agent': USER_AGENT,
    }
    url_base = "https://api.inhire.app/job-posts/public/pages"

    empresas = inhire_discovery.discover(
        conn, cursor,
        queries=TERMOS_DESCOBERTA_INHIRE,
        brave_api_key=BRAVE_API_KEY if USAR_BRAVE_DESCOBERTA_INHIRE else None,
        every=timedelta(days=DIAS_DESCOBERTA_INHIRE),
        errors=erros,
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
                        
                    if ja_enviada(cursor, link):
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
                    registrar_e_enviar(conn, cursor, link, titulo, nome_empresa, data_f, mensagem, "INHIRE")
                    
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
            inhire_discovery.remove_tenant(conn, cursor, empresa_slug)
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
    resp = requests.get(url, headers=headers, params={'page': pagina}, timeout=20)
    if resp.status_code != 200:
        raise FalhaFonte(f"o site recusou a busca ({resp.status_code})")
    flight = _solides_flight(resp.text)
    inicio = flight.find('"initialData":')
    if inicio == -1:
        # initialData sumiu: a página mudou de layout
        raise FalhaFonte("a página mudou de formato e o bot não conseguiu ler as vagas")
    dados, _ = json.JSONDecoder().raw_decode(flight, inicio + len('"initialData":'))
    return dados.get('data', []), dados.get('totalPages', 1), _solides_textos(flight)

def buscar_vagas_solides(conn, cursor):
    print("\n🟢 SOLIDES — iniciando varredura...")
    varrer_com_aviso("SOLIDES", _varrer_solides, conn, cursor)

def _varrer_solides(conn, cursor, erros):
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

                    if ja_enviada(cursor, link):
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
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "SOLIDES")

                if pagina >= total_pages:
                    break

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                erros.append(descrever_falha(filtro['nome'], e))
                break

# --- 9. REMOTAR ---

def buscar_vagas_remotar(conn, cursor):
    print("\n🟠 REMOTAR — iniciando varredura...")
    varrer_com_aviso("REMOTAR", _varrer_remotar, conn, cursor)

def _varrer_remotar(conn, cursor, erros):
    remotar.search_jobs(
        conn, cursor,
        filters=FILTROS_REMOTAR,
        max_age_days=DIAS_BUSCA_REMOTAR,
        ignore_foreign=IGNORAR_VAGAS_INTERNACIONAIS_REMOTAR,
        user_agent=USER_AGENT,
        check=filtros_basicos,
        send=registrar_e_enviar,
        errors=erros,
    )

# --- MAIN ---

def buscar_vagas_web(conn, cursor):
    varrer_com_aviso("WEB", _varrer_web, conn, cursor)

def _varrer_web(conn, cursor, erros):
    if BRAVE_API_KEY and not brave_due(conn, cursor, "WEB", INTERVALO_WEB_MIN):
        return
    web_search.search_jobs(
        conn, cursor,
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
    anotar_avisos("TELEGRAM", [
        f"{len(_falhas_envio)} vagas não chegaram ao grupo: {resumir_lista(_falhas_envio)}. "
        "Elas serão enviadas de novo na próxima execução."
    ])

def main():
    if not TOKEN or not CHAT_ID:
        print("❌ ERRO: Token do Telegram ou Chat ID não encontrados no arquivo .env!")
        return

    try:
        conn, cursor = iniciar_banco()
    except Exception as e:
        print(f"❌ Erro ao abrir o banco: {e}")
        # Sem banco não dá para guardar o aviso: vai direto
        enviar_telegram(montar_aviso([("BOT", f"Não foi possível abrir o banco de vagas, nenhuma busca foi feita: {e}")]))
        return

    if not BS4_DISPONIVEL:
        anotar_avisos("BOT", [AVISO_SEM_BS4])

    buscar_vagas_gupy(conn, cursor)
    buscar_vagas_programathor(conn, cursor)
    buscar_vagas_linkedin(conn, cursor)
    buscar_posts_linkedin(conn, cursor)
    buscar_vagas_inhire(conn, cursor)
    buscar_vagas_solides(conn, cursor)
    buscar_vagas_remotar(conn, cursor)
    # Last: dedicated sources send richer messages for the same links
    buscar_vagas_web(conn, cursor)
    anotar_falhas_envio()
    try:
        enviar_avisos(conn, cursor)
    except Exception as e:
        # Banco falhou ao guardar os avisos: manda os desta execução direto
        print(f"❌ Erro ao guardar os avisos: {e}")
        enviar_telegram(montar_aviso(_avisos + [("BOT", f"Não foi possível guardar os avisos no banco: {e}")]))

    conn.close()
    print("\n✅ Varredura completa de todas as fontes!")

if __name__ == '__main__':
    main()
