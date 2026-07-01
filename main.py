import os
import re
import time
import sqlite3
import requests
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

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
    # {"nome": "MOBILE · REMOTO",  "params": {"keywords": "mobile",  "location": "Brazil", "f_WT": "2", "f_TPR": "r259200", "start": 0}},
]

# Inhire: busca por termo no título + filtro de localização.
# local_filtro válidos: 'remoto' | 'presencial'
# Requer também EMPRESAS_INHIRE abaixo (lista de subdomínios monitorados).
FILTROS_INHIRE = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter", "local_filtro": "remoto"},
    # {"nome": "MOBILE · REMOTO",  "termo": "mobile",  "local_filtro": "remoto"},
]

# Inhire — empresas monitoradas.
# Adicione apenas o subdomínio: ex. "empresa" para empresa.inhire.app
EMPRESAS_INHIRE = [
    "reclameaqui",
    "mottu",
    "solutis",
    "avenue",
    "dtidigital",
    "frameworkdigital",
    "deal",
    "aarin",
    'kobe',
    'peers',
    'cubos',
    'iconit',
    'exa',
    'programmers',
    'growdev',
    'aarin',
    'wtime',
    'kstack',
    'premiersoft',
    'pilar',
    'ninecon',
    'jump',
    'via',
    'contaazul',
    'semantix',
    'mazzatech',
    'cappta',
    'nstech'
]

# Solides: busca por título.
# 'take' define quantas vagas por página (máx. recomendado: 14).
FILTROS_SOLIDES = [
    {"nome": "FLUTTER · REMOTO", "params": {'title': 'flutter', 'take': 14}},
    # {"nome": "MOBILE · REMOTO",  "params": {'title': 'mobile',  'take': 14}},
]

# ──────────────────────────────────────────────────────────────────────────────
# B. PERFIL — palavras-chave e empresas que bloqueiam a vaga
# ──────────────────────────────────────────────────────────────────────────────

# Período máximo de publicação aceito. Vagas mais antigas são ignoradas.
# Dica: na primeira execução, aumente os valores para preencher o histórico
# (ex: 30 dias), depois retorne ao padrão.
DIAS_BUSCA_GUPY    = 10   # Gupy    → padrão: 10 dias
DIAS_BUSCA_SOLIDES = 20  # Solides → padrão: 20 dias

# Qualquer termo abaixo encontrado no título da vaga a elimina da lista.
# Use letras minúsculas — a busca é case-insensitive.
GAPS_ELIMINATORIOS = [
    "inglês avançado", "inglês fluente", "presencial", "php", "python",
    "node.js", "node", "sqs", "rabbitmq", "product manager",
    "product owner", "vue.js", "java", "vue js", "salesforce", "sales force", "react", "apex",
    "kubernetes", "kafka", "dot net", ".net", "ruby", "go", "ruby on rails", "angular", "product designer",
    "tester", "quality assurance", "analista de testes", "qa", "fullstack", "maker", "CRO", "ux designer","typescript", "adsales", "marketing",
    "bi analyst", "offshore", "automation", "cobol", "mainframe", "head of sales", "ai developer", "editor de vídeo"
]

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

# ──────────────────────────────────────────────────────────────────────────────
# C. MINHA STACK — tecnologias que você domina (usadas para calcular o match)
# ──────────────────────────────────────────────────────────────────────────────
# Níveis de match (quantidade de itens encontrados no texto da vaga):
#   🔴 Baixo  → 0 itens   |  🔵 Padrão → 1 item
#   🟡 Médio  → 2 itens   |  🟢 Alto   → 3 ou mais
#
# Atenção: Gupy, LinkedIn e Inhire analisam apenas o título da vaga.
# ProgramaThor usa título + tags; Solides usa título + descrição completa.
MINHA_STACK = [
    "flutter", "dart", "clean architecture", "bloc", "cubit", "provider", "riverpod",  "mobx",
    "firebase", "crashlytics", "remote config", "firebase performance", "firebase authentication",
    "onesignal", "cloud messaging", "api rest", "apis rest", "rest apis", "restful",  "dio", 
    "flutter_test", "mocktail", "mockito", "tdd", "code coverage", "solid", "design patterns", 
    "cross-platform", "cross platform", "android", "ios",
    "codemagic", "github actions",  "fastlane", "gitflow",
    "sqlite", "isar", "hive", "sharedpreferences", "fluttersecurestorage",
    "tech lead", "agile", "scrum", "kanban",  "code review", "sênior", "pleno", "SN", "PL",
    "devsecops", "micro front end", "testes de widget", "widget tests", "integration tests",
    "offline first", "finops", "ci/cd", "mysql", "banco de dados", "figma", "flutter sdk", "solid", 
    "modularização", "modular", "bloc_test", "clarity", "app store", "play store", "publicação", "push notifications"
]

# Set em memória para evitar duplicatas na mesma execução (mesma vaga, fontes/buscas diferentes)
_enviados_sessao: set = set()

def _chave_sessao(titulo: str, empresa: str) -> str:
    normalizar = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
    return normalizar(titulo)[:60] + "|" + normalizar(empresa)[:30]

def tem_gap_eliminatorio(titulo):
    t = titulo.lower()
    return any(g in t for g in GAPS_ELIMINATORIOS)

def calcular_match(titulo):
    """
    Calcula o nível de compatibilidade da vaga com o perfil do candidato.

    O parâmetro `titulo` pode conter mais do que apenas o título da vaga —
    cada fonte passa textos diferentes:
      - Gupy:        apenas o título da vaga
      - LinkedIn:    apenas o título da vaga
      - Inhire:      apenas o título da vaga
      - ProgramaThor: título + tags de tecnologia exibidas no card
      - Solides:     título + descrição completa da vaga (HTML limpo)

    Para Gupy, LinkedIn e Inhire, tecnologias mencionadas somente na descrição
    NÃO são detectadas, podendo resultar em nível Baixo para vagas relevantes.

    Níveis de match (baseado na contagem de itens de MINHA_STACK encontrados):
      🔴 Baixo  — 0 itens compatíveis
      🔵 Padrão — 1 item compatível
      🟡 Médio  — 2 itens compatíveis
      🟢 Alto   — 3 ou mais itens compatíveis
    """
    t = titulo.lower()
    techs = [s for s in MINHA_STACK if s in t]
    score = len(techs)
    if score >= 3:
        nivel = "🟢 Alto"
    elif score == 2:
        nivel = "🟡 Médio"
    elif score == 1:
        nivel = "🔵 Padrão"
    else:
        nivel = "🔴 Baixo"
    return nivel, techs

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

def registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, fonte, nivel_match):
    chave = _chave_sessao(titulo, empresa)
    if chave in _enviados_sessao:
        print(f"   🔁 Duplicata (sessão): {titulo[:50]}")
        return
    _enviados_sessao.add(chave)
    cursor.execute('INSERT OR IGNORE INTO vagas_enviadas VALUES (?, ?, ?)', (link, data_f, titulo))
    conn.commit()
    enviar_telegram(mensagem)
    print(f"   ✅ [{nivel_match}] {titulo[:50]}...")
    time.sleep(2)

def filtros_basicos(titulo, empresa=None):
    """Retorna (bloqueada, motivo) com os filtros de perfil."""
    if tem_gap_eliminatorio(titulo):
        return True, f"🚫 Gap: {titulo[:55]}"
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
                    nivel_match, techs = calcular_match(titulo)
                    techs_str = " · ".join(t.upper() for t in techs[:4]) if techs else "Verificar descrição"

                    mensagem = (
                        f"🟣 <b>GUPY — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {empresa}\n"
                        f"📍 <b>Local:</b> {local}\n"
                        f"💻 <b>Modelo:</b> {modelo}\n"
                        f"📄 <b>Tipo:</b> {tipo}\n"
                        f"♿ <b>PCD:</b> {pcd}\n"
                        f"📅 <b>Data:</b> {data_f} às {hora_f}\n"
                        f"📊 <b>Match:</b> {nivel_match} · <i>{techs_str}</i>\n\n"
                        f"🔗 <a href='{link}'>Aplicar na Gupy</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "GUPY", nivel_match)

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

                    # Filtros básicos (gap no título ou empresa ignorada)
                    bloqueada, motivo = filtros_basicos(titulo, empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue

                    # Gaps nos tags de tecnologia
                    if any(tem_gap_eliminatorio(t) for t in tags):
                        print(f"   🚫 Gap na tag: {titulo[:55]}")
                        continue

                    if ja_enviada(cursor, link):
                        continue

                    novos_na_pagina += 1

                    # Match scoring usa título + stack explícita do card
                    texto_match = titulo + " " + " ".join(tags)
                    nivel_match, techs = calcular_match(texto_match)
                    techs_str = " · ".join(t.upper() for t in techs[:4]) if techs else (", ".join(tags[:4]) or "Verificar descrição")
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
                        + f"📊 <b>Match:</b> {nivel_match} · <i>{techs_str}</i>\n\n"
                        f"🔗 <a href='{link}'>Aplicar no ProgramaThor</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, datetime.now().strftime("%d/%m/%Y"), mensagem, "PROGRAMATHOR", nivel_match)

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
        try:
            resp = requests.get(url, params=filtro["params"], headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f"   🛑 HTTP {resp.status_code}")
                continue

            soup  = BeautifulSoup(resp.text, 'html.parser')
            cards = soup.find_all('div', class_='base-card')

            if not cards:
                print("   🔚 Nenhuma vaga ou resposta bloqueada.")
                continue

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

                nivel_match, techs = calcular_match(titulo)
                techs_str = " · ".join(t.upper() for t in techs[:4]) if techs else "Verificar descrição"

                mensagem = (
                    f"🔷 <b>LINKEDIN — {filtro['nome']}</b>\n\n"
                    f"💼 <b>Vaga:</b> {titulo}\n"
                    f"🏢 <b>Empresa:</b> {empresa}\n"
                    f"📅 <b>Data:</b> {data_f}\n"
                    f"📊 <b>Match:</b> {nivel_match} · <i>{techs_str}</i>\n\n"
                    f"🔗 <a href='{link}'>Aplicar no LinkedIn</a>"
                )
                registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "LINKEDIN", nivel_match)

        except Exception as e:
            print(f"   ⚠️  Erro: {e}")

# --- 7. INHIRE ---

def buscar_vagas_inhire(conn, cursor):
    print("\n🟣 INHIRE — iniciando varredura...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    url_base = "https://api.inhire.app/job-posts/public/pages"

    for empresa_slug in EMPRESAS_INHIRE:
        print(f"\n   🏢 {empresa_slug.upper()}...")
        headers['X-Tenant'] = empresa_slug
        
        try:
            resp = requests.get(url_base, headers=headers, timeout=15)
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
                    nivel_match, techs = calcular_match(titulo)
                    techs_str = " · ".join(t.upper() for t in techs[:4]) if techs else "Verificar descrição"
                    data_f = datetime.now().strftime("%d/%m/%Y")
                    
                    mensagem = (
                        f"🟣 <b>INHIRE — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {nome_empresa}\n"
                        f"📍 <b>Local:</b> {local}\n"
                        f"💻 <b>Modelo:</b> {modelo}\n"
                        f"📅 <b>Data (Descoberta):</b> {data_f}\n"
                        f"📊 <b>Match:</b> {nivel_match} · <i>{techs_str}</i>\n\n"
                        f"🔗 <a href='{link}'>Aplicar na Inhire</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, nome_empresa, data_f, mensagem, "INHIRE", nivel_match)
                    
        except Exception as e:
            print(f"   ⚠️  Erro ao buscar {empresa_slug}: {e}")

# --- 8. SOLIDES ---

def buscar_vagas_solides(conn, cursor):
    print("\n🟢 SOLIDES — iniciando varredura...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    url_base = "https://apigw.solides.com.br/jobs/v3/portal-vacancies-new"

    for filtro in FILTROS_SOLIDES:
        print(f"\n   🔎 {filtro['nome']}...")
        
        for pagina in range(1, 10):
            params = filtro['params'].copy()
            params['page'] = pagina
            
            try:
                resp = requests.get(url_base, headers=headers, params=params, timeout=15)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    break
                    
                json_data = resp.json()
                if not json_data.get('success'):
                    print("   🛑 Erro na resposta da Solides")
                    break
                    
                dados = json_data.get('data', {})
                vagas = dados.get('data', [])
                total_pages = dados.get('totalPages', 1)
                
                if not vagas:
                    print("   🔚 Sem mais vagas.")
                    break
                    
                for vaga in vagas:
                    titulo = vaga.get('title', 'Título Indisponível')
                    
                    # Verificação de modalidade remota (se o filtro exigir)
                    modelo_api = vaga.get('jobType', '').lower()
                    if 'remoto' in filtro['nome'].lower() and modelo_api != 'remoto':
                        continue
                        
                    link = vaga.get('redirectLink', '')
                    if not link:
                        continue

                    # Correção da URL da vaga Solides
                    match_url = re.search(r'https://([^.]+)\.solides\.jobs/vacancies/(\d+)', link)
                    if match_url:
                        company_slug = match_url.group(1)
                        vacancy_id = match_url.group(2)
                        link = f"https://{company_slug}.vagas.solides.com.br/vaga/{vacancy_id}"

                    empresa = vaga.get('companyName', 'Empresa não informada')
                    bloqueada, motivo = filtros_basicos(titulo, empresa)
                    if bloqueada:
                        print(f"   {motivo}")
                        continue
                        
                    if ja_enviada(cursor, link):
                        continue
                    
                    data_iso = vaga.get('createdAt', '')
                    if data_iso:
                        try:
                            # Tentar extrair "YYYY-MM-DD"
                            data_pub = datetime.strptime(data_iso[:10], "%Y-%m-%d")
                            data_f = data_pub.strftime("%d/%m/%Y")
                            if datetime.now() - data_pub > timedelta(days=DIAS_BUSCA_SOLIDES):
                                print(f"   📅 Vaga antiga ({data_f}). Pulando.")
                                continue
                        except Exception:
                            data_f = data_iso
                    else:
                        data_f = "Não informado"
                        
                    # Tratamento do texto descritivo para enriquecer o match
                    description_raw = vaga.get('description', '')
                    if BS4_DISPONIVEL and description_raw:
                        description_limpa = BeautifulSoup(description_raw, 'html.parser').get_text(separator=' ')
                    else:
                        description_limpa = re.sub(r'<[^>]+>', ' ', description_raw)
                        
                    texto_para_match = f"{titulo} {description_limpa}"
                    nivel_match, techs = calcular_match(texto_para_match)
                    techs_str = " · ".join(t.upper() for t in techs[:4]) if techs else "Verificar descrição"
                    
                    cidade_info = vaga.get('city') or {}
                    estado_info = vaga.get('state') or {}
                    local = f"{cidade_info.get('name', '')} - {estado_info.get('code', '')}".strip(" -")
                    if not local:
                        local = "Brasil"
                        
                    modelo = modelo_api.capitalize() if modelo_api else "Não informado"
                    
                    mensagem = (
                        f"🟢 <b>SOLIDES — {filtro['nome']}</b>\n\n"
                        f"💼 <b>Vaga:</b> {titulo}\n"
                        f"🏢 <b>Empresa:</b> {empresa}\n"
                        f"📍 <b>Local:</b> {local}\n"
                        f"💻 <b>Modelo:</b> {modelo}\n"
                        f"📅 <b>Data:</b> {data_f}\n"
                        f"📊 <b>Match:</b> {nivel_match} · <i>{techs_str}</i>\n\n"
                        f"🔗 <a href='{link}'>Aplicar na Solides</a>"
                    )
                    registrar_e_enviar(conn, cursor, link, titulo, empresa, data_f, mensagem, "SOLIDES", nivel_match)
                    
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
    buscar_vagas_inhire(conn, cursor)
    buscar_vagas_solides(conn, cursor)

    conn.close()
    print("\n✅ Varredura completa de todas as fontes!")

if __name__ == '__main__':
    main()
