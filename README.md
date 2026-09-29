# 🤖 Multi-Source Job Tracker: Automação de Vagas para Devs no Telegram

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![Sources](https://img.shields.io/badge/Fontes-Gupy%20%7C%20LinkedIn%20%7C%20ProgramaThor%20%7C%20Solides%20%7C%20InHire-orange)]()
[![Telegram](https://img.shields.io/badge/Alertas-Telegram-2CA5E0)]()
[![GitHub Actions](https://img.shields.io/badge/Automação-GitHub%20Actions-181717?logo=github)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Bot que monitora vagas de emprego em múltiplas plataformas e envia alertas formatados direto no Telegram, rodando automaticamente via GitHub Actions — sem precisar de servidor ou deixar o PC ligado.

---

## 📋 Índice

- [Como funciona](#-como-funciona)
- [Fontes monitoradas](#-fontes-monitoradas)
- [Pré-requisitos](#-pré-requisitos)
- [Instalação e configuração](#-instalação-e-configuração)
- [Guia de configuração detalhado](#-guia-de-configuração-detalhado)
- [Configurando o GitHub Actions](#-configurando-o-github-actions)
- [Exemplo de alerta](#-exemplo-de-alerta)
- [Avisos de erro](#avisos-de-erro)
- [Créditos](#-créditos)

---

## 🚀 Como funciona

```
GitHub Actions (agendado automaticamente)
    ↓
Varre Gupy + LinkedIn + ProgramaThor + Solides + InHire
    ↓
Aplica filtros de perfil (termo obrigatório no título, empresas ignoradas)
    ↓
Envia alertas formatados no Telegram
    ↓
Salva histórico no banco SQLite (evita reenvio de vagas já vistas)
```

---

## 🔍 Fontes monitoradas

| Fonte | Tipo de acesso |
|---|---|
| 🟣 **Gupy** | API JSON |
| 🔷 **LinkedIn** | API Guest (sem login) |
| 🟤 **ProgramaThor** | Web Scraping |
| 🟢 **Solides** | Página pública (dados Next.js) |
| 🟣 **InHire** | API JSON |


---

## 📦 Pré-requisitos

- Python 3.8+
- Conta no GitHub (para rodar via GitHub Actions)
- Um bot do Telegram criado via [@BotFather](https://t.me/botfather)
- O ID do seu grupo ou canal do Telegram

---

## ⚙️ Instalação e configuração

### 1. Faça o fork e clone o repositório

```bash
git clone https://github.com/SEU_USUARIO/telegram-vagas-gupy-bot.git
cd telegram-vagas-gupy-bot
pip install -r requirements.txt
```

### 2. Configure as credenciais do Telegram

O repositório inclui um arquivo [`.env.example`](.env.example) com a estrutura necessária. Copie-o e preencha com suas credenciais:

```bash
cp .env.example .env
```

Abra o `.env` e preencha os valores:

```env
TELEGRAM_TOKEN=seu_token_aqui
CHAT_ID_GRUPO=seu_chat_id_aqui
BRAVE_API_KEY=sua_chave_brave   # opcional: publicações do LinkedIn e busca na web
```

> **Como obter a `BRAVE_API_KEY` (opcional):** crie uma conta em [Brave Search API](https://brave.com/search/api/) e gere uma chave no plano grátis (US$ 5 de crédito por mês, cerca de 1.000 consultas; ative o "Monthly Usage Limit" em US$ 5 para nunca ser cobrado). Sem ela, o bot só pula a busca de publicações do LinkedIn e a busca na web.

> **Como obter o `TELEGRAM_TOKEN`:** crie um bot no Telegram via [@BotFather](https://t.me/botfather) e copie o token gerado.
>
> **Como obter o `CHAT_ID_GRUPO`:** adicione o bot ao seu grupo/canal e acesse:
> `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
> O ID do chat aparece no campo `"chat"` → `"id"`.

> ⚠️ **Nunca commite o `.env` no repositório.** Ele já está no `.gitignore`. Use apenas o `.env.example` (sem valores reais) para versionar a estrutura.

### 3. Ajuste as configurações no `main.py`

**Toda a configuração está centralizada no topo do arquivo**, na seção `2. CONFIGURAÇÕES DO USUÁRIO`. Você não precisará mexer em nenhuma outra parte do código.

### 4. Execute localmente para testar

```bash
python main.py
```

---

## 🛠️ Guia de configuração detalhado

Abra o `main.py`. Logo após os imports, você encontrará a seção de configuração dividida em duas partes: **A. Buscas** e **B. Perfil**.

---

### A. Buscas — o que procurar em cada plataforma

Cada constante `FILTROS_*` é uma lista de buscas que serão executadas naquela plataforma. Você pode ter quantas buscas quiser por plataforma.

O campo `"nome"` é apenas o label que aparece no cabeçalho do alerta no Telegram.

#### `FILTROS_GUPY`

```python
FILTROS_GUPY = [
    {"nome": "FLUTTER · REMOTO", "params": {'workplaceTypes': 'remote', 'jobName': 'flutter', 'limit': 10}},
    {"nome": "MOBILE · REMOTO",  "params": {'workplaceTypes': 'remote', 'jobName': 'mobile',  'limit': 10}},
]
```

| Campo | Descrição | Valores válidos |
|---|---|---|
| `jobName` | Termo de busca (cargo/tecnologia) | qualquer string |
| `workplaceTypes` | Modalidade de trabalho | `'remote'` · `'hybrid'` · `'on-site'` |
| `limit` | Vagas por página | inteiro (recomendado: 10) |

---

#### `FILTROS_PROGRAMATHOR`

```python
FILTROS_PROGRAMATHOR = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter", "local_filtro": "remoto"},
    {"nome": "MOBILE · REMOTO",  "termo": "mobile",  "local_filtro": "remoto"},
]
```

| Campo | Descrição | Valores válidos |
|---|---|---|
| `termo` | Termo de busca | qualquer string |
| `local_filtro` | Filtro de localização | `'remoto'` · `'sp'` |

---

#### `FILTROS_LINKEDIN`

```python
FILTROS_LINKEDIN = [
    {"nome": "FLUTTER · REMOTO", "params": {"keywords": "flutter", "location": "Brazil", "f_WT": "2", "f_TPR": "r259200", "start": 0}},
    {"nome": "MOBILE · REMOTO",  "params": {"keywords": "mobile",  "location": "Brazil", "f_WT": "2", "f_TPR": "r259200", "start": 0}},
]
```

| Campo | Descrição | Valores válidos |
|---|---|---|
| `keywords` | Termo de busca | qualquer string |
| `location` | País ou cidade | ex: `"Brazil"`, `"Portugal"` |
| `f_WT` | Modalidade | `"2"` = remoto · `"1"` = presencial · `"3"` = híbrido |
| `f_TPR` | Período de publicação | `"r86400"` = 24h · `"r259200"` = 3 dias · `"r604800"` = 7 dias |

---

#### `FILTROS_POSTS_LINKEDIN`

Busca **publicações** (posts) do LinkedIn das últimas 24h, como "estamos contratando dev Flutter". A busca interna de publicações do LinkedIn exige login, então o bot pesquisa `site:linkedin.com/posts` na [Brave Search API](https://brave.com/search/api/). Requer `BRAVE_API_KEY`. O código fica em `linkedin_posts.py`.

```python
FILTROS_POSTS_LINKEDIN = [
    {"nome": "FLUTTER · VAGA · REMOTO", "termo": "flutter vaga remoto OR remota"},
]
PAGINAS_POSTS_LINKEDIN = 1

TERMOS_OBRIGATORIOS_POSTS = [
    ["flutter"],
    ["vaga", "vagas"],
    ["remoto", "remota"],
]
```

| Campo | Descrição |
|---|---|
| `termo` | Termo pesquisado nas publicações |
| `PAGINAS_POSTS_LINKEDIN` | Páginas por termo (cada filtro × página = 1 consulta, até 20 posts) |
| `TERMOS_OBRIGATORIOS_POSTS` | Grupos de palavras: o texto precisa ter ao menos uma palavra de **cada** grupo |

Com a configuração padrão, a publicação só é enviada se tiver "flutter" **e** ("vaga" **ou** "vagas") **e** ("remoto" **ou** "remota"), como palavras inteiras. O autor é comparado com `EMPRESAS_IGNORADAS`.

---

#### `FILTROS_WEB`

Busca vagas publicadas nos últimos `DIAS_WEB` dias em **qualquer site** (blogs, portais de vagas, sites de empresas...), sem ficar preso às fontes acima. Usa a mesma Brave Search API e a regra `TERMOS_OBRIGATORIOS_WEB`: "flutter" **e** ("remoto", "remota" **ou** "home office", com ou sem hífen). Nem o termo pesquisado nem a regra exigem "vaga", porque muitas páginas de vaga só trazem o cargo. O código fica em `web_search.py`; o `main.py` só passa as configurações. O domínio do site aparece como autor e também é comparado com `EMPRESAS_IGNORADAS` (ex.: adicione `"indeed"` para ignorar o Indeed).

```python
FILTROS_WEB = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter remoto"},
]
PAGINAS_WEB = 1
SITES_EXCLUIDOS_WEB = ["linkedin.com"]
TERMOS_OBRIGATORIOS_WEB = [
    ["flutter"],
    ["remoto", "remota", "home office", "home-office", "homeoffice"],
]
TERMOS_BLOQUEADOS_WEB = ["híbrido", "hibrido", "híbrida", "hibrida", "presencial"]
DIAS_WEB = 7
```

- `TERMOS_BLOQUEADOS_WEB`: a página é descartada se tiver qualquer um desses termos. Um texto como "100% remoto, sem presencial" também cai fora.
- `DIAS_WEB`: até quantos dias atrás buscar. Em 24h quase nada sobre Flutter aparece na web, então o padrão é a última semana. Cada vaga é enviada uma vez só, então não há repetição. A Brave só filtra por dia, semana, mês ou ano.

`SITES_EXCLUIDOS_WEB` tira domínios da busca (com `-site:` na consulta da Brave), incluindo subdomínios. O LinkedIn fica de fora porque já tem busca própria.

Cada filtro × página também gasta 1 consulta da cota da Brave, somada às publicações do LinkedIn.

---

#### `FILTROS_INHIRE`

A InHire não publica a lista de empresas: a busca de vagas precisa do subdomínio de cada uma. Por isso as empresas ficam na tabela `inhire_tenants` do banco, preenchida pela **descoberta automática** (ver abaixo).

```python
FILTROS_INHIRE = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter"},
    {"nome": "MOBILE · REMOTO",  "termo": "mobile"},
]
```

| Campo | Descrição | Valores válidos |
|---|---|---|
| `termo` | Termo buscado no título da vaga (palavra inteira) | qualquer string |

Só entram vagas remotas da InHire.

> Para incluir uma empresa na mão: `sqlite3 vagas_gupy.db "INSERT OR IGNORE INTO inhire_tenants (slug, found_at) VALUES ('empresa', datetime('now'))"` (subdomínio de `empresa.inhire.app`).

#### Descoberta automática de empresas da InHire

O bot pesquisa páginas `*.inhire.app` no Yahoo (grátis) e, opcionalmente, na Brave (`USAR_BRAVE_DESCOBERTA_INHIRE = True`, requer `BRAVE_API_KEY`). Cada subdomínio achado fica salvo na tabela `inhire_tenants` do banco, então a lista cresce a cada rodada. Subdomínios que a InHire não reconhece são removidos sozinhos.

```python
TERMOS_DESCOBERTA_INHIRE = [
    "site:inhire.app vagas",
    "site:inhire.app flutter",
    # Mais termos = mais empresas encontradas
]
DIAS_DESCOBERTA_INHIRE = 7  # intervalo entre descobertas
USAR_BRAVE_DESCOBERTA_INHIRE = False  # Brave: mais empresas, mas gasta cota
```

A busca de vagas continua em toda execução; só a descoberta respeita o intervalo. Com a Brave ativa, cada termo gasta até 10 consultas da cota por descoberta.

Se a descoberta não achar nenhuma empresa (site de busca bloqueou), o bot tenta de novo no dia seguinte, e não em toda execução.

**Aviso de erros no Telegram:** problemas na varredura da InHire entram no aviso de erros — busca que falhou, cota da Brave esgotada, empresas que não responderam (cada uma é consultada duas vezes antes de contar como erro). `LIMITE_ITENS_NO_AVISO` limita quantas empresas aparecem nele (veja [Avisos de erro](#avisos-de-erro)). Uma empresa só é apagada do banco quando a InHire responde que ela não existe, e no máximo `MAX_REMOCOES_INHIRE` por execução: acima disso, é mais provável uma mudança na InHire, então nada é apagado e chega um aviso.

---

#### `FILTROS_SOLIDES`

```python
FILTROS_SOLIDES = [
    {"nome": "FLUTTER · REMOTO", "params": {'title': 'flutter', 'take': 14}},
    {"nome": "MOBILE · REMOTO",  "params": {'title': 'mobile',  'take': 14}},
]
```

| Campo | Descrição | Valores válidos |
|---|---|---|
| `title` | Termo de busca no título | qualquer string |
| `take` | Vagas por página | inteiro (máx. recomendado: 14) |

---

### B. Perfil — filtros que bloqueiam vagas

#### `TERMO_OBRIGATORIO_TITULO`

Palavra que precisa aparecer no **título** da vaga, em todas as fontes. Vaga sem ela é descartada. A comparação é por palavra inteira e ignora maiúsculas: `"flutter"` aceita "Desenvolvedor FLUTTER Pleno", mas não "Flutterwave Analyst".

```python
TERMO_OBRIGATORIO_TITULO = "flutter"
```

---

#### `EMPRESAS_IGNORADAS`

Lista de empresas cujas vagas serão ignoradas em **todas as fontes**.

```python
EMPRESAS_IGNORADAS = [
    "hired",          # bloqueia "Hired", "Hired Feed", etc.
    "Jobgether",
    "Quik Hire Staffing",
    # Adicione consultorias ou plataformas que você não quer ver
]
```

> A comparação é **parcial e case-insensitive**: `"hired"` também bloqueia `"Hired Feed"`.

---

## 🤖 Configurando o GitHub Actions

O bot roda via GitHub Actions. O agendamento principal é feito por um Cloudflare Worker, e o workflow tem um agendamento de reserva com poucos horários.

### 1. Adicione os secrets no repositório

Vá em **Settings → Secrets and variables → Actions → New repository secret** e adicione:

| Secret | Valor |
|---|---|
| `TELEGRAM_TOKEN` | Token gerado pelo [@BotFather](https://t.me/botfather) |
| `CHAT_ID_GRUPO` | ID do seu grupo ou canal do Telegram |
| `BRAVE_API_KEY` | (Opcional) Chave da Brave Search API, para as publicações do LinkedIn e a busca na web |

### 2. Ative o workflow

Após o fork, vá em **Actions** no seu repositório e clique em **"I understand my workflows, go ahead and enable them"** se aparecer o aviso de workflows desabilitados.

### 3. Horários de execução

O agendamento nativo do GitHub Actions atrasa ou pula execuções. Por isso, o horário principal fica num Cloudflare Worker (grátis), que dispara o workflow no minuto certo:
- **Segunda a sexta:** a cada 30 min, das 8h às 20h30 (BRT)
- **Sábado e domingo:** a cada 1h, das 10h às 18h (BRT)

Como **reserva**, o próprio workflow roda às 9h23, 13h23 e 17h23 (BRT), todo dia. Se o Worker parar (por exemplo, com o token vencido), o bot continua rodando, só que menos vezes. Execuções extras não duplicam vagas, porque o banco guarda o que já foi enviado.

O Worker fica no repositório [workana-telegram-bot](https://github.com/leandrorochaadm/workana-telegram-bot) (pasta `scheduler/`) e dispara os dois bots. Para alterar os horários principais, edite a regra `gupy` em `scheduler/src/index.js`, escrita em horário de Brasília. Para alterar a reserva, edite `.github/workflows/vagas.yml` (cron em UTC; BRT = UTC-3).

Em um fork, sem o Worker, o bot roda só nos horários de reserva. Para ter o agendamento preciso, publique um Worker como o do workana-telegram-bot apontando para o seu repositório, com um token (fine-grained) com permissão **Actions: Read and write**.

---

## 📸 Exemplo de alerta

```
🟣 GUPY — FLUTTER · REMOTO

💼 Vaga: Desenvolvedor Mobile Flutter Sênior
🏢 Empresa: Empresa X
📍 Local: Qualquer lugar (Remoto)
💻 Modelo: Remoto
📄 Tipo: Efetivo
♿ PCD: Não informado
📅 Data: 12/06/2026 às 09:15

🔗 Aplicar na Gupy
```

### Avisos de erro

Quando algo dá errado, o bot avisa no próprio grupo. Os problemas de todas as fontes vão **numa mensagem só**, no fim da execução, e no máximo **uma a cada 20 minutos** (`INTERVALO_AVISOS_MIN`). O que acontecer dentro do intervalo fica guardado no banco e vai junto no próximo aviso. Problema repetido aparece uma vez, com a contagem, como "(3x)".

O que entra no aviso:

- **Busca recusada ou sem resposta** em qualquer fonte (Gupy, ProgramaThor, LinkedIn, InHire, Solides, web). Ex.: página da Solides que mudou de formato. Quando o LinkedIn pede uma pausa (excesso de buscas), o bot espera `PAUSA_NOVA_TENTATIVA_LINKEDIN` segundos e tenta de novo antes de avisar.
- **Cota da Brave esgotada** nas publicações do LinkedIn e na busca na web.
- **Erro inesperado**: a fonte para e as outras continuam rodando.
- **Vagas que o Telegram não entregou**: não ficam marcadas como enviadas e voltam na próxima execução. Quando o Telegram pede para esperar (limite de 20 mensagens por minuto num grupo), o bot espera e tenta de novo; entre uma vaga e outra ele já espera `PAUSA_ENTRE_VAGAS` segundos.
- **beautifulsoup4 não instalado**: ProgramaThor e LinkedIn ficam desligados.
- **Banco de vagas que não abre**: nenhuma busca é feita e o aviso sai na hora.

```
⚠️ Problemas na varredura

LINKEDIN
• FLUTTER · REMOTO: o LinkedIn recusou a busca (429). (2x)

SOLIDES
• FLUTTER · REMOTO: a página mudou de formato e o bot não conseguiu ler as vagas.
```

`LIMITE_ITENS_NO_AVISO` limita quantos itens (empresas, vagas) aparecem em cada linha do aviso.

---

## 🙏 Créditos

Projeto originalmente desenvolvido por **[Lucas Nunes](https://github.com/lucasnunestrabalho99-sudo)** — obrigado por tornar o código público e inspirar esta evolução.

---

**Desenvolvido com ☕ por Carlos André Couto**
