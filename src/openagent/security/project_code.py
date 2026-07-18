"""Whether the project's own code may execute, and under what supervision (spec §3).

``run_tests`` is not a file operation with a command-shaped name — it is a request to **execute the
project's code**. pytest imports every test module and every ``conftest.py`` it collects; ``npm
test`` runs whatever ``package.json`` says; ``cargo test`` compiles and runs build scripts. An agent
that can write a file into the workspace therefore controls what that command does.

That makes the executable name almost irrelevant to the decision. Screening argv for ``pytest``
tells you the *shape* of the invocation, not whose code runs. The question that actually matters is:

    if this executes arbitrary code, what contains it, and did a human agree?

So the decision is centralised here and takes the whole situation into account — the permission
profile, the execution backend, and whether an approval can be obtained at all — rather than being
inferred at the call site from an allowlist. Structured-argv validation (``evaluate_test_argv``)
still runs; it is a *shape* check layered under this one, not a substitute for it.

The contract:

===========================================  ==========================================
profile + backend                            outcome
===========================================  ==========================================
``read-only`` (no command authority)         ``DENY``
any profile + ``container-sandbox``          ``ALLOW_SANDBOXED`` — contained, runs unattended
``safe-edit`` + ``host-restricted``          ``REQUIRE_APPROVAL`` — a human must say yes
``development`` / ``full-access`` + host     ``ALLOW_SANDBOXED``\\* — the profile accepts host risk
===========================================  ==========================================

\\* Not literally sandboxed: for these profiles the *user* has already accepted that project code
runs on the host unattended, which is the documented meaning of choosing them. The enum member says
"no further approval needed"; :attr:`ProjectCodeExecutionPolicy.contained` says whether anything is
actually containing it, and callers that surface risk to the user read that instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.permissions import PermissionProfile

#: Backend names, duplicated as plain strings to keep this module free of import cycles
#: (``execution_backend`` imports the filesystem/process layers, which import permissions).
HOST_RESTRICTED = "host-restricted"
CONTAINER_SANDBOX = "container-sandbox"


class ProjectCodeExecutionDecision(str, Enum):
    #: Run it: either a real sandbox contains it, or the profile explicitly accepts host risk.
    ALLOW_SANDBOXED = "allow_sandboxed"
    #: A human must approve this specific execution before any subprocess starts.
    REQUIRE_APPROVAL = "require_approval"
    #: Not permitted for this profile at all.
    DENY = "deny"


@dataclass(frozen=True)
class ProjectCodeExecutionPolicy:
    decision: ProjectCodeExecutionDecision
    reason: str
    #: The backend that would run it, for the audit record and the approval prompt.
    backend: str
    #: True only when an actual sandbox confines the execution. ``development`` on the host runs
    #: unattended but is **not** contained — the approval prompt and docs must not imply otherwise.
    contained: bool = False

    @property
    def needs_approval(self) -> bool:
        return self.decision is ProjectCodeExecutionDecision.REQUIRE_APPROVAL

    @property
    def denied(self) -> bool:
        return self.decision is ProjectCodeExecutionDecision.DENY


def decide_project_code_execution(
    *,
    profile: PermissionProfile,
    backend: str | None,
) -> ProjectCodeExecutionPolicy:
    """Decide how ``run_tests``-style project code execution must be supervised.

    ``backend`` is the execution backend's name; ``None`` means the caller has no backend configured
    and will fall back to host execution, so it is treated as ``host-restricted`` rather than as an
    absence of risk.
    """

    backend_name = backend or HOST_RESTRICTED

    if not profile.can_run_commands:
        return ProjectCodeExecutionPolicy(
            ProjectCodeExecutionDecision.DENY,
            f"the {profile.name!r} profile does not execute project code",
            backend_name,
        )

    if backend_name == CONTAINER_SANDBOX:
        return ProjectCodeExecutionPolicy(
            ProjectCodeExecutionDecision.ALLOW_SANDBOXED,
            "project code runs inside the container sandbox",
            backend_name,
            contained=True,
        )

    if backend_name != HOST_RESTRICTED:
        # An unknown backend is not a licence to guess. Fail closed.
        return ProjectCodeExecutionPolicy(
            ProjectCodeExecutionDecision.REQUIRE_APPROVAL,
            f"unrecognised execution backend {backend_name!r}; approval required",
            backend_name,
        )

    if profile.auto_run_project_code_on_host:
        return ProjectCodeExecutionPolicy(
            ProjectCodeExecutionDecision.ALLOW_SANDBOXED,
            f"the {profile.name!r} profile accepts running project code directly on the host",
            backend_name,
            contained=False,
        )

    return ProjectCodeExecutionPolicy(
        ProjectCodeExecutionDecision.REQUIRE_APPROVAL,
        (
            "running the project's tests executes code from the workspace on this host, and the "
            "agent can write that code. host-restricted is a policy boundary, not a sandbox"
        ),
        backend_name,
    )


def approval_detail(command: str, policy: ProjectCodeExecutionPolicy, workspace: str) -> str:
    """The text a human is shown before host project code runs. Blunt on purpose."""

    return (
        f"{command}\n"
        f"backend: {policy.backend} (no kernel-level isolation)\n"
        f"workspace: {workspace}\n"
        "risk: this executes code from the workspace, including any test or conftest file the "
        "agent just wrote. It runs with your user account's privileges."
    )
