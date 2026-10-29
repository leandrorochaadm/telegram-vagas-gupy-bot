#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""Avisa no Telegram quando uma rodada do bot de vagas falha na VPS e quando volta a funcionar.

Separado do main.py de propósito: avisa mesmo quando o erro está no próprio bot.
Chamado pelo systemd: `failed` pelo vagas-bot-failed.service (OnFailure da unit do bot)
e `ok` pelo ExecStartPost da unit, que só roda quando a rodada termina bem.
"""

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

import requests

UNIT = "vagas-bot.service"
# StateDirectory=vagas-bot in both units; outside systemd (tests, by hand) the env var points elsewhere
STATE_DIR_ENV = "STATE_DIRECTORY"
DEFAULT_STATE_DIR = Path("/var/lib/vagas-bot")
FAILED_FLAG_NAME = "run-failed"
TELEGRAM_TOKEN_ENV = "TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID_ENV = "CHAT_ID_GRUPO"
TELEGRAM_TIMEOUT_SECONDS = 15
LOG_HINT = f"Detalhes: journalctl -u {UNIT} -n 50"


class Mode(StrEnum):
    FAILED = "failed"
    OK = "ok"


class FailureReason(StrEnum):
    """Why the run failed: systemd's Result of the unit."""

    EXIT_CODE = "exit-code"
    TIMEOUT = "timeout"
    OOM_KILL = "oom-kill"
    SIGNAL = "signal"
    CORE_DUMP = "core-dump"
    UNKNOWN = "unknown"

    @classmethod
    def from_api(cls, raw: str) -> "FailureReason":
        try:
            return cls(raw.strip())
        except ValueError:
            return cls.UNKNOWN

    @property
    def phrase(self) -> str:
        match self:
            case FailureReason.EXIT_CODE:
                return "terminou com erro"
            case FailureReason.TIMEOUT:
                return "passou do tempo limite de 45 minutos"
            case FailureReason.OOM_KILL:
                return "ficou sem memória (passou do limite de 2 GB)"
            case FailureReason.SIGNAL | FailureReason.CORE_DUMP:
                return "foi interrompido no meio"
            case FailureReason.UNKNOWN:
                return "parou por um motivo desconhecido"


Sender = Callable[[str], None]
Runner = Callable[[Sequence[str]], str]


def run_command(cmd: Sequence[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def state_dir() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)


def failure_reason(run: Runner) -> FailureReason:
    return FailureReason.from_api(run(["systemctl", "show", UNIT, "-p", "Result", "--value"]))


def failed_text(reason: FailureReason) -> str:
    return f"⚠️ O bot de vagas falhou na VPS: {reason.phrase}. {LOG_HINT}"


RECOVERED_TEXT = "✅ O bot de vagas voltou a funcionar."


def handle_failed(flag: Path, send: Sender, run: Runner) -> None:
    """One alert per failure streak: the flag file marks that it was already sent."""
    if flag.exists():
        return
    reason = failure_reason(run)
    print(f"Rodada falhou: {reason.value}")
    send(failed_text(reason))
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


def handle_ok(flag: Path, send: Sender) -> None:
    if not flag.exists():
        return
    send(RECOVERED_TEXT)
    flag.unlink()


def telegram_sender(token: str, chat_id: str) -> Sender:
    def send(text: str) -> None:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()

    return send


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        mode = Mode(args[0]) if len(args) == 1 else None
    except ValueError:
        mode = None
    if mode is None:
        print(f"Uso: run_alert.py {{{'|'.join(Mode)}}}", file=sys.stderr)
        return 2
    token = os.environ.get(TELEGRAM_TOKEN_ENV)
    chat_id = os.environ.get(TELEGRAM_CHAT_ID_ENV)
    if not token or not chat_id:
        print(f"Faltam {TELEGRAM_TOKEN_ENV} e/ou {TELEGRAM_CHAT_ID_ENV}", file=sys.stderr)
        return 1
    flag = state_dir() / FAILED_FLAG_NAME
    send = telegram_sender(token, chat_id)
    try:
        match mode:
            case Mode.FAILED:
                handle_failed(flag, send, run_command)
            case Mode.OK:
                handle_ok(flag, send)
    except requests.RequestException as exc:
        # The flag stays as it was, so the next run tries the alert again; never print the token (it is in the URL)
        status = exc.response.status_code if exc.response is not None else type(exc).__name__
        print(f"Não foi possível avisar no Telegram ({status})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
