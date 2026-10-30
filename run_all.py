"""One round of the bot on the VPS: checks WARP, runs every source at once (each sends its jobs as found), sends the
queue, fetches the pending job descriptions (only with WARP up), then sends the alerts.

Replaces the matrix + `finalizar` jobs of the GitHub workflow. Started by vagas-bot.service.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from subprocess import Popen

from store import Source

SOURCE_TIMEOUT_S = 12 * 60
SEND_TIMEOUT_S = 15 * 60
DESCRIPTIONS_TIMEOUT_S = 10 * 60
KILL_GRACE_S = 10
POLL_INTERVAL_S = 1
EXIT_PROXY_DOWN = 78  # main.EXIT_PROXY_DOWN; not imported to keep main.py (and its env loading) out of this process
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")
MEMORY_LIMIT_GB = 2
BYTES_PER_GB = 1024 ** 3
MAIN_SCRIPT = "main.py"

Spawn = Callable[..., Popen]
Run = Callable[..., int]


class SourceOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    OUT_OF_MEMORY = "out_of_memory"


def _duration(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def outcome_line(name: str, outcome: SourceOutcome, elapsed: float, code: int | None) -> str:
    match outcome:
        case SourceOutcome.OK:
            return f"[{name}] ✅ terminou em {_duration(elapsed)}"
        case SourceOutcome.ERROR:
            return f"[{name}] ❌ terminou com erro (código {code}) em {_duration(elapsed)}"
        case SourceOutcome.TIMEOUT:
            return f"[{name}] ⏱️ passou de {SOURCE_TIMEOUT_S // 60} min e foi encerrada"
        case SourceOutcome.OUT_OF_MEMORY:
            return (f"[{name}] 💥 sem memória: o bot passou do limite de {MEMORY_LIMIT_GB} GB "
                    "e esta fonte foi encerrada")


def cgroup_dir(proc_cgroup: Path = PROC_CGROUP, root: Path = CGROUP_ROOT) -> Path | None:
    """The unit's cgroup directory, or None outside systemd with cgroup v2."""
    try:
        for line in proc_cgroup.read_text().splitlines():
            hierarchy, _, path = line.partition("::")
            if hierarchy == "0" and path:
                found = root / path.lstrip("/")
                return found if found.is_dir() else None
    except OSError:
        pass
    return None


def read_oom_kills(cgroup: Path | None) -> int:
    if cgroup is None:
        return 0
    try:
        for line in (cgroup / "memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        pass
    return 0


def read_peak_bytes(cgroup: Path | None) -> int | None:
    if cgroup is None:
        return None
    try:
        return int((cgroup / "memory.peak").read_text().strip())
    except (OSError, ValueError):
        return None


def log(text: str) -> None:
    print(text, flush=True)


def _pump(name: str, stream) -> None:
    for line in stream:
        log(f"[{name}] {line.rstrip()}")


def spawn_source(cmd: Sequence[str], **kwargs) -> Popen:
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, **kwargs)


def run_step(cmd: Sequence[str], *, timeout: float = SEND_TIMEOUT_S) -> int:
    try:
        return subprocess.run(cmd, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        return 1


def _kill_group(proc: Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def _python_main(*args: str) -> list[str]:
    return [sys.executable, "-u", MAIN_SCRIPT, *args]


def run_sources(spawn: Spawn, cgroup: Path | None, *, clock: Callable[[], float] = time.monotonic,
                pause: Callable[[float], None] = time.sleep) -> dict[Source, SourceOutcome]:
    """Starts every source together and waits for all of them with one shared deadline."""
    started = clock()
    sources = Source.search_sources()
    procs: dict[Source, Popen] = {}
    pumps: list[threading.Thread] = []
    for source in sources:
        proc = spawn(_python_main("--source", source.api_value, "--send-now"))
        procs[source] = proc
        if proc.stdout is not None:
            pump = threading.Thread(target=_pump, args=(source.api_value, proc.stdout), daemon=True)
            pump.start()
            pumps.append(pump)

    outcomes: dict[Source, SourceOutcome] = {}
    oom_seen = read_oom_kills(cgroup)
    pending = dict(procs)
    while pending:
        timed_out = clock() - started >= SOURCE_TIMEOUT_S
        for source, proc in list(pending.items()):
            code = proc.poll()
            name = source.api_value
            if code is None and not timed_out:
                continue
            del pending[source]
            if code is None:
                _kill_group(proc)
                outcomes[source] = SourceOutcome.TIMEOUT
                log(outcome_line(name, SourceOutcome.TIMEOUT, SOURCE_TIMEOUT_S, None))
                continue
            elapsed = clock() - started
            if code == 0:
                outcome = SourceOutcome.OK
            else:
                oom_now = read_oom_kills(cgroup)
                killed = code == -signal.SIGKILL
                outcome = SourceOutcome.OUT_OF_MEMORY if killed and oom_now > oom_seen else SourceOutcome.ERROR
                oom_seen = oom_now
            outcomes[source] = outcome
            log(outcome_line(name, outcome, elapsed, code))
        if pending:
            pause(POLL_INTERVAL_S)
    for pump in pumps:
        pump.join(timeout=5)
    return outcomes


def main(argv: Sequence[str] = (), *, spawn: Spawn = spawn_source, run: Run = run_step,
         fetch_descriptions: bool = False) -> int:
    # fetch_descriptions is off by default only so the older tests keep their exact step lists; __main__ turns it on.
    cgroup = cgroup_dir()
    proxy_code = run(_python_main("--check-proxy"))
    if proxy_code != 0:
        log(f"❌ WARP fora do ar (código {proxy_code}): nenhuma fonte vai rodar.")
    else:
        outcomes = run_sources(spawn, cgroup)
        for source, outcome in outcomes.items():
            if outcome is not SourceOutcome.OK:
                run(_python_main("--failed", source.api_value))
    run(_python_main("--send-jobs"))
    if fetch_descriptions and proxy_code == 0:
        run(_python_main("--fetch-descriptions"), timeout=DESCRIPTIONS_TIMEOUT_S)  # exit code ignored on purpose
    alerts_code = run(_python_main("--send-alerts"))
    peak = read_peak_bytes(cgroup)
    if peak is not None:
        peak_gb = f"{peak / BYTES_PER_GB:.1f}".replace(".", ",")
        log(f"Pico de memória da rodada: {peak_gb} GB de {MEMORY_LIMIT_GB} GB")
    return 0 if alerts_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], fetch_descriptions=True))
