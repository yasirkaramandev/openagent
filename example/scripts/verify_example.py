#!/usr/bin/env python3
"""Verify the OpenAgent Task Board Example without network or credentials.

The verifier is standard-library-only.  Pytest and OpenAgent are invoked through
the selected Python interpreter and must already be installed in that
environment; this script never installs anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXAMPLE_ROOT.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src"
CANARY_NAME = "OPENAGENT_EXAMPLE_SECRET_CANARY"

REQUIRED_FILES = (
    "README.md",
    "pyproject.toml",
    ".gitignore",
    "OPENAGENT.md",
    "app/__init__.py",
    "app/models.py",
    "app/repository.py",
    "app/service.py",
    "app/cli.py",
    "tests/test_repository.py",
    "tests/test_service.py",
    "tests/test_cli.py",
    "prompts/01_inspect.md",
    "prompts/02_add_feature.md",
    "prompts/03_fix_bug.md",
    "prompts/04_resume.md",
    "expected/doctor.json.example",
    "expected/run-events.jsonl.example",
    "expected/resume-summary.json.example",
    "expected/schemas/doctor.schema.json",
    "expected/schemas/run-event.schema.json",
    "expected/schemas/resume-summary.schema.json",
    "scenarios/buggy/completion_guard.pyfrag",
    "scenarios/fixed/README.md",
    "scripts/setup_example.py",
    "scripts/setup_example.sh",
    "scripts/setup_example.ps1",
    "scripts/run_example.sh",
    "scripts/run_example.ps1",
    "scripts/resume_via_api.py",
    "scripts/verify_example.py",
)

POLICY_PHRASES = (
    "Do not modify files outside `/example`.",
    "Do not access the network",
    "Do not read, enumerate, print, copy, or modify environment secrets",
    "python -m pytest -q -p no:cacheprovider",
    "python -m ruff format --check app tests",
    "Do not run `rm -rf`",
)

README_COMMANDS = (
    "openagent doctor --json",
    "openagent agent add --name task-board-codex --cli codex --profile safe-edit",
    "openagent agent add --name task-board-claude --cli claude --profile safe-edit",
    "openagent provider add ollama-local --type ollama --no-key",
    "openagent provider probe ollama-local --model llama3.2 --json",
    "openagent run --name task-board-codex --prompt",
    "openagent runs --limit 5 --json",
    "openagent cancel --id RUN_ID",
    "openagent resume --id",
    "openagent output --id RUN_ID --format events",
)


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _short(text: str, limit: int = 700) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _is_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _snapshot_outside_example() -> dict[str, str]:
    """Hash repository files outside example, excluding tool-owned caches.

    `.coverage` and other root artifacts are deliberately included.  The
    verifier disables pytest caches and coverage, so changing one is a real
    containment failure rather than verifier noise.
    """

    ignored_directories = {
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "__pycache__",
    }
    snapshot: dict[str, str] = {}
    for current, directories, files in os.walk(REPOSITORY_ROOT, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if name not in ignored_directories
            and not _is_child((current_path / name).resolve(), EXAMPLE_ROOT)
        ]
        for name in files:
            path = current_path / name
            if _is_child(path.resolve(), EXAMPLE_ROOT):
                continue
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            if path.is_symlink():
                snapshot[relative] = "symlink:" + os.readlink(path)
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                mode = path.stat().st_mode & 0o777
            except OSError as exc:
                raise VerificationError(f"cannot snapshot {relative}: {exc}") from exc
            snapshot[relative] = f"file:{mode:o}:{digest}"
    return snapshot


GUARD_SOURCE = r"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path


def _resolved(raw):
    value = Path(os.fsdecode(raw))
    if not value.is_absolute():
        value = Path.cwd() / value
    return value.resolve(strict=False)


_allowed = tuple(
    _resolved(item)
    for item in os.environ.get("OPENAGENT_EXAMPLE_ALLOWED_WRITE_ROOTS", "").split(os.pathsep)
    if item
)


def _allow(raw):
    if os.path.normcase(os.fsdecode(raw)) == os.path.normcase(os.devnull):
        return
    candidate = _resolved(raw)
    for root in _allowed:
        try:
            candidate.relative_to(root)
            return
        except ValueError:
            pass
    raise PermissionError(f"example containment denied write outside allowed roots: {candidate}")


def _writing(mode, flags):
    if isinstance(mode, str) and any(character in mode for character in "wax+"):
        return True
    if isinstance(flags, int):
        mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & mask)
    return False


def _audit(event, args):
    if event == "open" and args and not isinstance(args[0], int):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if _writing(mode, flags):
            _allow(args[0])
    elif event in {
        "os.remove",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.utime",
        "os.symlink",
        "os.link",
    }:
        if args and not isinstance(args[0], int):
            _allow(args[0])
    elif event in {"os.rename", "os.replace"}:
        if len(args) >= 2:
            _allow(args[0])
            _allow(args[1])
    elif event in {"socket.connect", "socket.getaddrinfo"}:
        raise PermissionError("example verification denied network access")


sys.addaudithook(_audit)


def _deny_network(*_args, **_kwargs):
    raise PermissionError("example verification denied network access")


socket.socket.connect = _deny_network
socket.socket.connect_ex = _deny_network
socket.create_connection = _deny_network
socket.getaddrinfo = _deny_network


_real_popen = subprocess.Popen
_python = Path(sys.executable).resolve()


def _guarded_popen(args, *positional, **keywords):
    if keywords.get("shell"):
        raise PermissionError("example verification denied shell execution")
    if isinstance(args, (str, bytes, os.PathLike)) or not args:
        raise PermissionError("example verification requires an argument-vector Python command")
    executable = _resolved(args[0])
    if executable != _python:
        raise PermissionError(f"example verification denied non-Python command: {args[0]}")
    return _real_popen(args, *positional, **keywords)


subprocess.Popen = _guarded_popen


def _deny_shell(*_args, **_kwargs):
    raise PermissionError("example verification denied shell execution")


os.system = _deny_shell
os.popen = _deny_shell


_sensitive = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "PRIVATE_KEY", "CREDENTIAL")
_real_getitem = os._Environ.__getitem__


def _guarded_getitem(mapping, key):
    name = os.fsdecode(key).upper()
    if any(fragment in name for fragment in _sensitive):
        raise PermissionError(f"example verification denied secret environment access: {name}")
    return _real_getitem(mapping, key)


os._Environ.__getitem__ = _guarded_getitem
"""


class Verifier:
    def __init__(self, python: Path, work: Path) -> None:
        # Keep a virtual-environment launcher path intact. Resolving its symlink
        # to the base interpreter would silently drop the venv's site-packages.
        self.python = python.expanduser().absolute()
        self.work = work.resolve()
        self.guard = self.work / "guard"
        self.guard.mkdir(parents=True)
        (self.guard / "sitecustomize.py").write_text(GUARD_SOURCE, encoding="utf-8")
        self.temp = self.work / "tmp"
        self.home = self.work / "home"
        self.data = self.work / "openagent-data"
        self.config = self.work / "openagent-config"
        for directory in (self.temp, self.home, self.data, self.config):
            directory.mkdir()
        self.canary = "example-canary-" + uuid.uuid4().hex
        self.outputs: list[str] = []
        self.commands: list[tuple[str, ...]] = []
        self._pytest_index = 0

    def environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in (
            "PATH",
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "LANG",
            "LC_ALL",
            "TZ",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        python_paths = [self.guard, EXAMPLE_ROOT]
        if SOURCE_ROOT.is_dir():
            python_paths.append(SOURCE_ROOT)
        environment.update(
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "TMPDIR": str(self.temp),
                "TEMP": str(self.temp),
                "TMP": str(self.temp),
                "PYTHONPATH": os.pathsep.join(str(path) for path in python_paths),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
                "PYTHONHASHSEED": "0",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
                "COLUMNS": "120",
                "OPENAGENT_DATA_DIR": str(self.data),
                "OPENAGENT_CONFIG_DIR": str(self.config),
                "OPENAGENT_EXAMPLE_ALLOWED_WRITE_ROOTS": os.pathsep.join(
                    (str(EXAMPLE_ROOT), str(self.work))
                ),
                CANARY_NAME: self.canary,
            }
        )
        return environment

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path = EXAMPLE_ROOT,
        expected: set[int] | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        command = (str(self.python), *map(str, arguments))
        if Path(command[0]).absolute() != self.python:
            raise VerificationError("only the selected Python interpreter may be executed")
        self.commands.append(command)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=self.environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerificationError(
                f"command timed out after {timeout}s: {' '.join(command)}"
            ) from exc
        self.outputs.extend((completed.stdout, completed.stderr))
        accepted = expected if expected is not None else {0}
        if completed.returncode not in accepted:
            detail = _short(completed.stderr or completed.stdout or "no output")
            raise VerificationError(f"exit {completed.returncode}: {' '.join(command)} — {detail}")
        return CommandResult(command, completed.returncode, completed.stdout, completed.stderr)

    def pytest(
        self, cwd: Path, selection: Sequence[str] = (), *, expected: set[int] | None = None
    ) -> CommandResult:
        self._pytest_index += 1
        base_temp = self.work / f"pytest-{self._pytest_index}"
        return self.run(
            (
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-c",
                str(cwd / "pyproject.toml"),
                "--basetemp",
                str(base_temp),
                *selection,
            ),
            cwd=cwd,
            expected=expected,
        )


def _check_required_files(_verifier: Verifier) -> str:
    missing = [name for name in REQUIRED_FILES if not (EXAMPLE_ROOT / name).is_file()]
    if missing:
        raise VerificationError("missing required files: " + ", ".join(missing))
    return f"{len(REQUIRED_FILES)} required files present"


def _check_policy_and_readme(_verifier: Verifier) -> str:
    policy = (EXAMPLE_ROOT / "OPENAGENT.md").read_text(encoding="utf-8")
    missing_policy = [phrase for phrase in POLICY_PHRASES if phrase not in policy]
    if missing_policy:
        raise VerificationError("OPENAGENT.md missing policy: " + "; ".join(missing_policy))
    readme = (EXAMPLE_ROOT / "README.md").read_text(encoding="utf-8")
    missing_commands = [command for command in README_COMMANDS if command not in readme]
    if missing_commands:
        raise VerificationError("README missing exact CLI shape: " + "; ".join(missing_commands))
    return "workspace, test, formatting, shell, secret, and success policies documented"


def _type_matches(value: object, wanted: str) -> bool:
    if wanted == "object":
        return isinstance(value, dict)
    if wanted == "array":
        return isinstance(value, list)
    if wanted == "string":
        return isinstance(value, str)
    if wanted == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if wanted == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if wanted == "boolean":
        return isinstance(value, bool)
    if wanted == "null":
        return value is None
    raise VerificationError(f"unsupported schema type {wanted!r}")


def _validate(value: object, schema: Mapping[str, Any], location: str = "$") -> None:
    if "const" in schema and value != schema["const"]:
        raise VerificationError(f"{location}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise VerificationError(f"{location}: value is not in enum")
    wanted = schema.get("type")
    if isinstance(wanted, str) and not _type_matches(value, wanted):
        raise VerificationError(f"{location}: expected {wanted}, got {type(value).__name__}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        raise VerificationError(f"{location}: string is too short")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise VerificationError(f"{location}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise VerificationError(f"{location}: above maximum")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise VerificationError(f"{location}: schema required must be a string array")
        missing = [item for item in required if item not in value]
        if missing:
            raise VerificationError(f"{location}: missing {missing}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise VerificationError(f"{location}: schema properties must be an object")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value).difference(properties))
            if extra:
                raise VerificationError(f"{location}: unexpected properties {extra}")
        for name, child_schema in properties.items():
            if name in value:
                if not isinstance(child_schema, dict):
                    raise VerificationError(f"{location}.{name}: schema must be an object")
                _validate(value[name], child_schema, f"{location}.{name}")
    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        if not isinstance(item_schema, dict):
            raise VerificationError(f"{location}: items schema must be an object")
        for index, item in enumerate(value):
            _validate(item, item_schema, f"{location}[{index}]")


def _load_schema(name: str) -> dict[str, Any]:
    path = EXAMPLE_ROOT / "expected" / "schemas" / name
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise VerificationError(f"{path.name}: schema must be an object")
    if parsed.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise VerificationError(f"{path.name}: unsupported or missing $schema")
    if not isinstance(parsed.get("$id"), str) or not parsed["$id"]:
        raise VerificationError(f"{path.name}: missing $id")
    return parsed


def _check_expected_artifacts(_verifier: Verifier) -> str:
    doctor_schema = _load_schema("doctor.schema.json")
    event_schema = _load_schema("run-event.schema.json")
    resume_schema = _load_schema("resume-summary.schema.json")

    doctor = json.loads(
        (EXAMPLE_ROOT / "expected" / "doctor.json.example").read_text(encoding="utf-8")
    )
    resume = json.loads(
        (EXAMPLE_ROOT / "expected" / "resume-summary.json.example").read_text(encoding="utf-8")
    )
    _validate(doctor, doctor_schema)
    _validate(resume, resume_schema)

    events: list[dict[str, Any]] = []
    event_path = EXAMPLE_ROOT / "expected" / "run-events.jsonl.example"
    for line_number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VerificationError(f"run-events line {line_number} is invalid JSON") from exc
        _validate(event, event_schema, f"events[{line_number}]")
        events.append(event)
    sequences = [event["sequence"] for event in events]
    if sequences != sorted(set(sequences)):
        raise VerificationError("event sequences must be unique and increasing")
    event_types = {event["type"] for event in events}
    if not {"run.started", "run.cancelled", "turn.started", "run.completed"}.issubset(event_types):
        raise VerificationError("event example does not demonstrate cancel and resume")
    return f"3 schemas and {len(events) + 2} example records parsed and validated"


def _check_imports(verifier: Verifier) -> str:
    code = textwrap.dedent(
        """
        import app
        from app.cli import build_parser
        from app.models import Priority, Task
        from app.repository import TaskRepository
        from app.service import TaskBoardService

        assert app.Task is Task
        assert Priority.HIGH.value == "high"
        assert build_parser().prog == "task-board"
        assert TaskBoardService(TaskRepository("unused.json")).list() == []
        print("imports-ok")
        """
    )
    result = verifier.run(("-c", code))
    if result.stdout.strip() != "imports-ok":
        raise VerificationError("import smoke returned unexpected output")
    return "app models, repository, service, and CLI imported"


def _check_tests(verifier: Verifier) -> str:
    result = verifier.pytest(EXAMPLE_ROOT)
    if "passed" not in result.stdout:
        raise VerificationError("pytest did not report passing tests")
    return _short(result.stdout)


def _json_stdout(result: CommandResult) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"CLI did not emit JSON: {_short(result.stdout)}") from exc


def _check_task_cli(verifier: Verifier) -> str:
    board = verifier.work / "cli-smoke" / "board.json"
    board.parent.mkdir()
    prefix = ("-m", "app.cli", "--db", str(board))
    added = _json_stdout(
        verifier.run((*prefix, "add", "Verify the example", "--priority", "high", "--json"))
    )
    if added["task"]["id"] != 1 or added["task"]["priority"] != "high":
        raise VerificationError("add smoke returned the wrong task")
    listed = _json_stdout(verifier.run((*prefix, "list", "--json")))
    if len(listed["tasks"]) != 1:
        raise VerificationError("list smoke did not persist the task")
    first = _json_stdout(verifier.run((*prefix, "complete", "1", "--json")))
    repeated = _json_stdout(verifier.run((*prefix, "complete", "1", "--json")))
    summary = _json_stdout(verifier.run((*prefix, "summary", "--json")))
    if first["changed"] is not True or repeated["changed"] is not False:
        raise VerificationError("repeat completion is not idempotent")
    if summary != {"completed": 1, "pending": 0, "total": 1}:
        raise VerificationError("summary counter is inconsistent")
    return "add/list/complete/repeat/summary JSON smoke passed"


def _check_openagent_cli(verifier: Verifier) -> str:
    top = verifier.run(("-m", "openagent", "--help")).stdout
    for command in ("run", "cancel", "resume", "output", "doctor"):
        if command not in top:
            raise VerificationError(f"OpenAgent top-level help is missing {command}")

    help_requirements = {
        "run": ("--name", "--prompt", "--worktree", "--profile"),
        "cancel": ("--id", "--all-projects"),
        "resume": ("--id", "--prompt", "--all-projects"),
        "output": ("--id", "--format", "--all-projects"),
        "doctor": ("--json", "--refresh-cli-updates", "--flat"),
    }
    for command, flags in help_requirements.items():
        output = verifier.run(("-m", "openagent", command, "--help")).stdout
        for flag in flags:
            if flag not in output:
                raise VerificationError(f"openagent {command} --help is missing {flag}")
    helper = verifier.run((str(EXAMPLE_ROOT / "scripts" / "resume_via_api.py"), "--help"))
    if "--prompt-file" not in helper.stdout or "--id" not in helper.stdout:
        raise VerificationError("application API resume helper has incomplete help")
    return "top-level plus run/cancel/resume/output/doctor/API-resume help passed"


def _check_scenarios(verifier: Verifier) -> str:
    with tempfile.TemporaryDirectory(prefix=".verify-scenarios-", dir=EXAMPLE_ROOT) as raw:
        container = Path(raw)
        fixed = container / "fixed"
        buggy = container / "buggy"
        setup = str(EXAMPLE_ROOT / "scripts" / "setup_example.py")
        verifier.run((setup, "--scenario", "fixed", "--destination", str(fixed), "--json"))
        verifier.run((setup, "--scenario", "buggy", "--destination", str(buggy), "--json"))
        fixed_result = verifier.pytest(fixed)
        if "passed" not in fixed_result.stdout:
            raise VerificationError("generated fixed scenario did not pass")
        buggy_result = verifier.pytest(
            buggy,
            ("tests/test_repository.py::test_completing_a_task_is_idempotent_and_counts_once",),
            expected={1},
        )
        combined = buggy_result.stdout + buggy_result.stderr
        if "test_completing_a_task_is_idempotent_and_counts_once" not in combined:
            raise VerificationError("buggy scenario did not expose the controlled regression")
    return "fixed copy passed; buggy copy failed the intended idempotence regression"


def _check_security_guards(verifier: Verifier) -> str:
    forbidden_path = REPOSITORY_ROOT / f".example-forbidden-{uuid.uuid4().hex}"
    probe = textwrap.dedent(
        """
        import json
        import os
        import socket
        import subprocess
        import sys

        results = {}
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)
        except PermissionError:
            results["network"] = "blocked"
        try:
            os.getenv(sys.argv[2])
        except PermissionError:
            results["secret"] = "blocked"
        try:
            with open(sys.argv[1], "w", encoding="utf-8") as stream:
                stream.write("forbidden")
        except PermissionError:
            results["outside_write"] = "blocked"
        try:
            subprocess.run(["echo", "forbidden"], check=False)
        except PermissionError:
            results["command"] = "blocked"
        print(json.dumps(results, sort_keys=True))
        """
    )
    result = verifier.run(("-c", probe, str(forbidden_path), CANARY_NAME))
    observed = _json_stdout(result)
    expected = {
        "command": "blocked",
        "network": "blocked",
        "outside_write": "blocked",
        "secret": "blocked",
    }
    if observed != expected:
        raise VerificationError(f"security guard probe was incomplete: {observed}")
    if forbidden_path.exists():
        raise VerificationError("outside-write probe unexpectedly created a file")
    if any(verifier.canary in output for output in verifier.outputs):
        raise VerificationError("secret canary value appeared in subprocess output")
    if any(Path(command[0]).absolute() != verifier.python for command in verifier.commands):
        raise VerificationError("a non-Python command was executed")
    return "network, secret reads, outside writes, and non-Python commands blocked"


def _run_check(
    results: list[CheckResult], name: str, operation: Callable[[Verifier], str], verifier: Verifier
) -> None:
    try:
        detail = operation(verifier)
    except Exception as exc:
        results.append(CheckResult(name, False, str(exc) or exc.__class__.__name__))
    else:
        results.append(CheckResult(name, True, detail))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python containing pytest and OpenAgent (default: current interpreter)",
    )
    parser.add_argument("--json", action="store_true", dest="json_out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    python = args.python.expanduser().absolute()
    if not python.is_file():
        print(f"verification failed: Python not found: {python}", file=sys.stderr)
        return 2

    try:
        before = _snapshot_outside_example()
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 2

    results: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="openagent-example-verify-") as raw_work:
        verifier = Verifier(python, Path(raw_work))
        checks: tuple[tuple[str, Callable[[Verifier], str]], ...] = (
            ("required-files", _check_required_files),
            ("policy-and-readme", _check_policy_and_readme),
            ("expected-artifacts", _check_expected_artifacts),
            ("imports", _check_imports),
            ("pytest", _check_tests),
            ("task-board-cli", _check_task_cli),
            ("openagent-cli", _check_openagent_cli),
            ("controlled-scenarios", _check_scenarios),
            ("security-guards", _check_security_guards),
        )
        for name, operation in checks:
            _run_check(results, name, operation, verifier)

    try:
        after = _snapshot_outside_example()
        if before != after:
            changed = sorted(set(before).symmetric_difference(after))
            changed.extend(
                name for name in set(before).intersection(after) if before[name] != after[name]
            )
            detail = "outside example changed: " + ", ".join(sorted(set(changed))[:20])
            results.append(CheckResult("workspace-containment", False, detail))
        else:
            results.append(
                CheckResult(
                    "workspace-containment",
                    True,
                    f"{len(before)} outside-example files unchanged",
                )
            )
    except VerificationError as exc:
        results.append(CheckResult("workspace-containment", False, str(exc)))

    passed = all(result.passed for result in results)
    if args.json_out:
        print(
            json.dumps(
                {
                    "schema_version": "openagent.example-verifier/v1",
                    "passed": passed,
                    "network_used": False,
                    "secret_values_inherited": False,
                    "checks": [asdict(result) for result in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for result in results:
            mark = "PASS" if result.passed else "FAIL"
            print(f"[{mark}] {result.name}: {result.detail}")
        print("EXAMPLE VERIFICATION PASSED" if passed else "EXAMPLE VERIFICATION FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
