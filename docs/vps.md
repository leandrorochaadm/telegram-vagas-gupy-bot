# Bot de vagas na VPS

O bot roda na VPS `srv2083922` por um timer do systemd, com o banco local e todo acesso a site de vagas pelo WARP.
O GitHub Actions fica como reserva manual (`workflow_dispatch`).

## Peças

| Peça | Onde | Para quê |
|---|---|---|
| `deploy/vagas-bot.timer` | `/etc/systemd/system/` | Dispara a rodada nos horários de Brasília |
| `deploy/vagas-bot.service` | `/etc/systemd/system/` | Uma rodada: `run_all.py` |
| `deploy/vagas-bot-failed.service` | `/etc/systemd/system/` | Avisa no Telegram quando a rodada falha |
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

## Desligar

```bash
sudo systemctl disable --now vagas-bot.timer
```

## Voltar para o GitHub (rollback)

1. `sudo systemctl disable --now vagas-bot.timer`
2. Devolver os `crons` de `scheduler/wrangler.jsonc` (`git revert`) e rodar `npx wrangler deploy` dentro de `scheduler/`.
3. Devolver o `schedule:` de `.github/workflows/vagas.yml`.
