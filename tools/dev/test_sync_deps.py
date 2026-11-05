"""Self-check for sync-deps.sh.

Run directly: python3 -I tools/dev/test_sync_deps.py
Stdlib only: the repo root has no Python venv, so the check must run with nothing
installed.

Every test builds a throwaway git repo in a temp dir, copies the real script into
it and runs it with fake `uv` and `npm` first on PATH. The fakes log their argv and
fail on request; the fake `npm ci` creates node_modules. Git is the real one, with
no network. PATH holds only the fakes and a directory of symlinks to the system
tools the script and git need, so a uv or npm installed on this machine can't leak
in, and "uv or npm is missing" is testable.

The shell that runs the script is the first `bash` on PATH; set
SYNC_DEPS_TEST_BASH to test another one (macOS ships 3.2 as /bin/bash).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync-deps.sh"

SYSTEM_TOOLS = (
    "git bash sh env cat mkdir tr rm sed dirname basename uname awk grep sort cut "
    "head tail mktemp cp mv ln ls expr readlink date sleep chmod touch id hostname "
    "true false test"
).split()

FAKE_UV = """\
#!/bin/sh
echo "uv $* @${PWD##*/}" >>"$FAKE_LOG"
[ "$1" = sync ] || exit 2
case " ${FAKE_UV_FAIL:-} " in *" ${PWD##*/} "*) exit 1 ;; esac
"""

FAKE_NPM = """\
#!/bin/sh
echo "npm $* @${PWD##*/}" >>"$FAKE_LOG"
[ "$1" = ci ] || exit 2
[ -z "${FAKE_NPM_FAIL:-}" ] || exit 1
mkdir -p node_modules
"""

GITCONFIG = """\
[user]
	name = Test
	email = test@example.com
[init]
	defaultBranch = main
[commit]
	gpgsign = false
"""

SYNC_BACKEND = "uv sync --locked @backend"
SYNC_TAX = "uv sync --locked @tax"
NPM = "npm ci --no-audit --no-fund @frontend"
EVERYTHING = [SYNC_BACKEND, SYNC_TAX, NPM]
UV_ONLY = [SYNC_BACKEND, SYNC_TAX]

Expect = Callable[[str, object, object], None]


@dataclass
class Result:
    code: int
    out: str
    err: str
    calls: list[str]

    @property
    def failures(self) -> list[str]:
        return [line for line in self.err.splitlines() if line.startswith("sync-deps: failed")]


class Tools:
    """PATH directories shared by every test, built once."""

    def __init__(self, base: Path) -> None:
        self.system = base / "system"
        self.system.mkdir()
        bash = os.environ.get("SYNC_DEPS_TEST_BASH") or shutil.which("bash")
        if not bash:
            raise SystemExit("no bash on PATH; set SYNC_DEPS_TEST_BASH")
        for name in SYSTEM_TOOLS:
            found = bash if name == "bash" else shutil.which(name)
            if found:
                (self.system / name).symlink_to(found)
        self.bash = bash
        self.bash_version = subprocess.run(
            [bash, "-c", "echo $BASH_VERSION"], capture_output=True, text=True, check=True
        ).stdout.strip()
        git = shutil.which("git")
        if not git:
            raise SystemExit("no git on PATH")
        self.git_exec_path = subprocess.run(
            [git, "--exec-path"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.gitconfig = base / "gitconfig"
        self.gitconfig.write_text(GITCONFIG)
        self.fakes = {
            missing: self._fakes(base / ("fakes-without-" + "-".join(missing or ("nothing",))), missing)
            for missing in ((), ("npm",), ("uv",), ("npm", "uv"))
        }

    @staticmethod
    def _fakes(directory: Path, missing: Iterable[str]) -> Path:
        directory.mkdir()
        for name, body in (("uv", FAKE_UV), ("npm", FAKE_NPM)):
            if name not in missing:
                write_executable(directory / name, body)
        return directory


def write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Sandbox:
    """A throwaway repo laid out like the dependency inputs: backend, extras/tax, frontend."""

    def __init__(self, tmp: Path, tools: Tools) -> None:
        self.tmp = tmp
        self.tools = tools
        self.root = tmp / "repo"
        self.log = tmp / "calls.log"
        self.root.mkdir()
        self.git("init", "-q")
        self.write(".gitignore", "node_modules/\n")
        self.write("backend/pyproject.toml", "")
        self.write("extras/tax/pyproject.toml", "")
        self.write("frontend/package.json", '{"name": "x"}\n')
        self.write("frontend/package-lock.json", '{"lockfileVersion": 3}\n')
        (self.root / "tools/dev").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "tools/dev/sync-deps.sh")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def env(self, missing: Iterable[str] = (), extra: dict[str, str] | None = None) -> dict[str, str]:
        fakes = self.tools.fakes[tuple(sorted(missing))]
        env = {
            "PATH": f"{fakes}{os.pathsep}{self.tools.system}",
            "HOME": str(self.tmp),
            "GIT_CONFIG_GLOBAL": str(self.tools.gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_EXEC_PATH": self.tools.git_exec_path,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "FAKE_LOG": str(self.log),
        }
        env.update(extra or {})
        return env

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run(
        self,
        cwd: Path | None = None,
        extra: dict[str, str] | None = None,
        missing: Iterable[str] = (),
        script: Path | None = None,
    ) -> Result:
        """Run the copied script the way a git hook does: the file itself, via its shebang."""
        self.log.write_text("")
        done = subprocess.run(
            [str(script or self.root / "tools/dev/sync-deps.sh")],
            cwd=cwd or self.root,
            env=self.env(missing, extra),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return Result(done.returncode, done.stdout, done.stderr, self.log.read_text().splitlines())


def test_file_checkout_skips(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(extra={"PRE_COMMIT_CHECKOUT_TYPE": "0"})
    expect("exit", result.code, 0)
    expect("calls", result.calls, [])
    expect("output", (result.out, result.err), ("", ""))
    expect("stamps", (sb.root / ".git/sync-deps").exists(), False)


def test_amend_skips(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(extra={"PRE_COMMIT_REWRITE_COMMAND": "amend"})
    expect("exit", result.code, 0)
    expect("calls", result.calls, [])


def test_rebase_and_branch_checkout_do_not_skip(sb: Sandbox, expect: Expect) -> None:
    expect("rebase calls", sb.run(extra={"PRE_COMMIT_REWRITE_COMMAND": "rebase"}).calls, EVERYTHING)
    expect("branch checkout calls", sb.run(extra={"PRE_COMMIT_CHECKOUT_TYPE": "1"}).calls, UV_ONLY)


def test_first_run_installs_everything(sb: Sandbox, expect: Expect) -> None:
    result = sb.run()
    expect("exit", result.code, 0)
    expect("calls", result.calls, EVERYTHING)
    expect("stderr", result.err, "")
    expect("node_modules", (sb.root / "frontend/node_modules").is_dir(), True)
    expect("stamp lives in the git dir", sorted(p.name for p in (sb.root / ".git/sync-deps").iterdir()), ["frontend"])


def test_second_run_only_syncs_uv(sb: Sandbox, expect: Expect) -> None:
    sb.run()
    result = sb.run()
    expect("exit", result.code, 0)
    expect("calls", result.calls, UV_ONLY)


def test_runs_from_a_subdirectory(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(cwd=sb.root / "backend")
    expect("exit", result.code, 0)
    expect("calls", result.calls, EVERYTHING)


def test_outside_a_repo_fails(sb: Sandbox, expect: Expect) -> None:
    plain = sb.tmp / "plain"
    plain.mkdir()
    result = sb.run(cwd=plain)
    expect("exit", result.code, 1)
    expect("calls", result.calls, [])


def make_npm_input_test(filename: str) -> Callable[[Sandbox, Expect], None]:
    def test(sb: Sandbox, expect: Expect) -> None:
        sb.run()
        target = sb.root / "frontend" / filename
        target.write_text(target.read_text() + "\n")
        result = sb.run()
        expect("exit", result.code, 0)
        expect("reinstalls", result.calls, EVERYTHING)
        expect("then only uv runs", sb.run().calls, UV_ONLY)

    test.__name__ = f"npm input: frontend/{filename}"
    return test


def test_missing_node_modules_reinstalls(sb: Sandbox, expect: Expect) -> None:
    sb.run()
    shutil.rmtree(sb.root / "frontend/node_modules")
    expect("calls", sb.run().calls, EVERYTHING)


def test_uv_failure_is_reported_and_other_steps_run(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(extra={"FAKE_UV_FAIL": "backend"})
    expect("exit", result.code, 1)
    expect("everything attempted", result.calls, EVERYTHING)
    expect("reported", result.failures, ["sync-deps: failed, run by hand: cd backend && uv sync --locked"])
    expect("retry", sb.run().calls, UV_ONLY)


def test_npm_failure_is_reported_and_retried(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(extra={"FAKE_NPM_FAIL": "1"})
    expect("exit", result.code, 1)
    expect("uv still runs", result.calls, EVERYTHING)
    expect("reported", result.failures, ["sync-deps: failed, run by hand: cd frontend && npm ci"])
    expect("no stamp", (sb.root / ".git/sync-deps/frontend").exists(), False)
    expect("retry", sb.run().calls, EVERYTHING)


def test_independent_failures_are_all_reported(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(extra={"FAKE_UV_FAIL": "backend tax", "FAKE_NPM_FAIL": "1"})
    expect("exit", result.code, 1)
    expect("everything attempted", result.calls, EVERYTHING)
    expect(
        "reported",
        result.failures,
        [
            "sync-deps: failed, run by hand: cd backend && uv sync --locked",
            "sync-deps: failed, run by hand: cd extras/tax && uv sync --locked",
            "sync-deps: failed, run by hand: cd frontend && npm ci",
        ],
    )


def test_missing_lockfile_fails(sb: Sandbox, expect: Expect) -> None:
    (sb.root / "frontend/package-lock.json").unlink()
    result = sb.run()
    expect("exit", result.code, 1)
    expect("npm skipped", result.calls, UV_ONLY)
    expect(
        "reported",
        result.failures,
        [
            "sync-deps: failed, run by hand: restore frontend/package.json and "
            "frontend/package-lock.json, then rerun tools/dev/sync-deps.sh"
        ],
    )


def test_missing_npm_fails(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(missing=("npm",))
    expect("exit", result.code, 1)
    expect("uv side still runs", result.calls, UV_ONLY)
    expect(
        "reported",
        result.failures,
        ["sync-deps: failed, run by hand: install Node.js, then rerun tools/dev/sync-deps.sh"],
    )


def test_missing_uv_fails(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(missing=("uv",))
    expect("exit", result.code, 1)
    expect("npm side still runs", result.calls, [NPM])
    expect(
        "reported",
        result.failures,
        ["sync-deps: failed, run by hand: install uv (https://docs.astral.sh/uv/), then rerun tools/dev/sync-deps.sh"],
    )


def test_missing_uv_and_npm_report_both(sb: Sandbox, expect: Expect) -> None:
    result = sb.run(missing=("npm", "uv"))
    expect("exit", result.code, 1)
    expect("no tool called", result.calls, [])
    expect("both reported", len(result.failures), 2)


def test_stamps_are_per_worktree(sb: Sandbox, expect: Expect) -> None:
    expect("main installs", sb.run().calls, EVERYTHING)
    worktree = sb.tmp / "wt"
    sb.git("worktree", "add", "-q", "-b", "other", str(worktree))
    script = worktree / "tools/dev/sync-deps.sh"
    expect("worktree installs", sb.run(cwd=worktree, script=script).calls, EVERYTHING)
    expect("main is untouched by it", sb.run().calls, UV_ONLY)
    expect("worktree is stamped", sb.run(cwd=worktree, script=script).calls, UV_ONLY)
    main_dir = sb.git("rev-parse", "--path-format=absolute", "--git-path", "sync-deps")
    wt_dir = sb.git("rev-parse", "--path-format=absolute", "--git-path", "sync-deps", cwd=worktree)
    expect("separate stamp dirs", main_dir != wt_dir, True)
    expect("both exist", (Path(main_dir).is_dir(), Path(wt_dir).is_dir()), (True, True))


TESTS: list[Callable[[Sandbox, Expect], None]] = [
    test_file_checkout_skips,
    test_amend_skips,
    test_rebase_and_branch_checkout_do_not_skip,
    test_first_run_installs_everything,
    test_second_run_only_syncs_uv,
    test_runs_from_a_subdirectory,
    test_outside_a_repo_fails,
    make_npm_input_test("package.json"),
    make_npm_input_test("package-lock.json"),
    test_missing_node_modules_reinstalls,
    test_uv_failure_is_reported_and_other_steps_run,
    test_npm_failure_is_reported_and_retried,
    test_independent_failures_are_all_reported,
    test_missing_lockfile_fails,
    test_missing_npm_fails,
    test_missing_uv_fails,
    test_missing_uv_and_npm_report_both,
    test_stamps_are_per_worktree,
]


def run_case(base: Path, tools: Tools, index: int, test: Callable[[Sandbox, Expect], None]) -> list[str]:
    """Run one test in its own directory and return what it found wrong."""
    found: list[str] = []

    def expect(label: str, actual: object, expected: object) -> None:
        if actual != expected:
            found.append(f"{label}: expected {expected!r}, got {actual!r}")

    case = base / f"case-{index}"
    case.mkdir()
    try:
        test(Sandbox(case, tools), expect)
    except Exception:  # noqa: BLE001 - report any setup or run error as a failure
        found.append("raised:\n" + traceback.format_exc())
    shutil.rmtree(case, ignore_errors=True)
    return found


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sync-deps-test-") as base_name:
        base = Path(base_name).resolve()
        tools = Tools(base)
        print(f"bash {tools.bash} ({tools.bash_version})")
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            results = list(pool.map(lambda item: run_case(base, tools, *item), enumerate(TESTS)))
        for test, found in zip(TESTS, results, strict=True):
            name = test.__name__.replace("test_", "", 1).replace("_", " ")
            print(("FAIL " if found else "ok   ") + name)
            failures.extend(f"{name}: {line}" for line in found)

    for failure in failures:
        print(f"FAIL {failure}")
    print(f"test_sync_deps: {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
