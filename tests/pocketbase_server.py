"""Throwaway PocketBase server for tests (not collected by unittest discovery)."""

import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time

import requests

POCKETBASE_BIN_ENV = "POCKETBASE_BIN"
MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pocketbase", "pb_migrations"
)
SUPERUSER_EMAIL = "superuser@test.local"
HEALTH_TIMEOUT_SECONDS = 15
HEALTH_POLL_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 10
LOG_TAIL_LINES = 30


def pocketbase_bin() -> str | None:
    return os.getenv(POCKETBASE_BIN_ENV) or None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PocketBaseServer:
    def __init__(self, binary: str, migrations_dir: str = MIGRATIONS_DIR) -> None:
        self._binary = binary
        self._migrations_dir = migrations_dir
        self._root: str | None = None
        self._process: subprocess.Popen | None = None
        self._log_path = ""
        self.url = ""
        self.superuser_token = ""

    def _run(self, args: list[str]) -> None:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{args[1]} failed: {result.stdout}{result.stderr}")

    def _log_tail(self) -> str:
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as handle:
                return "".join(handle.readlines()[-LOG_TAIL_LINES:])
        except OSError:
            return ""

    def start(self) -> None:
        self._root = tempfile.mkdtemp(prefix="pb_test_")
        data_dir = os.path.join(self._root, "pb_data")
        os.makedirs(data_dir)
        self._log_path = os.path.join(self._root, "pocketbase.log")
        # Prefix: token_urlsafe can start with "-", which the CLI would parse as a flag.
        password = "pw" + secrets.token_urlsafe(24)
        try:
            self._run([self._binary, "migrate", "up", f"--dir={data_dir}", f"--migrationsDir={self._migrations_dir}"])
            self._run([self._binary, "superuser", "upsert", SUPERUSER_EMAIL, password, f"--dir={data_dir}"])
            port = _free_port()
            self.url = f"http://127.0.0.1:{port}"
            with open(self._log_path, "wb") as log:
                self._process = subprocess.Popen(
                    [
                        self._binary,
                        "serve",
                        f"--http=127.0.0.1:{port}",
                        f"--dir={data_dir}",
                        f"--migrationsDir={self._migrations_dir}",
                        "--automigrate=false",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            self._wait_healthy()
            response = self.request(
                "POST",
                "/api/collections/_superusers/auth-with-password",
                json={"identity": SUPERUSER_EMAIL, "password": password},
            )
            response.raise_for_status()
            self.superuser_token = response.json()["token"]
        except Exception:
            tail = self._log_tail()
            self.stop()
            if tail:
                print(tail)
            raise

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(f"PocketBase exited early:\n{self._log_tail()}")
            try:
                if requests.get(f"{self.url}/api/health", timeout=1).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(HEALTH_POLL_SECONDS)
        raise RuntimeError(f"PocketBase did not become healthy:\n{self._log_tail()}")

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> requests.Response:
        headers = {"Authorization": token} if token else {}
        return requests.request(
            method,
            f"{self.url}{path}",
            headers=headers,
            json=json,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def create_record(self, collection: str, body: dict) -> dict:
        response = self.request(
            "POST", f"/api/collections/{collection}/records", token=self.superuser_token, json=body
        )
        assert response.status_code == 200, f"{collection}: {response.status_code} {response.text}"
        return response.json()

    def get_record(self, collection: str, record_id: str) -> dict | None:
        response = self.request(
            "GET", f"/api/collections/{collection}/records/{record_id}", token=self.superuser_token
        )
        if response.status_code == 404:
            return None
        assert response.status_code == 200, f"{collection}: {response.status_code} {response.text}"
        return response.json()

    def count(self, collection: str) -> int:
        response = self.request(
            "GET",
            f"/api/collections/{collection}/records",
            token=self.superuser_token,
            params={"perPage": 1},
        )
        assert response.status_code == 200, f"{collection}: {response.status_code} {response.text}"
        return response.json()["totalItems"]

    def create_auth_account(self, collection: str, email: str, password: str) -> str:
        record = self.create_record(
            collection, {"email": email, "password": password, "passwordConfirm": password}
        )
        return record["id"]

    def login(self, collection: str, email: str, password: str) -> str:
        response = self.request(
            "POST",
            f"/api/collections/{collection}/auth-with-password",
            json={"identity": email, "password": password},
        )
        assert response.status_code == 200, f"login {collection}: {response.status_code} {response.text}"
        return response.json()["token"]
