import os
import re
import html
import json
import time
import sqlite3
import requests
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import inhire_discovery
import linkedin_posts
import web_search

try:
    from bs4 import BeautifulSoup
    BS4_DISPONIVEL = True
except ImportError:
    BS4_DISPONIVEL = False
    print("⚠️  beautifulsoup4 não instalado — LinkedIn desativado. Rode: pip install beautifulsoup4")

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

# ════════════════════════════════════════════════════════════════════════════════
# 2. CONFIGURAÇÕES DO USUÁRIO
# Tudo que você precisa alterar para adaptar o bot ao seu perfil está aqui.
# ════════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────────────
# A. BUSCAS — o que procurar em cada plataforma
# ──────────────────────────────────────────────────────────────────────────────
# "nome" é o label exibido no alerta do Telegram.

# Gupy: busca por cargo (jobName) e modalidade (workplaceTypes).
# workplaceTypes válidos: 'remote' | 'hybrid' | 'on-site'
FILTROS_GUPY = [
    {"nome": "FLUTTER · REMOTO", "params": {'workplaceTypes': 'remote', 'jobName': 'flutter', 'limit': 15}},
    # {"nome": "MOBILE · REMOTO",  "params": {'workplaceTypes': 'remote', 'jobName': 'mobile',  'limit': 10}},
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

# LinkedIn — publicações das últimas 24h (busca via Brave Search, requer BRAVE_API_KEY).
# "termo" é pesquisado em site:linkedin.com/posts. Cada filtro × página = 1 consulta
# (até 20 posts); o plano grátis da Brave dá ~1.000 consultas/mês (US$ 5 de crédito).
# "OR" (maiúsculo) é operador da Brave: acha posts com "remoto" ou com "remota".
FILTROS_POSTS_LINKEDIN = [
    {"nome": "FLUTTER · VAGA · REMOTO", "termo": "flutter vaga remoto OR remota"},
]
PAGINAS_POSTS_LINKEDIN = 1

# Web toda — páginas das últimas 24h em qualquer site (blogs, portais de vagas,
# sites de empresas...), também via Brave Search (código em web_search.py).
# Usa TERMOS_OBRIGATORIOS_POSTS e EMPRESAS_IGNORADAS (comparado com o domínio).
# Cada filtro × página também gasta 1 consulta da cota da Brave.
FILTROS_WEB = [
    {"nome": "FLUTTER · VAGA · REMOTO", "termo": "flutter vaga remoto OR remota"},
]
PAGINAS_WEB = 1
# Domínios fora da busca na web (subdomínios inclusos). O LinkedIn já tem
# busca própria (vagas e publicações), então ficaria repetido.
SITES_EXCLUIDOS_WEB = ["linkedin.com"]

# A publicação só é enviada se o texto tiver ao menos um termo de CADA grupo
# (palavra inteira, case-insensitive): flutter E vaga E (remoto OU remota).
TERMOS_OBRIGATORIOS_POSTS = [
    ["flutter"],
    ["vaga"],
    ["remoto", "remota"],
]

# Inhire: busca por termo no título + filtro de localização.
# local_filtro válidos: 'remoto' | 'presencial'
# As empresas ficam na tabela inhire_tenants do banco (ver descoberta abaixo).
FILTROS_INHIRE = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter", "local_filtro": "remoto"},
    # {"nome": "MOBILE · REMOTO",  "termo": "mobile",  "local_filtro": "remoto"},
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

# Solides: busca pela página pública vagas.solides.com.br/vagas/<modalidade>/<termo>.
# 'caminho' = "<modalidade>/<termo>". A modalidade no caminho já filtra as vagas
# (ex: "remoto/flutter" só traz vagas remotas). Atenção: "todos/<termo>" ignora o termo.
FILTROS_SOLIDES = [
    {"nome": "FLUTTER · REMOTO", "caminho": "remoto/flutter"},
    # {"nome": "MOBILE · REMOTO",  "caminho": "remoto/mobile"},
]

# ──────────────────────────────────────────────────────────────────────────────
# B. PERFIL — palavras-chave e empresas que bloqueiam a vaga
# ──────────────────────────────────────────────────────────────────────────────

# Período máximo de publicação aceito. Vagas mais antigas são ignoradas.
# Dica: na primeira execução, aumente os valores para preencher o histórico
# (ex: 30 dias), depois retorne ao padrão.
DIAS_BUSCA_GUPY    = 10   # Gupy    → padrão: 10 dias
DIAS_BUSCA_SOLIDES = 20  # Solides → padrão: 20 dias

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

TRADUCAO_MODELO = {
    "on-site": "Presencial",
    "hybrid":  "Híbrido",
    "remote":  "Remoto",
}

TRADUCAO_TIPO_VAGA = {
    "vacancy_type_effective":   "Efetivo",
    "vacancy_type_apprentice":  "Jovem Aprendiz",
    "vacancy_type_internship":  "Estágio",
    "vacancy_type_temporary":   "Temporário",
    "vacancy_type_freelancer":  "Freelancer",
}

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
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=10)
        if r.status_code != 200:
            print(f"⚠️  Telegram recusou: {r.text}")
    except Exception as e:
        print(f"❌ Erro Telegram: {e}")

def registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, fonte):
    chave = _chave_sessao(titulo, empresa)
    if chave in _enviados_sessao:
        print(f"   🔁 Duplicata (sessão): {titulo[:50]}")
        return
    _enviados_sessao.add(chave)
    cursor.execute('INSERT OR IGNORE INTO vagas_enviadas VALUES (?, ?, ?)', (link, data_f, titulo))
    conn.commit()
    enviar_telegram(mensagem)
    print(f"   ✅ {titulo[:50]}...")
    time.sleep(2)

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

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept':     'application/json, text/plain, */*',
        'Origin':     'https://portal.gupy.io',
    }
    url_api = "https://employability-portal.gupy.io/api/v1/jobs"

    for filtro in FILTROS_GUPY:
        print(f"\n   🔎 {filtro['nome']}...")
        vagas_velhas  = 0
        LIMITE_VELHAS = 20

        for pagina in range(1, 36):
            params = filtro['params'].copy()
            params['offset'] = (pagina - 1) * 10

            try:
                resp = requests.get(url_api, headers=headers, params=params, timeout=15)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    break

                dados = resp.json().get('data', [])
                if not dados:
                    print("   🔚 Sem mais vagas.")
                    break

                for vaga in dados:
                    link   = vaga.get('jobUrl', '')
                    if not link:
                        continue

                    titulo  = vaga.get('name', 'Título Indisponível')
                    empresa = vaga.get('careerPageName', 'Empresa não informada')
                    local   = "Qualquer lugar (Remoto)" if 'REMOTO' in filtro['nome'] else f"{vaga.get('city', 'Não informado')} - {vaga.get('state', 'Não informado')}"
                    modelo  = TRADUCAO_MODELO.get(vaga.get('workplaceType', ''), "Não informado")
                    tipo    = TRADUCAO_TIPO_VAGA.get(vaga.get('type', ''), "Outros")
                    pcd     = "Sim" if vaga.get('disabilities') else "Não informado"

                    pais    = vaga.get('country', '')
                    if pais and pais.lower() not in ['brasil', 'brazil', 'br']:
                        continue

                    data_iso = vaga.get('publishedDate', '')
                    try:
                        data_utc = datetime.strptime(data_iso.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                        data_brt = data_utc - timedelta(hours=3)
                        data_f   = data_brt.strftime("%d/%m/%Y")
                        hora_f   = data_brt.strftime("%H:%M")
                        if datetime.now() - data_brt > timedelta(days=DIAS_BUSCA_GUPY):
                            print(f"   📅 Vaga antiga ({data_f}). Encerrando busca.")
                            vagas_velhas = LIMITE_VELHAS
                            break
                    except Exception:
                        data_f, hora_f = "Sem data", "--:--"

                    bloqueada, motivo = filtros_basicos(titulo, empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue

                    if ja_enviada(cursor, link):
                        vagas_velhas += 1
                        if vagas_velhas >= LIMITE_VELHAS:
                            break
                        continue

                    vagas_velhas = 0

                    mensagem = (
                        f"🟣 <b>GUPY — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {empresa}\n"
                        f"📍 <b>Local:</b> {local}\n"
                        f"💻 <b>Modelo:</b> {modelo}\n"
                        f"📄 <b>Tipo:</b> {tipo}\n"
                        f"♿ <b>PCD:</b> {pcd}\n"
                        f"📅 <b>Data:</b> {data_f} às {hora_f}\n\n"
                        f"🔗 <a href='{link}'>Aplicar na Gupy</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "GUPY")

                if vagas_velhas >= LIMITE_VELHAS:
                    print("   🛑 Encerrando paginação.")
                    break

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                break

# --- 5. PROGRAMATHOR ---

def buscar_vagas_programathor(conn, cursor):
    if not BS4_DISPONIVEL:
        print("\n⚠️  ProgramaThor desativado: instale beautifulsoup4")
        return

    print("\n🟤 PROGRAMATHOR — iniciando varredura...")

    headers = {
        'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9',
    }

    for filtro in FILTROS_PROGRAMATHOR:
        print(f"\n   🔎 {filtro['nome']}...")

        # Monta URL base: /jobs-{termo}/{local_filtro} ou /jobs-{termo} se sem local
        termo        = filtro["termo"].lower()
        local_filtro = filtro.get("local_filtro", "")
        base_url     = f"https://programathor.com.br/jobs-{termo}/{local_filtro}" if local_filtro else f"https://programathor.com.br/jobs-{termo}"

        for pagina in range(1, 6):
            params = {} if pagina == 1 else {"page": pagina}

            try:
                resp = requests.get(base_url, params=params, headers=headers, timeout=15)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    break

                soup  = BeautifulSoup(resp.text, 'html.parser')
                cards = soup.find_all('div', class_='cell-list')

                if not cards:
                    print("   🔚 Sem mais vagas.")
                    break

                novos_na_pagina = 0

                for card in cards:
                    link_el = card.find('a', href=lambda h: h and '/jobs/' in h)
                    if not link_el:
                        continue

                    link = "https://programathor.com.br" + link_el['href']

                    titulo_el = card.find('h3')
                    titulo_raw = titulo_el.get_text(strip=True) if titulo_el else ""
                    if 'Vencida' in titulo_raw or 'vencida' in titulo_raw:
                        continue
                    titulo = titulo_raw.replace('NOVA', '').strip() or "Título Indisponível"

                    spans   = card.select('.cell-list-content-icon span')
                    empresa = spans[0].get_text(strip=True) if len(spans) > 0 else "Empresa não informada"
                    local   = spans[1].get_text(strip=True) if len(spans) > 1 else ""
                    salario = spans[3].get_text(strip=True) if len(spans) > 3 else ""
                    nivel   = spans[4].get_text(strip=True) if len(spans) > 4 else ""
                    tipo    = spans[5].get_text(strip=True) if len(spans) > 5 else ""
                    tags    = [t.get_text(strip=True) for t in card.select('span.tag-list')]

                    # Filtros básicos (termo obrigatório no título ou empresa ignorada)
                    bloqueada, motivo = filtros_basicos(titulo, empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue

                    if ja_enviada(cursor, link):
                        continue

                    novos_na_pagina += 1

                    tags_str  = ", ".join(tags[:6]) if tags else ""

                    mensagem = (
                        f"🟤 <b>PROGRAMATHOR — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {empresa}\n"
                        f"📍 <b>Local:</b> {local}\n"
                        f"📄 <b>Nível:</b> {nivel}"
                        + (f" · {tipo}" if tipo else "") + "\n"
                        + (f"💰 <b>Salário:</b> {salario}\n" if salario else "")
                        + (f"🛠️  <b>Stack:</b> <i>{tags_str}</i>\n" if tags_str else "")
                        + "\n"
                        f"🔗 <a href='{link}'>Aplicar no ProgramaThor</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, datetime.now().strftime("%d/%m/%Y"), mensagem, "PROGRAMATHOR")

                if novos_na_pagina == 0:
                    break

                time.sleep(1)

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                break

# --- 6. LINKEDIN ---

def buscar_vagas_linkedin(conn, cursor):
    if not BS4_DISPONIVEL:
        print("\n⚠️  LinkedIn desativado: instale beautifulsoup4")
        return

    print("\n🔷 LINKEDIN — iniciando varredura...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    for filtro in FILTROS_LINKEDIN:
        print(f"\n   🔎 {filtro['nome']}...")
        for pagina in range(PAGINAS_LINKEDIN):
            params = filtro["params"].copy()
            params["start"] = pagina * 10
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
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
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {empresa}\n"
                        f"📅 <b>Data:</b> {data_f}\n\n"
                        f"🔗 <a href='{link}'>Aplicar no LinkedIn</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "LINKEDIN")

                time.sleep(1)

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                break

# --- 6b. LINKEDIN (PUBLICAÇÕES) ---

def buscar_posts_linkedin(conn, cursor):
    if not BRAVE_API_KEY:
        print("\n⚠️  Publicações do LinkedIn desativadas: defina BRAVE_API_KEY no .env")
        return

    print("\n📝 LINKEDIN PUBLICAÇÕES — iniciando varredura...")

    for filtro in FILTROS_POSTS_LINKEDIN:
        print(f"\n   🔎 {filtro['nome']}...")
        try:
            posts = linkedin_posts.search_posts(BRAVE_API_KEY, filtro["termo"], PAGINAS_POSTS_LINKEDIN)
        except Exception as e:
            print(f"   ⚠️  Erro: {e}")
            continue

        for post in posts:
            texto, autor, link = post["text"], post["author"], post["link"]

            if not post_relevante(texto):
                print(f"   🚫 Sem flutter + vaga + remoto/remota: {texto[:55]}")
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
                f"👤 <b>Autor:</b> {html.escape(autor)}\n"
                f"📅 <b>Data:</b> {post['date']}\n\n"
                f"💬 {html.escape(resumo)}\n\n"
                f"🔗 <a href='{html.escape(link)}'>Ver publicação</a>"
            )
            registrar_e_enviar(conn, cursor, link, texto, autor, post["date"], mensagem, "LINKEDIN_POST")

# --- 7. INHIRE ---

def buscar_vagas_inhire(conn, cursor):
    print("\n🟣 INHIRE — iniciando varredura...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    url_base = "https://api.inhire.app/job-posts/public/pages"

    empresas = inhire_discovery.discover(
        conn, cursor,
        queries=TERMOS_DESCOBERTA_INHIRE,
        brave_api_key=BRAVE_API_KEY if USAR_BRAVE_DESCOBERTA_INHIRE else None,
        every=timedelta(days=DIAS_DESCOBERTA_INHIRE),
    )
    print(f"   📋 {len(empresas)} empresas para verificar")

    for empresa_slug in empresas:
        print(f"\n   🏢 {empresa_slug.upper()}...")
        headers['X-Tenant'] = empresa_slug
        
        try:
            resp = requests.get(url_base, headers=headers, timeout=15)
            if resp.status_code == 404:
                # Subdomínio que não é (ou deixou de ser) empresa da Inhire
                print("   🗑️  Empresa não existe na Inhire — removida da lista")
                inhire_discovery.remove_tenant(conn, cursor, empresa_slug)
                continue
            if resp.status_code != 200:
                print(f"   🛑 HTTP {resp.status_code}")
                continue
                
            dados = resp.json()
            jobs = dados.get('jobsPage', [])
            nome_empresa = dados.get('tenantName', empresa_slug.capitalize())
            
            if not jobs:
                print("   🔚 Nenhuma vaga encontrada.")
                continue

            for filtro in FILTROS_INHIRE:
                # Vamos buscar vagas para cada filtro
                for job in jobs:
                    if job.get('status') != 'published':
                        continue
                        
                    titulo = job.get('displayName', 'Título Indisponível')
                    titulo_lower = titulo.lower()
                    
                    if filtro['termo'] not in titulo_lower:
                        continue
                        
                    modelo_api = job.get('workplaceType', '').lower()
                    modelo = TRADUCAO_MODELO.get(modelo_api, "Não informado")
                    
                    if filtro['local_filtro'] == 'remoto' and modelo_api != 'remote':
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
                        
                    local = job.get('location', 'Não informado')
                    data_f = datetime.now().strftime("%d/%m/%Y")
                    
                    mensagem = (
                        f"🟣 <b>INHIRE — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {nome_empresa}\n"
                        f"📍 <b>Local:</b> {local}\n"
                        f"💻 <b>Modelo:</b> {modelo}\n"
                        f"📅 <b>Data (Descoberta):</b> {data_f}\n\n"
                        f"🔗 <a href='{link}'>Aplicar na Inhire</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, nome_empresa, data_f, mensagem, "INHIRE")
                    
        except Exception as e:
            print(f"   ⚠️  Erro ao buscar {empresa_slug}: {e}")

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
        raise RuntimeError(f"HTTP {resp.status_code}")
    flight = _solides_flight(resp.text)
    inicio = flight.find('"initialData":')
    if inicio == -1:
        raise RuntimeError("initialData não encontrado na página (layout mudou?)")
    dados, _ = json.JSONDecoder().raw_decode(flight, inicio + len('"initialData":'))
    return dados.get('data', []), dados.get('totalPages', 1), _solides_textos(flight)

def buscar_vagas_solides(conn, cursor):
    print("\n🟢 SOLIDES — iniciando varredura...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
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
                        f"💼 <b>Vaga:</b> {html.escape(titulo)}\n"
                        f"🏢 <b>Empresa:</b> {html.escape(empresa)}\n"
                        f"📍 <b>Local:</b> {html.escape(local)}\n"
                        f"💻 <b>Modelo:</b> {modelo}\n"
                        f"📅 <b>Data:</b> {data_f}\n\n"
                        f"🔗 <a href='{link}'>Aplicar na Solides</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "SOLIDES")

                if pagina >= total_pages:
                    break

            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                break

# --- MAIN ---

def main():
    if not TOKEN or not CHAT_ID:
        print("❌ ERRO: Token do Telegram ou Chat ID não encontrados no arquivo .env!")
        return

    conn, cursor = iniciar_banco()

    buscar_vagas_gupy(conn, cursor)
    buscar_vagas_programathor(conn, cursor)
    buscar_vagas_linkedin(conn, cursor)
    buscar_posts_linkedin(conn, cursor)
    buscar_vagas_inhire(conn, cursor)
    buscar_vagas_solides(conn, cursor)
    # Last: dedicated sources send richer messages for the same links
    web_search.search_jobs(
        conn, cursor,
        api_key=BRAVE_API_KEY,
        filters=FILTROS_WEB,
        pages=PAGINAS_WEB,
        excluded_sites=SITES_EXCLUIDOS_WEB,
        required_terms=TERMOS_OBRIGATORIOS_POSTS,
        ignored_companies=EMPRESAS_IGNORADAS,
        send=registrar_e_enviar,
    )

    conn.close()
    print("\n✅ Varredura completa de todas as fontes!")

if __name__ == '__main__':
    main()
