# 🤖 Multi-Source Job Tracker: Automação de Vagas para Devs no Telegram

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![Sources](https://img.shields.io/badge/Fontes-Gupy%20%7C%20LinkedIn%20%7C%20ProgramaThor-orange)]()
[![Telegram](https://img.shields.io/badge/Alertas-Telegram-2CA5E0)]()
[![GitHub Actions](https://img.shields.io/badge/Automação-GitHub%20Actions-181717?logo=github)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 📖 Sobre o Projeto

Este projeto começou como um fork do excelente trabalho de [Lucas Nunes](https://github.com/lucasnunestrabalho99-sudo/telegram-vagas-gupy-bot), que criou um bot para monitorar vagas na Gupy para o Rio de Janeiro.

Partindo dessa base, evoluí o projeto para atender minha própria realidade como Engenheiro de Software Mobile (Flutter) Sênior buscando oportunidades **100% Remoto no Brasil**, adicionando múltiplas fontes, filtros de perfil e automação completa via GitHub Actions.

---

## ✨ Diferenciais da versão adaptada

### 🔍 Múltiplas fontes de vagas
| Fonte | Tipo | Cobertura |
|---|---|---|
| 🟣 **Gupy** | API JSON | SP + Remoto |
| 🔷 **LinkedIn** | API Guest | SP + Remoto |
| 🟤 **ProgramaThor** | Web Scraping | SP + Remoto |
| 🟢 **Solides** | API JSON | Remoto |
| 🟡 **InHire** | Web Scraping | Remoto |

### 🎯 Filtros inteligentes baseados em perfil
- **Senioridade:** o bot agora busca todas as senioridades, focado no nível Sênior (o antigo bloqueio foi removido).
- **Gaps eliminatórios:** bloqueia vagas com PHP, Python, Node.js, React Native, KMP, modelo presencial, Fullstack, Swift, Kotlin, UX, exigência de inglês fluente/avançado, entre outros.
- **Vagas recentes:** limita a vagas publicadas nos últimos 3 dias
- **Deduplicação dupla:** por URL (banco SQLite) + por título/empresa na sessão

### 📊 Score de match por vaga
Cada alerta chega com um indicador de compatibilidade baseado no stack técnico detectado. O algoritmo analisa **tanto o título quanto a descrição completa da vaga** para garantir precisão:
- 🟢 **Alto** — 2+ tecnologias do stack avançado identificadas
- 🟡 **Médio** — 1 tecnologia identificada
- 🔵 **Padrão** — sem tech específica no título (ainda relevante)

### ⚙️ Automação completa via GitHub Actions
Roda automaticamente na nuvem sem precisar de servidor ou PC ligado:
- **Segunda a sexta:** 8h, 10h, 12h, 14h, 16h, 18h e 20h (BRT)
- **Sábado e domingo:** 10h, 14h e 18h (BRT)

---

## 🚀 Como funciona

```
GitHub Actions (agendado)
    ↓
Varre Gupy + LinkedIn + ProgramaThor + Solides + InHire
    ↓
Aplica filtros de perfil (senioridade, gaps, data)
    ↓
Calcula score de match com o stack técnico
    ↓
Envia alertas formatados no Telegram
    ↓
Salva DB atualizado no repositório
```

---

## ⚙️ Como usar / adaptar para o seu perfil

### 1. Pré-requisitos
- Python 3.8+
- Um bot criado no Telegram via [@BotFather](https://t.me/botfather)
- Conta no GitHub

### 2. Clone e configure

```bash
git clone https://github.com/pmarsiglia93/telegram-vagas-gupy-bot.git
cd telegram-vagas-gupy-bot
pip install -r requirements.txt
```

Crie um arquivo `.env` na raiz:
```env
TELEGRAM_TOKEN=seu_token_aqui
CHAT_ID_GRUPO=seu_chat_id_aqui
```

### 3. Adapte para o seu perfil

Abra o `main.py` e ajuste as constantes de perfil:

```python
# Termos que eliminam a vaga (tecnologias fora do seu stack)
GAPS_ELIMINATORIOS = [
    "php", "python", "react native", "presencial", ...
]

# Tecnologias do seu stack avançado (peso 2 no score)
STACK_AVANCADO = [
    "flutter", "dart", "clean architecture", ...
]

# Buscas por cargo e localização
filtros_de_busca = [
    {"nome": "FLUTTER · REMOTO", "params": {'workplaceTypes': 'remote', 'jobName': 'flutter', 'limit': 10}},
    ...
]
```

### 4. Configure o GitHub Actions

No seu repositório GitHub, vá em **Settings → Secrets and variables → Actions** e adicione:

| Secret | Descrição |
|---|---|
| `TELEGRAM_TOKEN` | Token do seu bot (via @BotFather) |
| `CHAT_ID_GRUPO` | ID do grupo/canal do Telegram |

---

## 📸 Exemplo de alerta no Telegram

```
🟣 GUPY — FLUTTER · REMOTO

💼 Vaga: Desenvolvedor Mobile Flutter Senior
🏢 Empresa: Empresa X
📍 Local: Qualquer lugar (Remoto)
💻 Modelo: Remoto
📄 Tipo: Efetivo
♿ PCD: Não informado
📅 Data: 30/04/2026 às 09:15
📊 Match: 🟢 Alto · FLUTTER · DART · CLEAN ARCHITECTURE

🔗 Clique aqui para aplicar
```

---

## 🙏 Créditos

Projeto originalmente desenvolvido por **[Lucas Nunes](https://github.com/lucasnunestrabalho99-sudo)** — obrigado por tornar o código público e inspirar esta evolução.

---

**Desenvolvido com ☕ por Carlos André Couto**
