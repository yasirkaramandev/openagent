"""Explicit per-executable argv validators for the auto-allow tier (spec §4).

A broad "known executable" allowlist is not a boundary, because the executable name does not
determine what the executable *does*. Every one of these ran unattended before v0.1.4::

    env python -c "..."            # env is allowlisted; python is what actually runs
    find . -exec python -c "..." + # find is allowlisted; -exec runs anything
    find . -delete                 # a "search" tool that deletes
    xargs python                   # builds a command line and runs it
    git branch -D release          # "branch" is read-only until you pass -D
    sort --output=../../outside f  # the clamp only looked at tokens not starting with "-"

The pattern is the same each time: the *name* was screened, the *arguments* were not. So this module
inverts the model. An executable is auto-allowed only if it has a validator here, and a validator
must name every option it accepts. The parser is shared, so the rules that matter hold everywhere:

* unknown option → not allowed unattended (**fail closed**, never "probably fine");
* options are classified as flag or value-taking, so a value is never mistaken for a new option;
* ``--opt=value``, ``--opt value``, ``-oVALUE`` and ``-o VALUE`` are all parsed to the same value,
  because the workspace clamp has to see the path in every one of those spellings;
* every path — positional or hidden inside an option value — is clamped to the workspace;
* options that mutate state or spawn another program are named and refused with a reason.

Anything not covered here falls through to approval. That is the intended outcome, not a gap: the
read-only work an agent actually needs is served by dedicated tools (``read_file``, ``search_text``,
``git_status``, ``git_diff``) that take structured arguments and cannot be talked into running a
subprocess.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: Short options whose value may be attached (``-oFILE``). Kept explicit per executable.
_LOOKS_LIKE_PATH = re.compile(r"[\\/]|^~")
#: The ``head -20`` / ``tail -5`` shorthand.
_NUMERIC_SHORTHAND = re.compile(r"-\d+")


@dataclass(frozen=True)
class ExecutableSpec:
    """What one executable may be handed when it runs unattended."""

    #: Options taking no value (``-l``, ``--long``).
    flags: frozenset[str] = frozenset()
    #: Options consuming exactly one value.
    value_options: frozenset[str] = frozenset()
    #: Options refused outright, mapped to why. Checked before ``flags``/``value_options``.
    forbidden: Mapping[str, str] = field(default_factory=dict)
    #: Subcommands that may run unattended. ``None`` when the executable has no subcommand concept.
    subcommands: frozenset[str] | None = None
    #: Whether positional arguments are accepted at all (they are always workspace-clamped).
    allow_positional: bool = True
    #: Options whose value is a path even when it does not look like one.
    path_values: frozenset[str] = frozenset()
    #: Accept the historical ``-NUM`` line-count shorthand (``head -20``, ``tail -5``). It carries no
    #: path and names no program, so it is safe — but it must be opted into, not inferred.
    numeric_shorthand: bool = False


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    reason: str = ""


def _is_long(token: str) -> bool:
    return token.startswith("--") and len(token) > 2


def _is_short(token: str) -> bool:
    return token.startswith("-") and not token.startswith("--") and len(token) > 1


def option_value_candidates(token: str) -> list[str]:
    """Path-looking values hidden inside a single option token.

    ``--output=../../x`` and ``-o../../x`` both smuggle a path past any check that skips tokens
    beginning with ``-``. This is deliberately independent of the validator registry so the generic
    workspace clamp can use it too — the bug it fixes is not specific to allowlisted executables.
    """

    if not token.startswith("-") or token == "-" or token == "--":
        return []
    if "=" in token:
        _, _, value = token.partition("=")
        return [value] if value else []
    if _is_short(token) and len(token) > 2:
        remainder = token[2:]
        # Only treat an attached remainder as a path when it looks like one; ``-la`` is two flags.
        if _LOOKS_LIKE_PATH.search(remainder):
            return [remainder]
    return []


def validate_argv(
    spec: ExecutableSpec,
    argv: tuple[str, ...],
    *,
    clamp: bool,
    workspace_root: Path | None,
    escapes: Callable[[str, Path], bool],
) -> ValidationOutcome:
    """Walk ``argv`` against ``spec``. ``escapes`` is the workspace-escape predicate.

    Passing the predicate in keeps this module free of a circular import back into the policy while
    still enforcing the clamp on every path the parser identifies.
    """

    rest = list(argv[1:])
    if spec.subcommands is not None:
        subcommand = next((token for token in rest if not token.startswith("-")), None)
        if subcommand is None:
            return ValidationOutcome(False, "a subcommand is required")
        if subcommand not in spec.subcommands:
            return ValidationOutcome(
                False, f"{subcommand!r} is not a read-only subcommand for this executable"
            )

    def _check_path(value: str) -> ValidationOutcome | None:
        if not clamp or workspace_root is None or not value:
            return None
        if escapes(value, workspace_root):
            return ValidationOutcome(False, f"{value!r} resolves outside the workspace")
        return None

    index = 0
    positional_only = False
    while index < len(rest):
        token = rest[index]
        index += 1

        if token == "--":
            positional_only = True
            continue

        if not positional_only and (_is_long(token) or _is_short(token)):
            name, has_eq, inline_value = token.partition("=")

            # Exact matches win over any cluster reading. ``find`` spells its primaries with a
            # single dash (``-name``, ``-maxdepth``), so treating every single-dash token as a
            # bundle of one-letter flags would reject ordinary usage — and, worse, would tempt
            # someone to loosen the unknown-option rule to compensate.
            if name in spec.forbidden:
                return ValidationOutcome(False, spec.forbidden[name])

            if name in spec.value_options:
                if has_eq:
                    value = inline_value
                else:
                    if index >= len(rest):
                        return ValidationOutcome(False, f"{name} requires a value")
                    value = rest[index]
                    index += 1
                if name in spec.path_values or _LOOKS_LIKE_PATH.search(value):
                    failure = _check_path(value)
                    if failure is not None:
                        return failure
                continue

            if name in spec.flags:
                if has_eq:
                    return ValidationOutcome(False, f"{name} does not take a value")
                continue

            if spec.numeric_shorthand and _NUMERIC_SHORTHAND.fullmatch(token):
                continue

            if not _is_long(token) and not has_eq and len(token) > 2:
                # A short cluster (``-la``) or an attached value (``-oFILE``). Try the attached-value
                # reading first: if the leading option consumes a value, the rest of the token is it.
                leading = token[:2]
                if leading in spec.forbidden:
                    return ValidationOutcome(False, spec.forbidden[leading])
                if leading in spec.value_options:
                    failure = _check_path(token[2:])
                    if failure is not None:
                        return failure
                    continue
                # Otherwise every character must be a known standalone flag.
                for char in token[1:]:
                    flag = f"-{char}"
                    if flag in spec.forbidden:
                        return ValidationOutcome(False, spec.forbidden[flag])
                    if flag not in spec.flags:
                        return ValidationOutcome(False, f"{flag} is not an allowed option")
                continue

            return ValidationOutcome(False, f"{name} is not an allowed option")

        # A positional argument.
        if not spec.allow_positional:
            return ValidationOutcome(False, "this executable takes no positional arguments here")
        failure = _check_path(token)
        if failure is not None:
            return failure

    return ValidationOutcome(True)


# --------------------------------------------------------------------------- the registry

#: ``find`` primaries that run another program or mutate the filesystem. ``find`` is a search tool
#: only until one of these appears; ``-delete`` and ``-exec`` are why it cannot be name-allowlisted.
_FIND_FORBIDDEN = {
    "-exec": "find -exec runs another program",
    "-execdir": "find -execdir runs another program",
    "-ok": "find -ok runs another program",
    "-okdir": "find -okdir runs another program",
    "-delete": "find -delete removes files",
    "-fprint": "find -fprint writes to a file",
    "-fprint0": "find -fprint0 writes to a file",
    "-fprintf": "find -fprintf writes to a file",
    "-fls": "find -fls writes to a file",
}

#: ``git`` subcommands that mutate the repository, the index, or global configuration. Read-only
#: inspection belongs to the dedicated ``git_status`` / ``git_diff`` tools.
_GIT_FORBIDDEN_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "fetch",
        "filter-branch",
        "gc",
        "init",
        "merge",
        "mv",
        "prune",
        "pull",
        "push",
        "rebase",
        "reflog",
        "remote",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "submodule",
        "switch",
        "tag",
        "update-index",
        "update-ref",
        "worktree",
    }
)

_COMMON_OUTPUT_FORBIDDEN = {
    "--output": "writing output to a file is not read-only inspection",
    "-o": "writing output to a file is not read-only inspection",
}

VALIDATORS: dict[str, ExecutableSpec] = {
    "pwd": ExecutableSpec(allow_positional=False),
    "date": ExecutableSpec(flags=frozenset({"-u", "--utc"}), allow_positional=False),
    "echo": ExecutableSpec(flags=frozenset({"-n", "-e", "-E"})),
    "true": ExecutableSpec(),
    "false": ExecutableSpec(),
    "ls": ExecutableSpec(
        flags=frozenset(
            {
                "-l",
                "-a",
                "-A",
                "-h",
                "-t",
                "-r",
                "-S",
                "-R",
                "-1",
                "-d",
                "-F",
                "-i",
                "-n",
                "--all",
                "--almost-all",
                "--human-readable",
                "--reverse",
                "--recursive",
                "--size",
                "--long",
                "--classify",
                "--directory",
                "--inode",
            }
        ),
        value_options=frozenset({"--sort", "--time", "--color", "--format"}),
    ),
    "cat": ExecutableSpec(flags=frozenset({"-n", "-b", "-s", "-A", "-E", "-T", "--number"})),
    "head": ExecutableSpec(
        flags=frozenset({"-q", "-v"}),
        value_options=frozenset({"-n", "-c", "--lines", "--bytes"}),
        numeric_shorthand=True,
    ),
    "tail": ExecutableSpec(
        flags=frozenset({"-q", "-v"}),
        value_options=frozenset({"-n", "-c", "--lines", "--bytes"}),
        numeric_shorthand=True,
    ),
    "wc": ExecutableSpec(flags=frozenset({"-l", "-w", "-c", "-m", "-L"})),
    "uniq": ExecutableSpec(flags=frozenset({"-c", "-d", "-u", "-i"})),
    "cut": ExecutableSpec(
        flags=frozenset({"-s"}),
        value_options=frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields"}),
    ),
    # ``sort`` is read-only *unless* you give it -o/--output, which writes anywhere it is pointed.
    "sort": ExecutableSpec(
        flags=frozenset({"-n", "-r", "-u", "-f", "-b", "-h", "-V", "--numeric-sort", "--reverse"}),
        value_options=frozenset({"-k", "-t", "--key", "--field-separator"}),
        forbidden=_COMMON_OUTPUT_FORBIDDEN,
    ),
    "grep": ExecutableSpec(
        flags=frozenset(
            {
                "-i",
                "-v",
                "-n",
                "-r",
                "-R",
                "-l",
                "-L",
                "-c",
                "-w",
                "-x",
                "-E",
                "-F",
                "-o",
                "-q",
                "-s",
                "-a",
                "-H",
                "-h",
                "-I",
                "--ignore-case",
                "--invert-match",
                "--line-number",
                "--recursive",
                "--files-with-matches",
                "--count",
                "--word-regexp",
                "--extended-regexp",
                "--fixed-strings",
                "--only-matching",
                "--color",
                "--no-messages",
            }
        ),
        value_options=frozenset(
            {"-e", "-m", "-A", "-B", "-C", "--include", "--exclude", "--exclude-dir", "--regexp"}
        ),
        forbidden={
            "-f": "grep -f reads the pattern list from a file",
            "--file": "grep --file reads the pattern list from a file",
            "-d": "grep -d controls directory recursion behaviour",
        },
    ),
    "rg": ExecutableSpec(
        flags=frozenset(
            {
                "-i",
                "-v",
                "-n",
                "-l",
                "-c",
                "-w",
                "-x",
                "-F",
                "-S",
                "-s",
                "-u",
                "-U",
                "-N",
                "--ignore-case",
                "--invert-match",
                "--line-number",
                "--files-with-matches",
                "--count",
                "--word-regexp",
                "--fixed-strings",
                "--smart-case",
                "--hidden",
                "--no-heading",
                "--json",
                "--files",
            }
        ),
        value_options=frozenset(
            {
                "-e",
                "-g",
                "-t",
                "-m",
                "-A",
                "-B",
                "-C",
                "--regexp",
                "--glob",
                "--type",
                "--max-count",
            }
        ),
        forbidden={
            "--pre": "rg --pre runs another program on every file",
            "--hostname-bin": "rg --hostname-bin runs another program",
            "-f": "rg -f reads the pattern list from a file",
            "--file": "rg --file reads the pattern list from a file",
        },
    ),
    "find": ExecutableSpec(
        flags=frozenset(
            {
                "-print",
                "-print0",
                "-empty",
                "-follow",
                "-depth",
                "-L",
                "-H",
                "-P",
                "-nowarn",
                "-xdev",
                "-mount",
                "-readable",
                "-writable",
                "-executable",
                # Boolean operators between primaries.
                "-a",
                "-and",
                "-o",
                "-or",
                "-not",
            }
        ),
        value_options=frozenset(
            {
                "-name",
                "-iname",
                "-path",
                "-ipath",
                "-regex",
                "-iregex",
                "-maxdepth",
                "-mindepth",
                "-size",
                "-mtime",
                "-mmin",
                "-newer",
                "-user",
                "-group",
                "-perm",
                "-wholename",
                "-type",
                "-lname",
                "-ilname",
                "-anewer",
                "-cmin",
                "-ctime",
                "-inum",
                "-links",
                "-samefile",
                "-uid",
                "-gid",
            }
        ),
        forbidden=_FIND_FORBIDDEN,
        path_values=frozenset({"-path", "-ipath", "-wholename", "-newer", "-samefile", "-anewer"}),
    ),
    "stat": ExecutableSpec(
        flags=frozenset({"-L", "-t"}), value_options=frozenset({"-f", "-c", "--format"})
    ),
    "file": ExecutableSpec(flags=frozenset({"-b", "-i", "-L", "--brief", "--mime"})),
    "which": ExecutableSpec(flags=frozenset({"-a"})),
    "basename": ExecutableSpec(flags=frozenset({"-a", "-z"}), value_options=frozenset({"-s"})),
    "dirname": ExecutableSpec(flags=frozenset({"-z"})),
    "realpath": ExecutableSpec(flags=frozenset({"-e", "-m", "-s", "-q", "--relative-to"})),
    "readlink": ExecutableSpec(flags=frozenset({"-f", "-e", "-m", "-n", "-q"})),
    "tree": ExecutableSpec(
        flags=frozenset({"-a", "-d", "-f", "-i", "-L", "-r", "--dirsfirst"}),
        value_options=frozenset({"-L", "-P", "-I"}),
        forbidden={"-o": "tree -o writes output to a file"},
    ),
    "diff": ExecutableSpec(
        flags=frozenset({"-u", "-r", "-q", "-w", "-b", "-B", "-i", "-N", "--unified", "--brief"}),
        value_options=frozenset({"-U", "--unified"}),
        forbidden={
            "--to-file": "diff --to-file writes outside the compared pair",
            "--from-file": "diff --from-file reads an arbitrary file",
        },
    ),
    "cmp": ExecutableSpec(flags=frozenset({"-s", "-l", "--silent"})),
    "git": ExecutableSpec(
        # Only genuinely read-only porcelain/plumbing. Note the deliberate absence of ``branch`` and
        # ``tag``: both mutate as soon as -d/-D/-m appears, and listing is what git_status is for.
        subcommands=frozenset(
            {
                "status",
                "diff",
                "log",
                "show",
                "rev-parse",
                "ls-files",
                "describe",
                "blame",
                "shortlog",
                "diff-tree",
                "cat-file",
                "ls-tree",
            }
        ),
        flags=frozenset(
            {
                "--short",
                "--branch",
                "--porcelain",
                "--stat",
                "--name-only",
                "--name-status",
                "--oneline",
                "--graph",
                "--decorate",
                "--cached",
                "--staged",
                "--numstat",
                "--no-color",
                "--no-pager",
                "--no-ext-diff",
                "--no-textconv",
                "--all",
                "--abbrev-ref",
                "--show-toplevel",
                "--is-inside-work-tree",
                "--git-dir",
                "-s",
                "-b",
                "-p",
                "-1",
                "--quiet",
                "--others",
                "--exclude-standard",
                "--long",
            }
        ),
        value_options=frozenset(
            {
                "-n",
                "--max-count",
                "--since",
                "--until",
                "--author",
                "--grep",
                "--pretty",
                "--format",
                "-U",
                "--unified",
            }
        ),
        forbidden={
            "-c": "git -c injects configuration, including pagers and hooks that execute code",
            "-C": "git -C moves the working directory outside the screened workspace",
            "--exec-path": "git --exec-path redirects git to another set of executables",
            "--output": "git --output writes to a file",
            "--upload-pack": "git --upload-pack runs another program",
            "--receive-pack": "git --receive-pack runs another program",
            "--ext-diff": "git --ext-diff runs an external diff program",
        },
    ),
}

#: Executables that must never be auto-allowed because they exist to run *other* programs, or
#: because their options write outside the workspace. Named so the refusal can explain itself.
WRAPPER_EXECUTABLES: dict[str, str] = {
    "env": "env runs whatever program is named after it, so allowlisting env allowlists everything",
    "xargs": "xargs builds a command line from its input and runs it",
    "nice": "nice runs another program",
    "nohup": "nohup runs another program",
    "time": "time runs another program",
    "timeout": "timeout runs another program",
    "stdbuf": "stdbuf runs another program",
    "setsid": "setsid runs another program",
    "watch": "watch repeatedly runs another program",
    "parallel": "parallel runs another program",
    "sudo": "sudo runs another program with elevated privileges",
    "doas": "doas runs another program with elevated privileges",
    "tee": "tee writes its input to any file it is given",
    "sed": "sed can write files in place and execute commands via the e flag",
    "awk": "awk is a full programming language with system() access",
    "gawk": "gawk is a full programming language with system() access",
    "mawk": "mawk is a full programming language with system() access",
    "start": "start launches another program",
    "cmd": "cmd is a shell interpreter",
    "where": "where is resolved by the Windows shell",
}


def get_validator(executable: str) -> ExecutableSpec | None:
    return VALIDATORS.get(executable)
