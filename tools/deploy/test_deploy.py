"""Docker-only setup/pull-deploy checks: real temporary Git clones, fake Docker.

Run with `python3 -I tools/deploy/test_deploy.py`. No network or Docker daemon is
used. Optional real-prek chaining checks run when prek is available on PATH (or
LOVENEST_TEST_PREK names it). Set LOVENEST_TEST_BASH to exercise e.g. /bin/bash.
"""
from __future__ import annotations

import contextlib
import email.message
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PREK = os.environ.get("LOVENEST_TEST_PREK") or shutil.which("prek")

FAKE_DOCKER = r'''
import json, os, pathlib, sys
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
if args[:2] == ['compose', 'version']:
    print('Docker Compose version test')
    sys.exit(0)
if not args or args[0] != 'compose': sys.exit(90)
args = args[1:]
while args and args[0] in ('--project-directory', '--env-file', '-f'):
    args = args[2:]
command, rest = args[0], args[1:]
if state.get('fail') == command:
    print('simulated ' + command + ' failure', file=sys.stderr)
    sys.exit(7)
if command == 'config':
    if '--quiet' in rest: pass
    elif '--environment' in rest:
        for key, value in values.items(): print(key + '=' + value)
    else:
        project = values.get('COMPOSE_PROJECT_NAME', 'securo')
        # Actual Compose emits quoted YAML for string names such as "true".
        rendered = json.dumps(project) if project in ('true', 'false', 'null', '0') else project
        print('name: ' + rendered)
elif command == 'up':
    assert '--build' in rest and '--wait' in rest and '--wait-timeout' in rest
    assert '--profile' not in rest
    print('fixture stack started')
elif command == 'exec':
    assert rest[:4] == ['-T', 'backend', 'python', '-']
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
        for failed in ("up", "exec"):
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

    @unittest.skipUnless(PREK, "optional real-prek check: install prek or set LOVENEST_TEST_PREK")
    def test_real_prek_install_preserves_native_hook_and_reinstall(self):
        self.env_file()
        self.script("install-pull-hook.sh", "enable")
        for _ in range(2):
            self.run_command(PREK, "install", "--config", "tools/dev/git-hooks.yaml", "-t", "post-merge")
            self.invoke_hook()
        self.assertEqual(len(self.calls("up")), 2)
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
