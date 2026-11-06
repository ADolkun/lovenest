"""Docker-only setup/pull-deploy checks: real temporary Git clones, fake Docker.

Run with `python3 -I tools/deploy/test_deploy.py`. No network or Docker daemon is
used. Optional real-prek chaining checks run when prek is available on PATH (or
LOVENEST_TEST_PREK names it). Set LOVENEST_TEST_BASH to exercise e.g. /bin/bash.
"""
from __future__ import annotations

import contextlib
import email.message
import fcntl
import io
import json
import os
import pty
import select
import signal
import re
import struct
import termios
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PREK = os.environ.get("LOVENEST_TEST_PREK") or shutil.which("prek")

FAKE_DOCKER = r'''
import json, os, pathlib, sys, time
state_path = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
values = {}
env_file = None
if '--env-file' in args:
    env_file = pathlib.Path(args[args.index('--env-file') + 1])
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
for key in ('COMPOSE_PROFILES', 'COMPOSE_PROJECT_NAME', 'FRONTEND_PORT', 'FRONTEND_URL'):
    if key in os.environ:
        values[key] = os.environ[key]
with open(state['log'], 'a') as log:
    log.write(json.dumps({'args': args, 'profiles': values.get('COMPOSE_PROFILES', ''),
                          'port': values.get('FRONTEND_PORT', '3000')}) + '\n')
if args == ['info']:
    sys.exit(1 if state.get('fail') == 'info' else 0)
if args[:2] == ['volume', 'ls']:
    if state.get('fail') == 'volumes': sys.exit(1)
    print('\n'.join(state.get('volumes', [])))
    sys.exit(0)
if args[:2] == ['image', 'inspect']:
    if state.get('missing_image'): sys.exit(1)
    print(state.get('image_revision', 'sha256:fixture-image'))
    sys.exit(0)
if args and args[0] == 'inspect':
    container = args[-1]
    if container.startswith('oneoff-'):
        service = container[len('oneoff-'):]
        print('True|' + service + '|' + state['oneoffs'][service] + '|none')
    else:
        service = container[len('container-'):].rsplit('-', 1)[0]
        status = 'exited' if state.get('stopped') and service == 'backend' else 'running'
        health = 'unhealthy' if state.get('unhealthy') else 'healthy'
        print('False|' + service + '|' + status + '|' + health)
    sys.exit(0)
if args[:2] == ['compose', 'version']:
    print('Docker Compose version test')
    sys.exit(0)
if not args or args[0] != 'compose': sys.exit(90)
args = args[1:]
while args and args[0] in ('--project-directory', '--env-file', '-f'):
    args = args[2:]
command, rest = args[0], args[1:]
services = ['backend', 'frontend', 'tax-planner', 'celery-worker', 'celery-beat', 'db', 'redis']
if 'agents' in values.get('COMPOSE_PROFILES', '').split(','): services.append('mcp-server')
if command == 'build':
    for i in range(state.get('noisy_build', 0)):
        print('fixture build detail ' + str(i))
for line in state.get('progress_lines', {}).get(command, []):
    print(line, flush=True)
time.sleep(state.get('delay_seconds', {}).get(command, 0))
if state.get('fail') == command:
    message = 'simulated ' + command + ' failure'
    if state.get('color_error'): message = '\x1b[31m' + message + '\x1b[0m'
    print(message, file=sys.stderr)
    sys.exit(7)
if command == 'config':
    if '--quiet' in rest: pass
    elif '--images' in rest:
        if state.get('fail') == 'images': sys.exit(7)
        print('fixture-backend\nfixture-frontend\nfixture-tax\nfixture-db\nfixture-redis')
    elif '--services' in rest:
        print('\n'.join(services))
    elif '--environment' in rest:
        for key, value in values.items(): print(key + '=' + value)
    else:
        project = values.get('COMPOSE_PROJECT_NAME', 'securo')
        # Actual Compose emits quoted YAML for string names such as "true".
        rendered = json.dumps(project) if project in ('true', 'false', 'null', '0') else project
        print('name: ' + rendered)
        print(json.dumps(values, sort_keys=True))
elif command == 'build':
    assert rest == ['backend', 'frontend', 'tax-planner']
    state.pop('missing_image', None)
    state_path.write_text(json.dumps(state))
    print('fixture images built once')
elif command == 'ps':
    if '--services' in rest:
        print('\n'.join(services[:-1] if state.get('stopped') else services))
    else:
        ids = ['container-' + s + '-' + str(state.get('container_revision', 1)) for s in services if s not in state.get('missing_services', [])]
        ids += ['oneoff-' + s for s in state.get('oneoffs', {})]
        print('\n'.join(ids))
elif command == 'up':
    assert '--no-build' in rest and '--build' not in rest and '--wait' in rest and '--wait-timeout' in rest
    assert '--profile' not in rest
    state.pop('missing_services', None)
    state.pop('stopped', None)
    state.pop('unhealthy', None)
    state_path.write_text(json.dumps(state))
    print('fixture stack started')
elif command == 'exec':
    assert rest[:4] == ['-T', 'backend', 'python', '-']
    if state.get('mutate_env_on_exec'):
        with env_file.open('a') as out: out.write('\nFRONTEND_PORT=9191\n')
    if state.get('exec_fail_once'):
        state.pop('exec_fail_once')
        state_path.write_text(json.dumps(state))
        sys.exit(7)
    probe = sys.stdin.read()
    assert 'http://frontend:8080/api/health' in probe
    assert 'http://tax-planner:8088/' in probe
    print('fixture HTTP checks passed')
elif command == 'port':
    print('0.0.0.0:' + values.get('FRONTEND_PORT', '3000'))
else: sys.exit(91)
'''


class ReadinessProbeTests(unittest.TestCase):
    """Run the actual container heredoc with urllib's real redirect machinery."""

    def probe(self, frontend_url, *, force_redirect=False, public_healthy=False):
        source = (ROOT / "tools/deploy/deploy.sh").read_text()
        probe = source.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\nPY\n", 1)[0]
        calls = []
        canonical = urllib.parse.urlsplit(frontend_url).hostname

        class Transport(urllib.request.BaseHandler):
            handler_order = 100

            def http_open(self, request):
                calls.append((request.full_url, request.get_header("Host")))
                host = urllib.parse.urlsplit(request.full_url).hostname
                headers = email.message.Message()
                status = 200
                if host == "frontend":
                    if force_redirect or (canonical not in {"localhost", "127.0.0.1"} and request.get_header("Host") != canonical):
                        headers["Location"] = frontend_url + urllib.parse.urlsplit(request.full_url).path
                        status = 308
                elif host != "tax-planner":
                    if not public_healthy:
                        raise urllib.error.URLError("public endpoint unavailable")
                response = urllib.response.addinfourl(io.BytesIO(b"fixture"), headers, request.full_url, status)
                response.msg = "OK" if status == 200 else "Permanent Redirect"
                return response

            https_open = http_open

        build_opener = urllib.request.build_opener
        with (
            mock.patch.dict(os.environ, {"FRONTEND_URL": frontend_url}),
            mock.patch("urllib.request.build_opener", side_effect=lambda *handlers: build_opener(*handlers, Transport())),
            mock.patch("time.monotonic", side_effect=[0, 0, 61]),
            mock.patch("time.sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                exec(compile(probe, "deploy-readiness-heredoc", "exec"), {})
            except SystemExit as error:
                return calls, str(error)
        return calls, None

    def test_canonical_host_probes_stay_internal_without_public_access(self):
        for url in ("https://finance.example.test", "http://localhost:3132", "http://127.0.0.1:3132"):
            for public_healthy in (False, True):
                with self.subTest(url=url, public_healthy=public_healthy):
                    calls, error = self.probe(url, public_healthy=public_healthy)
                    self.assertIsNone(error)
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(all(urllib.parse.urlsplit(url).hostname in {"frontend", "tax-planner"} for url, _ in calls))
                    expected = urllib.parse.urlsplit(url).hostname
                    self.assertEqual([host for target, host in calls if urllib.parse.urlsplit(target).hostname == "frontend"], [expected, expected])

    def test_redirect_cannot_validate_a_wrong_public_instance(self):
        calls, error = self.probe("https://finance.example.test", force_redirect=True, public_healthy=True)
        self.assertIn("Not ready:", error)
        self.assertTrue(all(urllib.parse.urlsplit(url).hostname in {"frontend", "tax-planner"} for url, _ in calls))



class OutputTests(unittest.TestCase):
    def output(self, extra_env=None, tty=False, lose_size_before_phase=False):
        with tempfile.TemporaryDirectory(prefix="lovenest-output-test-") as directory:
            env = dict(os.environ)
            for name in ("CI", "NO_COLOR", "TERM", "LOVENEST_PROGRESS"):
                env.pop(name, None)
            env.update({"TERM": "xterm", **(extra_env or {})})
            # Freeze Bash's special clock for formatting-only assertions. The
            # integration test below separately observes real-time updates.
            script = 'exec 3>&1; source "$1"; log="$2"; unset SECONDS; SECONDS=0; output_init; '
            if lose_size_before_phase:
                script += 'stty cols 0 <&3; '
            script += 'phase_begin "Build images" build; SECONDS=2; phase_end; output_total OK'
            command = [os.environ.get("LOVENEST_TEST_BASH", "bash"), "-c", script, "fixture", str(ROOT / "tools/deploy/output.sh"), str(Path(directory) / "run.log")]
            if not tty:
                return subprocess.run(command, env=env, capture_output=True, text=True, check=True).stdout
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            process = subprocess.Popen(command, env=env, stdout=slave, stderr=slave)
            os.close(slave)
            chunks = []
            try:
                while True:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(master)
            self.assertEqual(process.wait(timeout=10), 0)
            return b"".join(chunks).decode()

    def test_pipe_is_plain_and_accessible_even_with_terminal_environment(self):
        text = self.output()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertIn("Build images", text)
        self.assertIn("OK 2s", text)

    def test_color_is_optional_and_tty_only(self):
        self.assertIn("\x1b[", self.output(tty=True))
        for env in ({"NO_COLOR": "1"}, {"NO_COLOR": ""}, {"CI": "true"}, {"TERM": "dumb"}, {"LOVENEST_PROGRESS": "plain"}):
            with self.subTest(env=env):
                text = self.output(env, tty=True)
                self.assertNotIn("\x1b", text)
                self.assertIn("OK 2s", text)


    def animation(self, ending="success", resize=False, width=20):
        with tempfile.TemporaryDirectory(prefix="lovenest-animation-test-") as directory:
            env = dict(os.environ, TERM="xterm")
            for name in ("CI", "NO_COLOR", "LOVENEST_PROGRESS"):
                env.pop(name, None)
            script = r'''set -euo pipefail
exec 3>&1
source "$1"
log="$2"
SECONDS=0
output_init
cleanup() { result=$?; progress_stop; [[ -z "$phase" ]] || phase_end FAILED; return "$result"; }
trap cleanup EXIT
trap 'exit 143' TERM
phase_begin 'Build images' build
printf '#1 [backend] exporting to image\n#1 0.1 SECRET raw output\n' >> "$log"
sleep 1.6
if [[ "$3" == failure ]]; then exit 7; fi
phase_end
printf 'complete\n'
'''
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            before = termios.tcgetattr(slave)
            command = [os.environ.get("LOVENEST_TEST_BASH", "bash"), "-c", script, "fixture", str(ROOT / "tools/deploy/output.sh"), str(Path(directory) / "run.log"), ending]
            process = subprocess.Popen(command, env=env, stdout=slave, stderr=slave)
            started = time.monotonic()
            chunks = []
            resized = signaled = False
            try:
                while True:
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 5, "Animation/cleanup timed out")
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            chunk = os.read(master, 8192)
                        except OSError:
                            break
                        if not chunk:
                            break
                        chunks.append((elapsed, chunk))
                    if resize and elapsed >= 0.55 and not resized:
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
                        resized = True
                    if ending == "signal" and elapsed >= 0.55 and not signaled:
                        process.send_signal(signal.SIGTERM)
                        signaled = True
                    if process.poll() is not None:
                        # Parent holds the slave open to inspect unchanged modes;
                        # drain any remaining output rather than waiting for EOF.
                        while select.select([master], [], [], 0)[0]:
                            chunks.append((elapsed, os.read(master, 8192)))
                        break
                self.assertEqual(termios.tcgetattr(slave), before)
                self.assertEqual(process.wait(timeout=2), {"success": 0, "failure": 7, "signal": 143}[ending])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                os.close(slave)
            text = b"".join(chunk for _, chunk in chunks).decode()
            return text, chunks

    def test_tty_bar_moves_without_fake_percentage_and_resizes(self):
        text, chunks = self.animation(resize=True)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        bars = re.findall(r"\[([ =]{12})\]", plain)
        self.assertGreaterEqual(len(set(bars)), 2)
        self.assertTrue(all(bar.count("=") == 3 for bar in bars))
        self.assertNotIn("%", text)
        self.assertNotIn("SECRET", text)
        self.assertIn("backend: exporting image", plain)
        # Subsequent frames fit a resized narrow terminal and use a spinner.
        later = b"".join(chunk for elapsed, chunk in chunks if elapsed >= 0.9).decode()
        later = re.sub(r"\x1b\[[0-9;]*m", "", later)
        frames = re.findall(r"\r\x1b\[2K([^\r\n]*)", later)
        spinner_frames = [frame for frame in frames if re.fullmatch(r"[|/\\-] [0-9]+s", frame)]
        self.assertTrue(spinner_frames, frames)
        self.assertTrue(all(len(frame) < 20 for frame in spinner_frames))
        self.assertIn("OK", plain)
        self.assertTrue(plain.endswith("complete\r\n"), repr(plain[-100:]))
        self.assertNotIn("\x1b", re.sub(r"\x1b\[2K", "", plain))

    def test_initial_draw_fallback_disables_color_before_phase_starts(self):
        text = self.output(tty=True, lose_size_before_phase=True)
        # The heading precedes capability loss. Everything after it is plain.
        body = text.split("\n", 1)[1]
        self.assertIn("RUNNING", body)
        self.assertIn("OK 2s", body)
        self.assertNotIn("\x1b", body)

    def test_lost_terminal_sizing_keeps_parent_completion_plain(self):
        text, chunks = self.animation(resize=True, width=0)
        later = b"".join(chunk for elapsed, chunk in chunks if elapsed >= 0.9).decode()
        self.assertIn("Build images", later)
        self.assertIn("OK", later)
        self.assertNotIn("\x1b", later)
        self.assertTrue(later.endswith("complete\r\n"), repr(later))

    def test_tty_animation_clears_on_failure_and_signal_without_changing_modes(self):
        for ending in ("failure", "signal"):
            with self.subTest(ending=ending):
                text, _ = self.animation(ending)
                plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
                self.assertIn("\r\x1b[2K  Build images          FAILED", plain)
                self.assertNotIn("OK", plain)
                self.assertNotIn("complete", plain)
                self.assertNotIn("\x1b", re.sub(r"\x1b\[2K", "", plain))

    def test_progress_activity_is_allowlisted_and_tracks_unfinished_steps(self):
        cases = [
            ("build", "#1 [backend stage-1 4/6] RUN uv sync --compile-bytecode --private-token=SECRET\n#2 [frontend] exporting to image\n#2 DONE 0.1s\n", "backend: installing dependencies and compiling bytecode"),
            ("build", "#1 [backend] exporting to image\n#1 unpacking to SECRET\n", "backend: unpacking image"),
            ("build", "#1 [backend] exporting to image\n#1 DONE 0.1s\n", "waiting for Docker build to finish"),
            ("build", "#1 [backend] exporting to image\n#1 CACHED\n", "waiting for Docker build to finish"),
            ("build", "#1 [SECRET] RUN SECRET\n\x1b[31mSECRET\x1b[0m\n", "waiting for Docker build to finish"),
            ("build", "#1 [backend]SECRET RUN uv sync\n", "waiting for Docker build to finish"),
            ("build", "#1 [frontend]\x1b[31mSECRET RUN npm ci\n", "waiting for Docker build to finish"),
            ("build", "#1 [tax-planner]SECRET\x07 RUN uv sync\n", "waiting for Docker build to finish"),
            ("start", " Container SECRET-backend-1 Waiting \n Container SECRET-db-1 Healthy \n", "backend: waiting for readiness"),
            ("start", " Container SECRET-backend-1 Waiting \n Container SECRET-backend-1 Healthy \n", "waiting for Docker startup/readiness checks"),
            ("start", " Container SECRET-celery-worker-1 Starting \n", "celery-worker: starting container"),
            ("start", "SECRET private application log\n", "waiting for Docker startup/readiness checks"),
        ]
        for mode, lines, expected in cases:
            with self.subTest(mode=mode, lines=lines):
                phase = "Build images" if mode == "build" else "Start + health"
                text = "#0 [backend] RUN uv sync --compile-bytecode\n[fixture] START " + phase + "\n" + lines
                result = subprocess.run(["awk", "-v", "mode=" + mode, "-f", str(ROOT / "tools/deploy/progress.awk")], input=text, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertNotIn("SECRET", result.stdout)
                self.assertNotIn("\x1b", result.stdout)

    def test_progress_cleanup_is_prompt_on_success_failure_and_signal(self):
        for ending in ("phase_end", "exit 7", "kill -TERM $$"):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory(prefix="lovenest-progress-cleanup-") as directory:
                pid_file = Path(directory) / "pid"
                script = 'set -euo pipefail; exec 3>&1; source "$1"; log="$2"; SECONDS=0; output_init; trap progress_stop EXIT; trap "exit 143" TERM; phase_begin "Build images" build; printf "%s\n" "$progress_pid" > "$3"; ' + ending
                start = time.monotonic()
                result = subprocess.run([os.environ.get("LOVENEST_TEST_BASH", "bash"), "-c", script, "fixture", str(ROOT / "tools/deploy/output.sh"), str(Path(directory) / "run.log"), str(pid_file)], capture_output=True, text=True, timeout=4)
                self.assertLess(time.monotonic() - start, 4)
                self.assertEqual(result.returncode, 0 if ending == "phase_end" else 7 if ending == "exit 7" else 143)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
                self.assertNotIn("elapsed", result.stdout)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lovenest-setup-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.origin, self.repo = self.base / "origin", self.base / "checkout"
        self.origin.mkdir()
        binary = self.base / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
        docker.chmod(0o755)
        bash = os.environ.get("LOVENEST_TEST_BASH")
        if bash:
            (binary / "bash").symlink_to(bash)
        self.calls_file = self.base / "calls.jsonl"
        self.state_file = self.base / "docker.json"
        self.state = {"log": str(self.calls_file), "volumes": []}
        self.save_state()
        config = self.base / "gitconfig"
        config.write_text("[user]\nname=Fixture\nemail=fixture@example.invalid\n[commit]\ngpgsign=false\n")
        self.env = {
            **os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_STATE": str(self.state_file), "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1", "PREK_HOME": str(self.base / "prek-cache"),
        }
        for key in list(self.env):
            if key in {"SKIP", "COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME", "FRONTEND_PORT", "FRONTEND_URL", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}:
                self.env.pop(key)
        self.git("init", "-q", "-b", "lovenest", cwd=self.origin)
        shutil.copytree(ROOT / "tools/deploy", self.origin / "tools/deploy")
        shutil.copy2(ROOT / ".env.example", self.origin / ".env.example")
        shutil.copy2(ROOT / "docker-compose.prod.yml", self.origin / "docker-compose.prod.yml")
        (self.origin / ".gitignore").write_text(".env\n")
        (self.origin / "tracked").write_text("initial\n")
        dev = self.origin / "tools/dev"
        dev.mkdir()
        (dev / "git-hooks.yaml").write_text(
            "repos:\n  - repo: local\n    hooks:\n      - id: sync-deps\n"
            "        name: dependency-sync fixture\n        entry: sh tools/dev/sync.sh\n"
            "        language: system\n        always_run: true\n        pass_filenames: false\n"
            "        stages: [post-merge]\n"
        )
        (dev / "sync.sh").write_text('printf "sync\\n" >> "$(git rev-parse --git-common-dir)/sync-fixture.log"\n')
        self.git("add", ".", cwd=self.origin)
        self.git("commit", "-qm", "fixture", cwd=self.origin)
        self.git("clone", "-q", str(self.origin), str(self.repo), cwd=self.base)

    def save_state(self):
        self.state_file.write_text(json.dumps(self.state))

    def run_command(self, *args, cwd=None, expected=0, extra_env=None):
        result = subprocess.run(
            args, cwd=cwd or self.repo, env={**self.env, **(extra_env or {})},
            text=True, capture_output=True,
        )
        if expected is not None:
            self.assertEqual(result.returncode, expected, (args, result.stdout, result.stderr))
        return result

    def git(self, *args, **kwargs):
        return self.run_command("git", *args, **kwargs)

    def script(self, name, *args, **kwargs):
        return self.run_command(str(self.repo / "tools/deploy" / name), *args, **kwargs)

    def env_file(self, extra=""):
        text = "SECRET_KEY=fixture-real-secret\nPOSTGRES_PASSWORD=fixture-password\n" + extra
        (self.repo / ".env").write_text(text)
        return text

    def calls(self, command=None):
        if not self.calls_file.exists():
            return []
        rows = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        return [row for row in rows if command is None or command in row["args"]]

    def clear_calls(self):
        self.calls_file.unlink(missing_ok=True)

    def invoke_hook(self, **kwargs):
        return self.run_command(str(self.repo / ".git/hooks/post-merge"), "0", **kwargs)

    def advance_origin(self):
        (self.origin / "tracked").write_text("updated\n")
        self.git("commit", "-qam", "update", cwd=self.origin)

    def test_new_setup_generates_private_unique_secrets_and_starts(self):
        result = self.script("setup.sh")
        path = self.repo / ".env"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        values = dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#") and "=" in line)
        secrets = [values[key] for key in ("SECRET_KEY", "POSTGRES_PASSWORD", "AGENTS_MCP_JWT_SECRET")]
        self.assertEqual(len(set(secrets)), 3)
        for secret in secrets:
            self.assertRegex(secret, r"^[0-9a-f]{64}$")
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(len(self.calls("up")), 1)
        self.assertNotIn("agents", self.calls("up")[0]["profiles"])
        self.assertTrue((self.repo / ".git/lovenest-deploy/last-success").exists())

    def test_env_only_and_rerun_preserve_existing_bytes(self):
        self.script("setup.sh", "--env-only")
        before = (self.repo / ".env").read_bytes()
        self.assertEqual(self.calls(), [])
        self.script("setup.sh")
        self.assertEqual((self.repo / ".env").read_bytes(), before)

    def test_existing_settings_ports_profiles_and_no_shell_source(self):
        before = self.env_file("COMPOSE_PROFILES=agents\nAGENTS_ENABLED=true\nAGENTS_MCP_JWT_SECRET=fixture-agent-secret\nFRONTEND_PORT=3132\nBACKEND_PORT=8182\nFRONTEND_URL=https://finance.example.test\nUNUSED=$(touch should-not-exist)\n")
        result = self.script("setup.sh")
        self.assertEqual((self.repo / ".env").read_text(), before)
        self.assertFalse((self.repo / "should-not-exist").exists())
        self.assertEqual(self.calls("up")[0]["profiles"], "agents")
        self.assertIn("https://finance.example.test", result.stdout)
        self.assertNotIn("--profile", self.calls("up")[0]["args"])

    def test_custom_port_output_without_url_and_legacy_missing_password(self):
        (self.repo / ".env").write_text("SECRET_KEY=legacy-real-secret\nFRONTEND_PORT=3132\n")
        result = self.script("deploy.sh")
        self.assertIn("http://localhost:3132", result.stdout)

    def test_placeholder_secret_fails_without_overwriting_or_starting(self):
        before = "SECRET_KEY=change-me-in-production\n"
        (self.repo / ".env").write_text(before)
        result = self.script("setup.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY", result.stderr)
        self.assertEqual((self.repo / ".env").read_text(), before)
        self.assertEqual(self.calls("up"), [])

    def test_agent_secret_required_only_when_enabled(self):
        self.env_file("AGENTS_ENABLED=true\nCOMPOSE_PROFILES=agents\n")
        result = self.script("deploy.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGENTS_MCP_JWT_SECRET", result.stderr)
        self.assertEqual(self.calls("up"), [])

    def test_old_postgres_layout_refuses_start(self):
        self.env_file()
        self.state["volumes"] = ["securo_pgdata"]
        self.save_state()
        result = self.script("deploy.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("postgres-upgrade.md", result.stderr)
        self.assertEqual(self.calls("up"), [])
        self.assertFalse((self.repo / ".git/lovenest-deploy/last-success").exists())

    def test_completed_postgres_upgrade_is_allowed(self):
        self.env_file()
        self.state["volumes"] = ["securo_pgdata", "securo_postgres-data"]
        self.save_state()
        self.script("deploy.sh")

    def test_old_postgres_layout_uses_quoted_and_custom_project_names(self):
        for project in ("true", "custom-household"):
            with self.subTest(project=project):
                self.env_file(f"COMPOSE_PROJECT_NAME={project}\n")
                self.state["volumes"] = [f"{project}_pgdata"]
                self.save_state()
                self.clear_calls()
                result = self.script("deploy.sh", expected=None)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("postgres-upgrade.md", result.stderr)
                self.assertEqual(self.calls("up"), [])

    def test_failed_build_or_readiness_never_reports_success(self):
        self.env_file()
        for failed in ("build", "up", "exec"):
            with self.subTest(failed=failed):
                self.state["fail"] = failed
                self.save_state()
                result = self.script("deploy.sh", expected=None)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Ready:", result.stdout)
                self.assertFalse((self.repo / ".git/lovenest-deploy/last-success").exists())
                self.assertFalse((self.repo / ".git/lovenest-deploy.lock").exists())

    def test_volume_inspection_failure_cannot_start(self):
        self.env_file()
        self.state["fail"] = "volumes"
        self.save_state()
        result = self.script("deploy.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("up"), [])

    def test_default_off_and_real_fast_forward_pull(self):
        self.env_file()
        self.script("post-merge.sh")
        self.assertEqual(self.calls(), [])
        self.script("install-pull-hook.sh", "enable")
        self.assertEqual(self.calls(), [])
        self.advance_origin()
        self.git("pull", "--ff-only")
        self.assertEqual(len(self.calls("up")), 1)
        self.clear_calls()
        self.git("pull", "--ff-only")
        self.assertEqual(self.calls(), [])

    def test_skips_other_branch_detached_dirty_staged_and_untracked(self):
        self.env_file()
        self.script("install-pull-hook.sh", "enable")
        self.git("switch", "-qc", "feature")
        self.invoke_hook()
        self.git("switch", "-q", "--detach")
        self.invoke_hook()
        self.git("switch", "-q", "lovenest")
        (self.repo / "tracked").write_text("dirty\n")
        self.invoke_hook()
        self.git("add", "tracked")
        self.invoke_hook()
        self.git("restore", "--staged", "tracked")
        self.git("restore", "tracked")
        (self.repo / "untracked").write_text("new\n")
        self.invoke_hook()
        self.assertEqual(self.calls(), [])

    def test_configured_branch_skip_and_disable(self):
        self.env_file()
        self.script("install-pull-hook.sh", "enable", "feature")
        self.invoke_hook()
        self.git("switch", "-qc", "feature")
        self.invoke_hook(extra_env={"SKIP": "other,deploy-lovenest"})
        self.script("install-pull-hook.sh", "disable")
        self.invoke_hook()
        self.assertEqual(self.calls(), [])

    def test_linked_worktree_cannot_deploy_or_enable(self):
        self.env_file()
        self.script("install-pull-hook.sh", "enable")
        self.git("switch", "-qc", "feature")
        worktree = self.base / "worktree"
        self.git("worktree", "add", "-q", str(worktree), "lovenest")
        self.invoke_hook(cwd=worktree)
        for name, arguments in (("deploy.sh", []), ("install-pull-hook.sh", ["enable"])):
            result = self.run_command(str(worktree / "tools/deploy" / name), *arguments, cwd=worktree, expected=None)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_install_is_idempotent_and_status_detects_missing_hook(self):
        self.script("install-pull-hook.sh", "enable")
        hook = self.repo / ".git/hooks/post-merge"
        before = hook.read_bytes()
        self.script("install-pull-hook.sh", "enable")
        self.assertEqual(hook.read_bytes(), before)
        result = self.script("install-pull-hook.sh", "status")
        self.assertIn("installed", result.stdout)
        hook.unlink()
        result = self.script("install-pull-hook.sh", "status", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stdout)

    def test_skip_works_when_tracked_guard_is_missing(self):
        self.script("install-pull-hook.sh", "enable")
        (self.repo / "tools/deploy/post-merge.sh").unlink()
        result = self.invoke_hook(expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tooling is missing", result.stderr)
        self.invoke_hook(extra_env={"SKIP": "other,deploy-lovenest"})
        self.assertEqual(self.calls(), [])

    def test_status_detects_moved_checkout_and_enable_updates_path(self):
        self.script("install-pull-hook.sh", "enable")
        moved = self.base / "moved-checkout"
        shutil.move(self.repo, moved)
        self.repo = moved
        result = self.script("install-pull-hook.sh", "status", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checkout path changed", result.stdout)
        self.script("install-pull-hook.sh", "enable")
        self.script("install-pull-hook.sh", "status")
        self.assertEqual(self.git("config", "--get", "lovenest.deployRoot").stdout.strip(), str(self.repo.resolve()))

    def test_custom_hooks_legacy_and_hookspath_are_preserved(self):
        hook = self.repo / ".git/hooks/post-merge"
        hook.write_text("#!/bin/sh\necho custom\n")
        before = hook.read_bytes()
        result = self.script("install-pull-hook.sh", "enable", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(hook.read_bytes(), before)
        hook.write_text("#!/bin/sh\n# File generated by prek: fixture\nexit 0\n")
        legacy = hook.with_name("post-merge.legacy")
        legacy.write_bytes(before)
        result = self.script("install-pull-hook.sh", "enable", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(legacy.read_bytes(), before)
        for value in ("", "custom-hooks"):
            with self.subTest(hooks_path=value):
                self.git("config", "core.hooksPath", value)
                result = self.script("install-pull-hook.sh", "enable", expected=None)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("core.hooksPath", result.stderr)
                self.assertEqual(legacy.read_bytes(), before)

    def test_enabled_status_reports_empty_or_nonempty_hooks_path_override(self):
        self.script("install-pull-hook.sh", "enable")
        hook = self.repo / ".git/hooks/post-merge"
        before = hook.read_bytes()
        for value in ("", "custom-hooks"):
            with self.subTest(hooks_path=value):
                self.git("config", "core.hooksPath", value)
                result = self.script("install-pull-hook.sh", "status", expected=None)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("core.hooksPath is configured", result.stdout)
                self.assertEqual(hook.read_bytes(), before)
        self.assertEqual(self.calls(), [])

    def test_overlapping_deploy_fails_and_preserves_receipt(self):
        self.env_file()
        self.script("deploy.sh")
        receipt = self.repo / ".git/lovenest-deploy/last-success"
        before = receipt.read_bytes()
        (self.repo / ".git/lovenest-deploy.lock").mkdir()
        self.clear_calls()
        result = self.script("deploy.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("up"), [])
        self.assertEqual(receipt.read_bytes(), before)


    def commit_file(self, path, content="changed\n"):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", "--", path)
        self.git("commit", "-qm", "fixture change")

    def baseline_for_skip(self):
        self.env_file()
        self.script("deploy.sh")
        self.commit_file("docs/example.md")
        self.clear_calls()

    def assert_redeploy(self):
        self.script("deploy.sh", "--if-needed")
        self.assertEqual(len(self.calls("build")), 1)
        self.assertEqual(len(self.calls("up")), 1)

    def test_concise_success_keeps_full_private_log_and_phase_timings(self):
        self.env_file()
        self.state["noisy_build"] = 100
        self.save_state()
        result = self.script("deploy.sh", extra_env={"TERM": "xterm", "CI": "true"})
        self.assertNotIn("fixture build detail", result.stdout + result.stderr)
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        self.assertLessEqual(len(result.stdout.splitlines()), 11)
        self.assertIn("Ready:", result.stdout)
        self.assertIn("total)", result.stdout)
        directory = self.repo / ".git/lovenest-deploy/logs"
        log = next(directory.glob("*.log"))
        text = log.read_text()
        self.assertIn("fixture build detail 0", text)
        self.assertIn("fixture build detail 99", text)
        self.assertIn("START Start + health", text)
        self.assertIn("END Start + health status=OK seconds=", text)
        timings = log.with_suffix(".timings.tsv")
        rows = [line.split("\t") for line in timings.read_text().splitlines()]
        self.assertEqual(rows[0], ["Phase", "Seconds", "Status"])
        self.assertEqual([row[0] for row in rows[1:]], ["Prepare", "Build images", "Start + health", "Check endpoints", "Verify deployment", "Total"])
        self.assertTrue(all(row[1].isdigit() and row[2] == "OK" for row in rows[1:]))
        self.assertLessEqual(sum(int(row[1]) for row in rows[1:-1]), int(rows[-1][1]))
        for path in (log, timings):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failure_is_actionable_bounded_and_preserves_full_output(self):
        self.env_file()
        self.state.update(noisy_build=100, fail="build", color_error=True)
        self.save_state()
        result = self.script("deploy.sh", expected=None, extra_env={"CI": "true"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Build images", result.stdout)
        self.assertIn("FAILED", result.stdout)
        self.assertIn("simulated build failure", result.stderr)
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        self.assertIn("no services were restarted", result.stderr)
        self.assertIn("Full log:", result.stderr)
        self.assertLess(len(result.stderr.splitlines()), 20)
        self.assertNotIn("Ready:", result.stdout)
        log = next((self.repo / ".git/lovenest-deploy/logs").glob("*.log"))
        self.assertIn("fixture build detail 0", log.read_text())
        self.assertIn("Build images\t", log.with_suffix(".timings.tsv").read_text())
        self.assertIn("FAILED", log.with_suffix(".timings.tsv").read_text())
        self.assertFalse((self.repo / ".git/lovenest-deploy.lock").exists())
        self.assertTrue((self.repo / ".git/lovenest-deploy/needs-deploy").exists())

    def test_build_and_start_progress_arrives_while_commands_are_running(self):
        self.env_file()
        self.state.update(
            delay_seconds={"build": 6, "up": 6},
            progress_lines={
                "build": ["#10 [backend stage-1 4/6] RUN uv sync --compile-bytecode --token=SECRET", "#10 0.1 SECRET private build output"],
                "up": [" Container SECRET-backend-1 Waiting ", "SECRET private application output"],
            },
        )
        self.save_state()
        started = time.monotonic()
        process = subprocess.Popen([str(self.repo / "tools/deploy/deploy.sh")], cwd=self.repo, env={**self.env, "NO_COLOR": "1", "CI": "true"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = []
        live = []
        try:
            pending = ""
            deadline = started + 20
            while True:
                readable, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                self.assertTrue(readable, "Deployment progress/cleanup timed out")
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                pending += chunk.decode()
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    lines.append(line + "\n")
                    if "RUNNING" in line or "elapsed |" in line:
                        live.append((line, time.monotonic() - started))
                        self.assertIsNone(process.poll(), "Progress must arrive before the deployment exits")
            self.assertEqual(process.wait(timeout=2), 0, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
        text = "".join(lines)
        self.assertEqual(sum("RUNNING" in line for line, _ in live), 2)
        self.assertEqual(sum("elapsed |" in line for line, _ in live), 2)
        self.assertLess(live[0][1], 4)
        self.assertIn("backend: installing dependencies and compiling bytecode", text)
        self.assertIn("backend: waiting for readiness", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("%", text)
        self.assertIn("Ready:", text)
        self.assertLess(time.monotonic() - started, 20)
        log = next((self.repo / ".git/lovenest-deploy/logs").glob("*.log"))
        self.assertIn("SECRET private build output", log.read_text())

    def test_dependency_bytecode_stays_in_the_cached_dependency_layer(self):
        dockerfile = (ROOT / "backend/Dockerfile").read_text()
        dependencies, project = dockerfile.split("COPY . .", 1)
        self.assertIn("uv sync --locked --no-dev --no-install-project --compile-bytecode", dependencies)
        self.assertNotIn("--compile-bytecode", project)
        self.assertNotIn("compileall", project)
        self.assertIn("uv sync --locked --no-dev", project)

    def test_startup_polling_does_not_relax_health_guarantees(self):
        compose = (ROOT / "docker-compose.prod.yml").read_text()
        backend = compose.split("  backend:\n", 1)[1].split("  tax-planner:\n", 1)[0]
        for expected in ("alembic upgrade head && exec uvicorn", "interval: 5s", "timeout: 6s", "start_period: 30s", "start_interval: 1s", "retries: 12"):
            self.assertIn(expected, backend)
        self.assertNotIn("stop_grace_period:", backend)

    def test_builds_three_unique_images_then_starts_without_building(self):
        self.env_file()
        self.script("deploy.sh")
        build = self.calls("build")
        self.assertEqual(len(build), 1)
        self.assertEqual(build[0]["args"][-4:], ["build", "backend", "frontend", "tax-planner"])
        self.assertLess(self.calls().index(build[0]), self.calls().index(self.calls("up")[0]))
        self.assertIn("--no-build", self.calls("up")[0]["args"])

    def test_docs_and_ci_skip_build_and_start_but_check_readiness(self):
        self.baseline_for_skip()
        self.commit_file(".github/workflows/ci.yml")
        result = self.script("deploy.sh", "--if-needed")
        self.assertIn("Pull deployment skipped", result.stdout)
        self.assertEqual(self.calls("build"), [])
        self.assertEqual(self.calls("up"), [])
        self.assertEqual(len(self.calls("exec")), 1)

    def test_fast_forward_hook_uses_skip_and_preserves_deployed_revision(self):
        self.env_file()
        self.script("deploy.sh")
        receipt = self.repo / ".git/lovenest-deploy/last-success"
        before = receipt.read_bytes()
        self.script("install-pull-hook.sh", "enable")
        docs = self.origin / "docs/example.md"
        docs.parent.mkdir()
        docs.write_text("docs only\n")
        self.git("add", ".", cwd=self.origin)
        self.git("commit", "-qm", "docs", cwd=self.origin)
        self.clear_calls()
        result = self.git("pull", "--ff-only")
        self.assertIn("Pull deployment skipped", result.stdout + result.stderr)
        self.assertEqual(self.calls("up"), [])
        self.assertEqual(receipt.read_bytes(), before)

    def test_manual_deploy_always_builds_even_for_docs(self):
        self.baseline_for_skip()
        self.script("deploy.sh")
        self.assertEqual(len(self.calls("build")), 1)

    def test_first_run_and_legacy_receipt_do_not_skip(self):
        self.env_file()
        self.assert_redeploy()
        (self.repo / ".git/lovenest-deploy/last-success-state").unlink()
        self.commit_file("docs/example.md")
        self.clear_calls()
        self.assert_redeploy()

    def test_runtime_and_unknown_files_cannot_skip(self):
        self.baseline_for_skip()
        for path in ("backend/app/new.py", "frontend/src/new.ts", "extras/tax/app.py", "new-unknown-file", "tools/deploy/new.sh", "backend/.dockerignore"):
            with self.subTest(path=path):
                self.commit_file(path)
                self.clear_calls()
                self.assert_redeploy()

    def test_runtime_rename_to_docs_is_not_mistaken_for_docs_only(self):
        self.baseline_for_skip()
        self.git("mv", "tracked", "docs/renamed.md")
        self.git("commit", "-qm", "rename")
        self.assert_redeploy()

    def test_config_env_override_and_profiles_cannot_skip(self):
        self.baseline_for_skip()
        self.env_file("FRONTEND_PORT=3132\n")
        self.assert_redeploy()
        self.clear_calls()
        self.script("deploy.sh", "--if-needed", extra_env={"FRONTEND_PORT": "4142"})
        self.assertEqual(len(self.calls("up")), 1)
        self.clear_calls()
        self.env_file("COMPOSE_PROFILES=agents\nAGENTS_MCP_JWT_SECRET=fixture-agent-secret\n")
        self.assert_redeploy()
        self.assertEqual(self.calls("up")[0]["profiles"], "agents")

    def test_changed_compose_file_cannot_skip(self):
        self.baseline_for_skip()
        path = self.repo / "docker-compose.prod.yml"
        self.commit_file("docker-compose.prod.yml", path.read_text() + "\n# changed\n")
        self.assert_redeploy()

    def test_missing_or_retagged_image_and_replaced_container_cannot_skip(self):
        self.baseline_for_skip()
        for key, value in (("missing_image", True), ("image_revision", "sha256:replacement"), ("container_revision", 2)):
            with self.subTest(key=key):
                self.state = json.loads(self.state_file.read_text())
                self.state[key] = value
                self.save_state()
                self.clear_calls()
                self.assert_redeploy()

    def test_stopped_unhealthy_or_unready_stack_cannot_skip(self):
        self.baseline_for_skip()
        for key in ("stopped", "unhealthy", "exec_fail_once"):
            with self.subTest(key=key):
                self.state = json.loads(self.state_file.read_text())
                self.state[key] = True
                self.save_state()
                self.clear_calls()
                self.assert_redeploy()

    def test_oneoff_containers_are_ignored_without_being_removed(self):
        self.baseline_for_skip()
        for oneoffs in ({"backend": "exited"}, {"backend": "running", "frontend": "exited"}, {}):
            with self.subTest(oneoffs=oneoffs):
                self.state = json.loads(self.state_file.read_text())
                self.state["oneoffs"] = oneoffs
                self.save_state()
                self.clear_calls()
                result = self.script("deploy.sh", "--if-needed")
                self.assertIn("Pull deployment skipped", result.stdout)
                self.assertEqual(self.calls("build"), [])
                self.assertEqual(self.calls("up"), [])
                self.assertEqual(json.loads(self.state_file.read_text())["oneoffs"], oneoffs)
                self.assertFalse(any("rm" in row["args"] for row in self.calls()))
        # Stopped `compose run` leftovers also cannot fail a forced deployment.
        self.state["oneoffs"] = {"backend": "exited"}
        self.save_state()
        self.script("deploy.sh")
        self.assertEqual(json.loads(self.state_file.read_text())["oneoffs"], {"backend": "exited"})

    def test_running_oneoff_cannot_substitute_for_missing_service(self):
        self.baseline_for_skip()
        self.state["oneoffs"] = {"backend": "running"}
        self.state["missing_services"] = ["backend"]
        self.save_state()
        self.assert_redeploy()
        self.assertEqual(json.loads(self.state_file.read_text())["oneoffs"], {"backend": "running"})

    def test_failure_after_previous_success_requires_deploy_after_recovery(self):
        self.baseline_for_skip()
        receipt = self.repo / ".git/lovenest-deploy/last-success"
        before = receipt.read_bytes()
        self.state["fail"] = "up"
        self.save_state()
        result = self.script("deploy.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt.read_bytes(), before)
        marker = self.repo / ".git/lovenest-deploy/needs-deploy"
        self.assertTrue(marker.exists())
        self.state.pop("fail")
        self.save_state()
        self.clear_calls()
        self.assert_redeploy()
        self.assertFalse(marker.exists())

    def test_dirty_success_and_missing_or_invalid_ancestor_cannot_skip(self):
        self.baseline_for_skip()
        state = self.repo / ".git/lovenest-deploy"
        receipt = state / "last-success"
        original = receipt.read_text()
        receipt.write_text(original.replace("dirty=false", "dirty=true"))
        self.assert_redeploy()
        receipt.write_text("not-a-commit 2026-01-01T00:00:00Z dirty=false\n")
        self.clear_calls()
        self.assert_redeploy()
        self.git("checkout", "-q", "--orphan", "unrelated")
        self.git("commit", "-qm", "new history")
        self.clear_calls()
        self.assert_redeploy()

    def test_old_unknown_or_mismatched_state_schema_cannot_skip(self):
        self.baseline_for_skip()
        state = self.repo / ".git/lovenest-deploy/last-success-state"
        state.write_text(state.read_text().replace("schema=1", "schema=2"))
        self.assert_redeploy()
        state.write_text(state.read_text().replace("revision=", "revision=invalid"))
        self.clear_calls()
        self.assert_redeploy()

    def test_changed_config_during_deploy_never_records_success(self):
        self.env_file()
        self.state["mutate_env_on_exec"] = True
        self.save_state()
        result = self.script("deploy.sh", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration changed", result.stdout + result.stderr)
        self.assertFalse((self.repo / ".git/lovenest-deploy/last-success").exists())

    def test_receipts_do_not_contain_secrets(self):
        self.env_file()
        self.script("deploy.sh")
        for path in (self.repo / ".git/lovenest-deploy").glob("last-success*"):
            self.assertNotIn("fixture-real-secret", path.read_text())
            self.assertNotIn("fixture-password", path.read_text())

    @unittest.skipUnless(PREK, "optional real-prek check: install prek or set LOVENEST_TEST_PREK")
    def test_real_prek_install_preserves_native_hook_and_reinstall(self):
        self.env_file()
        self.script("install-pull-hook.sh", "enable")
        for _ in range(2):
            self.run_command(PREK, "install", "--config", "tools/dev/git-hooks.yaml", "-t", "post-merge")
            self.invoke_hook()
        self.assertEqual(len(self.calls("up")), 1)
        self.assertEqual((self.repo / ".git/sync-fixture.log").read_text().splitlines(), ["sync", "sync"])
        self.script("install-pull-hook.sh", "status")

    @unittest.skipUnless(PREK, "optional real-prek check: install prek or set LOVENEST_TEST_PREK")
    def test_install_next_to_existing_prek_and_missing_companion_status(self):
        self.env_file()
        self.run_command(PREK, "install", "--config", "tools/dev/git-hooks.yaml", "-t", "post-merge")
        hook = self.repo / ".git/hooks/post-merge"
        before = hook.read_bytes()
        self.script("install-pull-hook.sh", "enable")
        self.assertEqual(hook.read_bytes(), before)
        self.invoke_hook()
        self.assertEqual(len(self.calls("up")), 1)
        (self.repo / ".git/hooks/post-merge.legacy").unlink()
        result = self.script("install-pull-hook.sh", "status", expected=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
