"""Unit tests for the "Conectar Cloudflare WARP" step of .github/workflows/vagas.yml.

The step's bash script is extracted from the workflow and run for real, with fake curl,
sha256sum, tar, wgcf, wireproxy, timeout and sleep on the PATH: no network, no WARP.

Run: python -m unittest discover -s tests -v
"""
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "vagas.yml")
STEP_NAME = "Conectar Cloudflare WARP"
ENDPOINTS = [
    "engage.cloudflareclient.com:2408",
    "162.159.193.1:2408",
    "162.159.192.1:500",
    "162.159.193.1:4500",
    "162.159.192.1:1701",
]
PROXY_LINE = "SCRAPER_PROXY=socks5h://127.0.0.1:40000"


def read_step() -> tuple[str, str]:
    """(step header lines, run script) of the WARP step, parsed from the workflow text."""
    with open(WORKFLOW, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP_NAME}")
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    run_indent = len(lines[run_at]) - len(lines[run_at].lstrip())
    body: list[str] = []
    for line in lines[run_at + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= run_indent:
            break
        body.append(line)
    return "\n".join(lines[start:run_at]), textwrap.dedent("\n".join(body)) + "\n"


STEP_HEADER, STEP_SCRIPT = read_step()

# Fakes. State shared with the test lives in $FAKE_STATE.
FAKE_CURL = r"""#!/usr/bin/env bash
out=""; url=""; socks=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    --socks5-hostname) socks=1; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
if [ -n "$out" ]; then
  case "$url" in
    *wgcf_*) cp "$FAKE_BIN_SRC/wgcf" "$out" ;;
    *) echo fake-archive > "$out" ;;
  esac
  exit 0
fi
[ "$socks" = 1 ] || exit 2
active=$(cat "$FAKE_STATE/active" 2>/dev/null) || exit 7
case ",$FAKE_WORKING," in
  *",$active,"*) printf 'fl=1\nip=2a09:bac1::1\nwarp=%s\n' "${FAKE_WARP:-on}" ;;
  *) exit 7 ;;
esac
"""

FAKE_TAR = r"""#!/usr/bin/env bash
dir=""
while [ $# -gt 0 ]; do [ "$1" = "-C" ] && dir="$2"; shift; done
cp "$FAKE_BIN_SRC/wireproxy" "$dir/wireproxy"
"""

FAKE_WGCF = r"""#!/usr/bin/env bash
case "$1" in
  register) echo account > wgcf-account.toml ;;
  generate) printf '[Interface]\nPrivateKey = x\n\n[Peer]\n%s\n' \
    "${FAKE_ENDPOINT_LINE:-Endpoint = engage.cloudflareclient.com:2408}" > wgcf-profile.conf ;;
esac
"""

FAKE_WIREPROXY = r"""#!/usr/bin/env bash
profile=$(sed -n 's/^WGConfig = //p' "$2")
# Any spacing, like WireGuard itself: a profile the step failed to rewrite still connects
endpoint=$(sed -n 's/^Endpoint *= *//p' "$profile")
echo "$endpoint" >> "$FAKE_STATE/started"
echo "wireproxy log for $endpoint"
case ",$FAKE_CRASH," in *",$endpoint,"*) rm -f "$FAKE_STATE/active"; exit 1 ;; esac
echo "$$" >> "$FAKE_STATE/pids"
echo "$endpoint" > "$FAKE_STATE/active"
exec /bin/sleep 60
"""

# Cuts every "timeout N" to 3s so a dead endpoint costs 3s instead of 15s
FAKE_TIMEOUT = r"""#!/usr/bin/env bash
shift
exec perl -e 'alarm 3; exec @ARGV or exit 127' -- "$@"
"""

FAKE_SLEEP = "#!/usr/bin/env bash\nexec /bin/sleep 0.1\n"
FAKE_SHA256SUM = "#!/usr/bin/env bash\ncat > /dev/null\n"
# The runners have GNU sed; BSD sed (macOS) needs an explicit empty suffix for -i
BSD_SED_SHIM = '#!/usr/bin/env bash\nif [ "$1" = "-i" ]; then shift; exec /usr/bin/sed -i "" "$@"; fi\nexec /usr/bin/sed "$@"\n'


def has_gnu_sed() -> bool:
    try:
        return subprocess.run(["sed", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


GNU_SED = has_gnu_sed()


def write_exec(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(shutil.which("bash") and shutil.which("perl"), "needs bash and perl")
class WarpStepTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.runner_temp = os.path.join(self.tmp, "runner")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        self.bin_src = os.path.join(self.tmp, "bin-src")
        self.github_env = os.path.join(self.tmp, "github_env")
        for d in (self.home, self.runner_temp, self.state, self.bin, self.bin_src):
            os.makedirs(d)
        open(self.github_env, "w").close()

        fakes = {"curl": FAKE_CURL, "tar": FAKE_TAR, "timeout": FAKE_TIMEOUT,
                 "sleep": FAKE_SLEEP, "sha256sum": FAKE_SHA256SUM}
        if not GNU_SED:
            fakes["sed"] = BSD_SED_SHIM
        for name, content in fakes.items():
            write_exec(os.path.join(self.bin, name), content)
        write_exec(os.path.join(self.bin_src, "wgcf"), FAKE_WGCF)
        write_exec(os.path.join(self.bin_src, "wireproxy"), FAKE_WIREPROXY)

        self.script = os.path.join(self.tmp, "step.sh")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(STEP_SCRIPT)
        self.addCleanup(self.kill_wireproxies)

    def run_step(self, working=(), crash=(), warp="on", endpoint_line=None):
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": self.home,
            "RUNNER_TEMP": self.runner_temp,
            "GITHUB_ENV": self.github_env,
            "FAKE_STATE": self.state,
            "FAKE_BIN_SRC": self.bin_src,
            "FAKE_WORKING": ",".join(working),
            "FAKE_CRASH": ",".join(crash),
            "FAKE_WARP": warp,
        }
        if endpoint_line is not None:
            env["FAKE_ENDPOINT_LINE"] = endpoint_line
        # Same shell GitHub uses for a run step: bash -e
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", self.script], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def state_lines(self, name: str) -> list[str]:
        path = os.path.join(self.state, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().split()

    def pids(self) -> list[int]:
        return [int(p) for p in self.state_lines("pids")]

    def kill_wireproxies(self):
        for pid in self.pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def github_env_text(self) -> str:
        with open(self.github_env, encoding="utf-8") as f:
            return f.read()

    def assert_proxy_exported(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(PROXY_LINE, self.github_env_text())

    def assert_proxy_not_exported(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SCRAPER_PROXY", self.github_env_text())


class EndpointFallbackTest(WarpStepTestCase):
    def test_default_endpoint_works_first_try(self):
        result = self.run_step(working=[ENDPOINTS[0]])

        self.assert_proxy_exported(result)
        self.assertEqual(self.state_lines("started"), [ENDPOINTS[0]])
        self.assertIn(f"endpoint={ENDPOINTS[0]}", result.stdout)
        self.assertIn("warp=on", result.stdout)

    def test_falls_back_to_next_endpoint_when_default_has_no_handshake(self):
        result = self.run_step(working=[ENDPOINTS[1]])

        self.assert_proxy_exported(result)
        self.assertEqual(self.state_lines("started"), ENDPOINTS[:2])
        self.assertIn(f"No handshake via {ENDPOINTS[0]}", result.stdout)
        self.assertIn(f"endpoint={ENDPOINTS[1]}", result.stdout)

    def test_tries_every_endpoint_in_order_until_the_last_one_works(self):
        result = self.run_step(working=[ENDPOINTS[-1]])

        self.assert_proxy_exported(result)
        self.assertEqual(self.state_lines("started"), ENDPOINTS)

    def test_profile_points_at_the_endpoint_that_worked(self):
        self.run_step(working=[ENDPOINTS[1]])

        with open(os.path.join(self.home, ".wgcf", "wgcf-profile.conf"), encoding="utf-8") as f:
            profile = f.read()
        self.assertIn(f"Endpoint = {ENDPOINTS[1]}\n", profile)

    def test_failed_attempts_are_stopped_and_the_working_proxy_keeps_running(self):
        self.run_step(working=[ENDPOINTS[2]])

        *failed, working = self.pids()
        self.assertEqual(len(failed), 2)
        self.assertFalse(any(alive(pid) for pid in failed))
        self.assertTrue(alive(working))

    def test_all_endpoints_failing_fails_the_step_without_the_proxy(self):
        result = self.run_step(working=[])

        self.assert_proxy_not_exported(result)
        self.assertEqual(self.state_lines("started"), ENDPOINTS)
        self.assertFalse(any(alive(pid) for pid in self.pids()))
        # The last wireproxy log is dumped for debugging
        self.assertIn(f"wireproxy log for {ENDPOINTS[-1]}", result.stdout)

    def test_wireproxy_that_already_exited_does_not_stop_the_fallback(self):
        result = self.run_step(working=[ENDPOINTS[1]], crash=[ENDPOINTS[0]])

        self.assert_proxy_exported(result)
        self.assertEqual(self.state_lines("started"), ENDPOINTS[:2])

    def test_proxy_not_exported_when_the_trace_says_warp_is_off(self):
        result = self.run_step(working=[ENDPOINTS[0]], warp="off")

        self.assert_proxy_not_exported(result)

    def test_unexpected_profile_format_fails_before_starting_wireproxy(self):
        result = self.run_step(working=ENDPOINTS, endpoint_line="Endpoint=engage.cloudflareclient.com:2408")

        self.assert_proxy_not_exported(result)
        self.assertEqual(self.state_lines("started"), [])


class StepConfigTest(unittest.TestCase):
    def test_endpoint_list_matches_the_tested_fallback_order(self):
        loop = re.search(r"^for endpoint in (.+); do$", STEP_SCRIPT, re.M)
        self.assertIsNotNone(loop)
        self.assertEqual(loop.group(1).split(), ENDPOINTS)

    def test_step_timeout_fits_every_endpoint_attempt(self):
        minutes = int(re.search(r"timeout-minutes:\s*(\d+)", STEP_HEADER).group(1))
        per_try = int(re.search(r"timeout (\d+) bash -c", STEP_SCRIPT).group(1))
        # Leave at least 30s for downloads, wgcf registration and the kills between attempts
        self.assertLessEqual(per_try * len(ENDPOINTS) + 30, minutes * 60)

    def test_step_failure_does_not_stop_the_bot(self):
        self.assertIn("continue-on-error: true", STEP_HEADER)


if __name__ == "__main__":
    unittest.main()
