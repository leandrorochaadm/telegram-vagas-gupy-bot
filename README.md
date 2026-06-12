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
- [Score de match](#-score-de-match)
- [Exemplo de alerta](#-exemplo-de-alerta)
- [Créditos](#-créditos)

---

## 🚀 Como funciona

```
GitHub Actions (agendado automaticamente)
    ↓
Varre Gupy + LinkedIn + ProgramaThor + Solides + InHire
    ↓
Aplica filtros de perfil (gaps eliminatórios, empresas ignoradas)
    ↓
Calcula score de match com a sua stack técnica
    ↓
Envia alertas formatados no Telegram
    ↓
Salva histórico no banco SQLite (evita reenvio de vagas já vistas)
```

---

## 🔍 Fontes monitoradas

| Fonte | Tipo de acesso | O que é analisado no match |
|---|---|---|
| 🟣 **Gupy** | API JSON | Título da vaga |
| 🔷 **LinkedIn** | API Guest (sem login) | Título da vaga |
| 🟤 **ProgramaThor** | Web Scraping | Título + tags de tecnologia do card |
| 🟢 **Solides** | API JSON | Título + descrição completa |
| 🟣 **InHire** | API JSON | Título da vaga |

> **Atenção:** para Gupy, LinkedIn e InHire, tecnologias mencionadas somente na descrição da vaga **não são detectadas** pelo score de match — apenas o título é analisado.

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
```

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

Abra o `main.py`. Logo após os imports, você encontrará a seção de configuração dividida em três partes: **A. Buscas**, **B. Perfil** e **C. Minha Stack**.

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

#### `FILTROS_INHIRE` e `EMPRESAS_INHIRE`

A InHire funciona de forma diferente: você define as **empresas** que deseja monitorar (pelo subdomínio delas) e os termos de busca.

```python
FILTROS_INHIRE = [
    {"nome": "FLUTTER · REMOTO", "termo": "flutter", "local_filtro": "remoto"},
    {"nome": "MOBILE · REMOTO",  "termo": "mobile",  "local_filtro": "remoto"},
]

# Subdomínio de cada empresa: ex. "reclameaqui" para reclameaqui.inhire.app
EMPRESAS_INHIRE = [
    "reclameaqui",
    "mottu",
    "solutis",
    # Adicione mais empresas aqui
]
```

| Campo | Descrição | Valores válidos |
|---|---|---|
| `termo` | Termo buscado no título da vaga | qualquer string |
| `local_filtro` | Modalidade | `'remoto'` · `'presencial'` |

> Para adicionar uma empresa: acesse o portal de vagas dela na InHire (ex: `empresa.inhire.app`) e adicione o subdomínio `"empresa"` na lista `EMPRESAS_INHIRE`.

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

#### `GAPS_ELIMINATORIOS`

Lista de termos que, se encontrados no **título** da vaga, eliminam ela automaticamente. Use para tecnologias que você não domina ou modalidades indesejadas.

```python
GAPS_ELIMINATORIOS = [
    "inglês avançado", "inglês fluente",  # exigências de idioma
    "presencial",                          # modalidade indesejada
    "php", "python", "node.js", "react",  # tecnologias fora do seu stack
    "fullstack", "qa", "product manager", # cargos fora do seu perfil
    # Adicione o que fizer sentido para o seu caso
]
```

> ⚠️ A busca é **case-insensitive** e **parcial**: `"node"` também bloqueia `"Node.js Developer"`.

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

### C. Minha Stack — tecnologias para o score de match

Liste todas as tecnologias que você domina. O bot usa essa lista para calcular o score de compatibilidade de cada vaga.

```python
MINHA_STACK = [
    "flutter", "dart", "firebase", "clean architecture",
    "bloc", "riverpod", "rest api", "graphql",
    # Adicione suas tecnologias aqui
]
```

#### Como funciona o score

O bot conta quantos itens de `MINHA_STACK` foram encontrados no texto da vaga:

| Itens encontrados | Nível | Indicador |
|---|---|---|
| 0 | Baixo | 🔴 |
| 1 | Padrão | 🔵 |
| 2 | Médio | 🟡 |
| 3 ou mais | Alto | 🟢 |

> **Dica:** quanto mais específica for sua stack, mais preciso será o match. Prefira termos exatos como `"clean architecture"` em vez de só `"architecture"`.

---

## 🤖 Configurando o GitHub Actions

O bot roda automaticamente via GitHub Actions no schedule definido em `.github/workflows/`.

### 1. Adicione os secrets no repositório

Vá em **Settings → Secrets and variables → Actions → New repository secret** e adicione:

| Secret | Valor |
|---|---|
| `TELEGRAM_TOKEN` | Token gerado pelo [@BotFather](https://t.me/botfather) |
| `CHAT_ID_GRUPO` | ID do seu grupo ou canal do Telegram |

### 2. Ative o workflow

Após o fork, vá em **Actions** no seu repositório e clique em **"I understand my workflows, go ahead and enable them"** se aparecer o aviso de workflows desabilitados.

### 3. Horários de execução (padrão)

O schedule padrão configurado no workflow é:
- **Segunda a sexta:** 8h, 10h, 12h, 14h, 16h, 18h e 20h (BRT)
- **Sábado e domingo:** 10h, 14h e 18h (BRT)

Para alterar, edite o arquivo `.github/workflows/*.yml` e ajuste as expressões cron.

---

## 📊 Score de match

O score é calculado contando quantos termos de `MINHA_STACK` aparecem no texto analisado de cada vaga. O texto analisado varia por fonte:

| Fonte | Texto analisado |
|---|---|
| Gupy | Título da vaga |
| LinkedIn | Título da vaga |
| InHire | Título da vaga |
| ProgramaThor | Título + tags de tecnologia do card |
| Solides | Título + descrição completa (HTML limpo) |

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
📊 Match: 🟢 Alto · FLUTTER · DART · CLEAN ARCHITECTURE · FIREBASE

🔗 Aplicar na Gupy
```

---

## 🙏 Créditos

Projeto originalmente desenvolvido por **[Lucas Nunes](https://github.com/lucasnunestrabalho99-sudo)** — obrigado por tornar o código público e inspirar esta evolução.

---

**Desenvolvido com ☕ por Carlos André Couto**
