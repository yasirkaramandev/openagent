"""Cross-platform, provenance-friendly coding CLI executable discovery.

``shutil.which`` returns only the first PATH match and inherits whatever truncated PATH the process
was launched with. OpenAgent needs the complete candidate set so it can identify the binary that a
run will actually execute, report shadowed copies, reject desktop applications, and avoid updating a
different installation from the active one.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ...security.process import minimal_environment, run_capture

MAX_PROBE_OUTPUT_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 10


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], int, int], CommandResult]


def run_bounded(
    argv: Sequence[str],
    timeout: int = PROBE_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_PROBE_OUTPUT_BYTES,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> CommandResult:
    """Run structured argv with a minimal environment, tree timeout, and a real output bound."""

    completed = run_capture(
        list(argv),
        cwd=cwd or Path.cwd(),
        env=dict(env) if env is not None else minimal_environment(),
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


class ExecutableCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    resolved_path: str | None = None
    origin: str
    valid: bool = False
    active: bool = False
    version: str | None = None
    detail: str = ""


class CliLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli_type: str
    active_executable: str | None = None
    resolved_executable: str | None = None
    shadowed_executables: list[str] = Field(default_factory=list)
    candidates: list[ExecutableCandidate] = Field(default_factory=list)
    path_conflict: bool = False
    desktop_conflict: bool = False


_CLI_NAMES: dict[str, tuple[str, ...]] = {
    "codex": ("codex",),
    "claude": ("claude",),
    "antigravity": ("agy", "antigravity"),
}


def _windows(platform: str) -> bool:
    return platform.startswith("win")


def _name_variants(name: str, platform: str, env: Mapping[str, str]) -> list[str]:
    if not _windows(platform):
        return [name]
    suffixes = [part.lower() for part in (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";")]
    if Path(name).suffix:
        return [name]
    return [name, *[f"{name}{suffix}" for suffix in suffixes]]


def _path_candidates(
    names: Sequence[str], env: Mapping[str, str], platform: str
) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    separator = ";" if _windows(platform) else os.pathsep
    for raw_dir in (env.get("PATH") or "").split(separator):
        if not raw_dir:
            continue
        directory = Path(raw_dir).expanduser()
        for name in names:
            for variant in _name_variants(name, platform, env):
                candidate = directory / variant
                if candidate.exists() or candidate.is_symlink():
                    found.append((candidate, "path"))
    return found


def _npm_candidates(
    names: Sequence[str], env: Mapping[str, str], platform: str, runner: CommandRunner
) -> list[tuple[Path, str]]:
    prefixes: list[Path] = []
    try:
        result = runner(["npm", "prefix", "-g"], 5, 64 * 1024)
    except Exception:
        result = CommandResult(returncode=1)
    if result.returncode == 0 and result.stdout.strip():
        prefixes.append(Path(result.stdout.strip().splitlines()[-1]))
    if _windows(platform) and env.get("APPDATA"):
        prefixes.append(Path(env["APPDATA"]) / "npm")
    candidates: list[tuple[Path, str]] = []
    for prefix in prefixes:
        bins = [prefix] if _windows(platform) else [prefix / "bin"]
        for directory in bins:
            for name in names:
                for variant in _name_variants(name, platform, env):
                    candidates.append((directory / variant, "npm"))
    return candidates


def candidate_paths(
    cli_type: str,
    *,
    explicit_path: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
    runner: CommandRunner = run_bounded,
) -> list[tuple[Path, str]]:
    """Return all documented/likely candidate paths in deterministic priority order."""

    env = dict(os.environ if env is None else env)
    home = Path.home() if home is None else home
    platform = sys.platform if platform is None else platform
    names = _CLI_NAMES.get(cli_type, (cli_type,))
    candidates: list[tuple[Path, str]] = []
    if explicit_path:
        candidates.append((Path(explicit_path).expanduser(), "explicit"))
    candidates.extend(_path_candidates(names, env, platform))

    if _windows(platform):
        user_profile = Path(env.get("USERPROFILE") or home)
        local_app_data = Path(env.get("LOCALAPPDATA") or user_profile / "AppData" / "Local")
        if cli_type == "claude":
            candidates.extend(
                [
                    (user_profile / ".local" / "bin" / "claude.exe", "native"),
                    (user_profile / ".claude" / "local" / "claude.exe", "legacy-local"),
                ]
            )
        elif cli_type == "codex":
            candidates.append((user_profile / ".local" / "bin" / "codex.exe", "native"))
        elif cli_type == "antigravity":
            candidates.append((local_app_data / "agy" / "bin" / "agy.exe", "native"))
        winget_links = local_app_data / "Microsoft" / "WinGet" / "Links"
        for name in names:
            candidates.append((winget_links / f"{name}.exe", "winget"))
    else:
        official_name = "agy" if cli_type == "antigravity" else names[0]
        candidates.append((home / ".local" / "bin" / official_name, "native"))
        if cli_type == "claude":
            candidates.extend(
                [
                    (home / ".claude" / "local" / "claude", "legacy-local"),
                    (Path("/opt/homebrew/bin/claude"), "homebrew"),
                    (Path("/usr/local/bin/claude"), "homebrew"),
                ]
            )
        elif cli_type == "codex":
            candidates.extend(
                [
                    (Path("/opt/homebrew/bin/codex"), "homebrew"),
                    (Path("/usr/local/bin/codex"), "homebrew"),
                ]
            )
        elif cli_type == "antigravity":
            candidates.extend(
                [
                    (Path("/opt/homebrew/bin/agy"), "homebrew"),
                    (Path("/usr/local/bin/agy"), "homebrew"),
                ]
            )
    candidates.extend(_npm_candidates(names, env, platform, runner))
    return candidates


def _inspect_path(path: Path, platform: str) -> tuple[bool, Path | None, str]:
    try:
        info = path.lstat()
    except OSError as exc:
        return False, None, str(exc)
    if stat.S_ISDIR(info.st_mode):
        return False, None, "candidate is a directory/application bundle"
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        return False, None, "candidate is not a regular executable or symlink"
    try:
        resolved = path.resolve(strict=True)
        target_info = resolved.stat()
    except (OSError, RuntimeError) as exc:
        return False, None, f"unsafe or broken symlink: {exc}"
    if not stat.S_ISREG(target_info.st_mode):
        return False, None, "resolved target is not a regular file"
    if not _windows(platform) and not os.access(resolved, os.X_OK):
        return False, resolved, "resolved target is not executable"
    return True, resolved, ""


def _first_line(value: str) -> str | None:
    for line in value.splitlines():
        line = line.strip()
        if line:
            return line[:500]
    return None


def _validate_cli(cli_type: str, path: Path, runner: CommandRunner) -> tuple[bool, str | None, str]:
    try:
        version_result = runner([str(path), "--version"], PROBE_TIMEOUT_SECONDS, 64 * 1024)
    except Exception as exc:
        return False, None, f"--version failed: {exc}"
    version = _first_line(version_result.stdout or version_result.stderr)
    if version_result.returncode != 0 or not version:
        return False, version, f"--version exited {version_result.returncode}"
    if cli_type == "claude":
        try:
            help_result = runner(
                [str(path), "--help"], PROBE_TIMEOUT_SECONDS, MAX_PROBE_OUTPUT_BYTES
            )
        except Exception as exc:
            return False, version, f"Claude Code help validation failed: {exc}"
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        required = ("--output-format", "--model", "--permission-mode")
        if help_result.returncode != 0 or any(flag not in help_text for flag in required):
            return False, version, "executable is not the Claude Code CLI"
    return True, version, ""


def locate_candidates(
    cli_type: str,
    *,
    explicit_path: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
    runner: CommandRunner = run_bounded,
) -> CliLocation:
    """Locate, validate, realpath-dedupe, and classify active/shadowed executables."""

    env = dict(os.environ if env is None else env)
    platform = sys.platform if platform is None else platform
    inspected: list[ExecutableCandidate] = []
    seen_paths: set[str] = set()
    seen_realpaths: set[str] = set()
    active: ExecutableCandidate | None = None
    desktop_conflict = False
    for path, origin in candidate_paths(
        cli_type,
        explicit_path=explicit_path,
        env=env,
        home=home,
        platform=platform,
        runner=runner,
    ):
        normalized = os.path.normcase(str(path.expanduser()))
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        exists = path.exists() or path.is_symlink()
        if not exists:
            continue
        valid, resolved, detail = _inspect_path(path, platform)
        version: str | None = None
        if valid and resolved is not None:
            real_key = os.path.normcase(str(resolved))
            if real_key in seen_realpaths:
                continue
            seen_realpaths.add(real_key)
            valid, version, detail = _validate_cli(cli_type, path, runner)
        if (
            cli_type == "claude"
            and re.search(r"Claude\.exe$", path.name, re.IGNORECASE)
            and not valid
        ):
            desktop_conflict = True
        candidate = ExecutableCandidate(
            path=str(path),
            resolved_path=str(resolved) if resolved is not None else None,
            origin=origin,
            valid=valid,
            version=version,
            detail=detail,
        )
        if valid and active is None and (origin == "explicit" or origin == "path"):
            candidate.active = True
            active = candidate
        inspected.append(candidate)

    if active is None:
        active = next((candidate for candidate in inspected if candidate.valid), None)
        if active is not None:
            active.active = True
    shadowed = [
        candidate.path
        for candidate in inspected
        if candidate.valid and active is not None and candidate.path != active.path
    ]
    return CliLocation(
        cli_type=cli_type,
        active_executable=active.path if active else None,
        resolved_executable=active.resolved_path if active else None,
        shadowed_executables=shadowed,
        candidates=inspected,
        path_conflict=bool(shadowed),
        desktop_conflict=desktop_conflict,
    )
