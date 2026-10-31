# Bot de vagas na VPS

O bot roda na VPS `srv2083922` por um timer do systemd, com o banco local e todo acesso a site de vagas pelo WARP.
O GitHub Actions fica como reserva manual (`workflow_dispatch`).

## Peças

| Peça | Onde | Para quê |
|---|---|---|
| `deploy/vagas-bot.timer` | `/etc/systemd/system/` | Dispara a rodada nos horários de Brasília |
| `deploy/vagas-bot.service` | `/etc/systemd/system/` | Uma rodada: `run_all.py` |
| `deploy/vagas-bot-failed.service` | `/etc/systemd/system/` | Avisa no Telegram quando a rodada falha |
| `deploy/vagas-analysis.timer` | `/etc/systemd/system/` | Dispara a análise das vagas toda segunda às 8h |
| `deploy/vagas-analysis.service` | `/etc/systemd/system/` | Uma análise: `analyze_descriptions.py` (relatório em `/opt/vagas-bot/app/reports/`) |
| `/etc/vagas-bot/bot.env` | VPS | Segredos e configuração (modelo: `deploy/bot.env.example`) |
| `/opt/vagas-bot/app` | VPS | Clone deste repositório |
| `/opt/vagas-bot/venv` | VPS | Python 3.12 com as dependências |
| `/opt/vagas-bot/ms-playwright` | VPS | Chromium, instalado uma vez |
| `warp-svc` | VPS | Túnel da Cloudflare (proxy `socks5h://127.0.0.1:40000`) |
| PocketBase `pocketbase-job-search` | VPS, porta 8093 | Banco de vagas |

## WARP

Já instalado. Conferir que está ligado:

```bash
curl -fsS --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace | grep warp
```

Tem de mostrar `warp=on`. Com `REQUIRE_SCRAPER_PROXY=true`, se o WARP estiver fora nenhuma busca roda e sai o aviso
"Não consegui ligar o WARP, nenhuma busca foi feita.".

## Segredos

O dono grava com o nano (nunca colar no chat):

```bash
! sudo install -d -m 700 -o deploy -g deploy /etc/vagas-bot
! install -m 600 /dev/null /etc/vagas-bot/bot.env && nano /etc/vagas-bot/bot.env
```

Cole o modelo de `deploy/bot.env.example`, preencha, salve com Ctrl+O e saia com Ctrl+X.
Conferir sem mostrar o valor: `stat -c '%a %U' /etc/vagas-bot/bot.env` → `600 deploy`.

## Instalar

```bash
! sudo install -d -o deploy -g deploy /opt/vagas-bot
git clone <url-do-repositorio> /opt/vagas-bot/app
uv venv --python 3.12 /opt/vagas-bot/venv
uv pip install --python /opt/vagas-bot/venv -r /opt/vagas-bot/app/requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/vagas-bot/ms-playwright /opt/vagas-bot/venv/bin/python -m playwright install chromium
# se o Chromium não abrir por falta de bibliotecas:
! sudo /opt/vagas-bot/venv/bin/python -m playwright install-deps chromium
```

Units:

```bash
! sudo cp /opt/vagas-bot/app/deploy/vagas-bot.{service,timer} /opt/vagas-bot/app/deploy/vagas-bot-failed.service /etc/systemd/system/
! sudo systemctl daemon-reload
```

## Rodada

Ordem dos passos: `--check-proxy` → fontes (e `--failed` para as que falharam) → `--send-jobs` →
`--fetch-descriptions` (até 10 min, só com o WARP no ar) → `--send-alerts`. Se o passo de descrições falhar ou
estourar o tempo, a rodada não fica vermelha.

## Rodar à mão (antes do `enable`)

```bash
sudo systemctl start vagas-bot.service
journalctl -u vagas-bot.service -f
```

Conferir: vagas e um aviso só no Telegram; nenhuma fonte sem proxy; pico de memória abaixo de 2 GB
(`systemctl show vagas-bot.service -p MemoryPeak`).

Depois, ligar o agendamento:

```bash
sudo systemctl enable --now vagas-bot.timer
systemctl list-timers vagas-bot.timer
```

## Horários (Brasília)

- Segunda a sexta: 8h, 10h, 12h … 20h
- Sábado e domingo: 10h, 14h, 18h
- Análise das vagas: segunda às 8h

`Persistent=true` roda a rodada perdida quando a VPS volta. VPS desligada = nada roda e nada avisa.

## Ver o log

O journal da VPS é persistente.

```bash
journalctl -u vagas-bot.service --since today
# só os problemas:
journalctl -u vagas-bot.service --since today -g 'erro|sem memória|passou de|falhou'
```

Cada fonte aparece com o prefixo `[gupy]`, `[remotar]` etc. Estouro de memória também aparece na linha do systemd
"killed by the OOM killer".

## Atualizar o código

```bash
git -C /opt/vagas-bot/app pull --ff-only
```

Se o `requirements.txt` mudou, rodar o `uv pip install` de novo. Se mudou a versão do `playwright`, reinstalar o
Chromium com o mesmo `PLAYWRIGHT_BROWSERS_PATH`.

### Quando entra migration nova

1. Aplicar pelo `pb_deploy_migrations.py`, primeiro com `--dry-run` e depois sem. Ele faz backup do banco, troca as
   migrations e reinicia o PocketBase (volta ao estado anterior se falhar). O `pb_provision.py` só copia as
   migrations na primeira instalação; nas seguintes ele apenas avisa que há diferença.

```bash
sudo python3 /home/deploy/.claude/skills/publish-pocketbase-pwa/scripts/pb_deploy_migrations.py job-search --source /opt/vagas-bot/app/pocketbase/pb_migrations --dry-run
```

2. Conferir no painel (túnel `ssh -N -L 8093:127.0.0.1:8093 hostinger`) que a coleção `job_descriptions` existe.

### Descrições das vagas antigas (uma vez)

Rodar com o mesmo ambiente da unit, num subshell para não deixar as variáveis no terminal:

```bash
( cd /opt/vagas-bot/app && set -a && . /etc/vagas-bot/bot.env && set +a && /opt/vagas-bot/venv/bin/python main.py --backfill-descriptions )
```

Imprime o total de pendências criadas. Centenas de pendentes levam várias rodadas (40 por rodada). Rodar de novo
não duplica.

Prova contra os sites reais: rodar `main.py --fetch-descriptions` do mesmo jeito e conferir no painel que uma vaga
de cada fonte ficou `saved`.

## Análise semanal das vagas

Toda segunda às 8h o `vagas-analysis.timer` roda a análise das descrições salvas e grava o relatório em
`/opt/vagas-bot/app/reports/`. Gasta crédito da API do Claude.

### 1. Configurar (uma vez)

Chave da API do Claude: abra o arquivo e preencha `ANTHROPIC_API_KEY=`.

```bash
! nano /etc/vagas-bot/bot.env
```

Conferir sem mostrar o valor: `grep -c '^ANTHROPIC_API_KEY=.' /etc/vagas-bot/bot.env` → `1` e
`stat -c '%a %U' /etc/vagas-bot/bot.env` → `600 deploy`.

Currículo: o usuário `deploy` precisa ler o `.tex`, que fica na máquina de desenvolvimento (não na VPS). Envie
para `/etc/vagas-bot/` e preencha `RESUME_PATH=/etc/vagas-bot/curriculo.tex` no `bot.env`. Na máquina de
desenvolvimento:

```bash
scp /home/leandro/projects/cv/curriculo-leandro-rocha-flutter.tex hostinger:/tmp/curriculo.tex
ssh hostinger 'sudo install -m 600 -o deploy -g deploy /tmp/curriculo.tex /etc/vagas-bot/curriculo.tex && rm /tmp/curriculo.tex'
```

Repita a cópia quando o currículo mudar. Sem `RESUME_PATH`, a análise sai sem comparação com o currículo e não falha.

### 2. Instalar

```bash
! sudo cp /opt/vagas-bot/app/deploy/vagas-analysis.{service,timer} /etc/systemd/system/
! sudo systemctl daemon-reload
```

### 3. Rodar à mão (antes do `enable`)

```bash
sudo systemctl start vagas-analysis.service
journalctl -u vagas-analysis.service -f
```

### 4. Ver o resultado

```bash
ls -t /opt/vagas-bot/app/reports/
journalctl -u vagas-analysis.service -n 50
```

Os arquivos são `analise-vagas-AAAA-MM-DD-HHMM.md` e `.csv` (horário de Brasília). Se a análise falhar, o aviso
"A análise semanal das vagas falhou: …" chega no Telegram na próxima rodada do bot.

### 5. Ligar o agendamento

```bash
sudo systemctl enable --now vagas-analysis.timer
systemctl list-timers vagas-analysis.timer
```

### 6. Cache

- `reports/cache/extractions.json`: evita pagar de novo pela mesma vaga.
- `reports/cache/learned_terms.json`: guarda as tecnologias aprendidas (apagar faz a análise esquecer).

## Desligar

```bash
sudo systemctl disable --now vagas-bot.timer
# só a análise:
sudo systemctl disable --now vagas-analysis.timer
```

## Voltar para o GitHub (rollback)

1. `sudo systemctl disable --now vagas-bot.timer`
2. Devolver os `crons` de `scheduler/wrangler.jsonc` (`git revert`) e rodar `npx wrangler deploy` dentro de `scheduler/`.
3. Devolver o `schedule:` de `.github/workflows/vagas.yml`.
