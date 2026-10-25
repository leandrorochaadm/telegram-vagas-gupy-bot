# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""Create or update the bot's login account in PocketBase with a fresh password.

The superuser password comes from stdin (one line) or from a prompt when stdin is a terminal.
The new bot password is written only to a file with mode 600 and is never printed.
"""

import argparse
import getpass
import os
import secrets
import sys
from enum import StrEnum

import requests

BOT_COLLECTION = "bot_accounts"
SUPERUSER_COLLECTION = "_superusers"
REQUEST_TIMEOUT_SECONDS = 15
UNIQUE_ERROR_CODE = "validation_not_unique"
SECRET_FILE_MODE = 0o600

EXIT_OK = 0
EXIT_FAILURE = 2


class AccountFailure(Exception):
    """Short pt-BR message; never contains a password or a token."""


class AccountAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"


def generate_password() -> str:
    return secrets.token_urlsafe(32)


def write_secret(path: str, secret: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    except OSError:
        raise AccountFailure(f"não foi possível gravar o arquivo {path}") from None
    try:
        os.fchmod(fd, SECRET_FILE_MODE)
        os.write(fd, secret.encode("utf-8"))
    finally:
        os.close(fd)


def _request(
    method: str, url: str, path: str, token: str = "", **kwargs
) -> requests.Response:
    headers = {"Authorization": token} if token else {}
    try:
        return requests.request(
            method, f"{url.rstrip('/')}{path}", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs
        )
    except requests.RequestException:
        raise AccountFailure(f"o PocketBase não respondeu em {url}") from None


def _error_codes(response: requests.Response) -> list[str]:
    try:
        data = response.json().get("data", {})
    except ValueError:
        return []
    return [str(value.get("code", "")) for value in data.values() if isinstance(value, dict)]


def _fail(step: str, response: requests.Response) -> AccountFailure:
    codes = ", ".join(_error_codes(response)) or "-"
    return AccountFailure(f"erro em {step}: status {response.status_code}, códigos: {codes}")


def login(url: str, collection: str, email: str, password: str) -> str | None:
    """Returns the token, or None when the credentials are refused."""
    response = _request(
        "POST",
        url,
        f"/api/collections/{collection}/auth-with-password",
        json={"identity": email, "password": password},
    )
    if response.status_code in (400, 401, 403):
        return None
    if response.status_code >= 400:
        raise _fail(collection, response)
    return response.json()["token"]


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _find_account_id(url: str, token: str, bot_email: str) -> str | None:
    response = _request(
        "GET",
        url,
        f"/api/collections/{BOT_COLLECTION}/records",
        token,
        params={"filter": f'email = "{_escape_filter_value(bot_email)}"', "perPage": 1},
    )
    if response.status_code >= 400:
        raise _fail(BOT_COLLECTION, response)
    items = response.json()["items"]
    return items[0]["id"] if items else None


def _update_password(url: str, token: str, record_id: str, password: str) -> None:
    response = _request(
        "PATCH",
        url,
        f"/api/collections/{BOT_COLLECTION}/records/{record_id}",
        token,
        json={"password": password, "passwordConfirm": password},
    )
    if response.status_code >= 400:
        raise _fail(BOT_COLLECTION, response)


def upsert_bot_account(url: str, token: str, bot_email: str, password: str) -> AccountAction:
    record_id = _find_account_id(url, token, bot_email)
    if record_id is None:
        response = _request(
            "POST",
            url,
            f"/api/collections/{BOT_COLLECTION}/records",
            token,
            json={
                "email": bot_email,
                "password": password,
                "passwordConfirm": password,
                "verified": True,
            },
        )
        if response.status_code < 400:
            return AccountAction.CREATED
        if response.status_code != 400 or UNIQUE_ERROR_CODE not in _error_codes(response):
            raise _fail(BOT_COLLECTION, response)
        # Another run created it in between: fall through to the update.
        record_id = _find_account_id(url, token, bot_email)
        if record_id is None:
            raise _fail(BOT_COLLECTION, response)
    _update_password(url, token, record_id, password)
    return AccountAction.UPDATED


def read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Senha do superusuário: ")
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise AccountFailure("a senha do superusuário está vazia")
    return password


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _run(args: argparse.Namespace) -> int:
    superuser_password = read_password()
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if not os.path.isdir(out_dir):
        raise AccountFailure(f"a pasta {out_dir} não existe")

    token = login(args.url, SUPERUSER_COLLECTION, args.email, superuser_password)
    if token is None:
        raise AccountFailure("o login do superusuário foi recusado")

    password = generate_password()
    temp_path = f"{args.out}.tmp"
    write_secret(temp_path, password)
    try:
        action = upsert_bot_account(args.url, token, args.bot_email, password)
        if login(args.url, BOT_COLLECTION, args.bot_email, password) is None:
            raise AccountFailure("a conta do bot foi gravada, mas o login com a senha nova falhou")
        os.replace(temp_path, args.out)
    except BaseException:
        _remove_quietly(temp_path)
        raise

    label = "criada" if action is AccountAction.CREATED else "atualizada"
    print(f"conta do bot {label}: {args.bot_email}")
    print(f"senha gravada em {args.out}")
    if action is AccountAction.UPDATED:
        print("atenção: a senha anterior deixou de valer; atualize o segredo PB_BOT_PASSWORD")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cria ou atualiza a conta de login do bot.")
    parser.add_argument("--url", required=True, help="endereço do PocketBase")
    parser.add_argument("--email", required=True, help="e-mail do superusuário")
    parser.add_argument("--bot-email", required=True, help="e-mail da conta do bot")
    parser.add_argument("--out", required=True, help="arquivo onde a senha nova é gravada")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except AccountFailure as error:
        print(f"erro: {error}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
