"""Project identity (spec §3).

OpenAgent keeps **one SQLite database per user** (in the platform data dir) but writes **run
artifacts next to each project** (``<project>/.openagent/runs/<run-id>/``). Nothing tied the two
together, which made several behaviours wrong once a second project existed:

* ``recover_orphans()`` walked every active run in the global DB, so opening OpenAgent in project B
  marked project A's genuinely-running run as orphaned;
* ``output()``/``projection()`` resolved artifacts from the *current* ``Paths`` — i.e. the directory
  you happened to be in — so asking project B for project A's run looked in the wrong place;
* run lists mixed every project together with no way to tell them apart.

A run therefore records where it came from: a stable ``project_id``, the canonical ``project_root``,
the ``project_state_dir`` and the concrete ``artifact_dir``. The id is derived from the *resolved*
path so that symlinked or differently-spelled routes to the same directory agree, and it is stored so
that later moving the project does not silently reassign old runs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def canonical_root(project_root: Path) -> Path:
    """The canonical form of a project root.

    Resolved so that ``/tmp/x`` and ``/private/tmp/x`` (macOS), a symlinked checkout, or a relative
    path all produce the same identity. Falls back to an absolute path when the directory cannot be
    resolved (it may not exist yet).
    """

    try:
        return project_root.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - unresolvable path
        return project_root.absolute()


def project_id_for(project_root: Path) -> str:
    """A stable, filesystem-safe id for a project directory.

    A hash of the canonical path: short, stable across runs, and free of separators/case issues that
    would make the raw path awkward as a key. It identifies *a directory*, not its contents.
    """

    digest = hashlib.sha256(str(canonical_root(project_root)).encode("utf-8")).hexdigest()
    return f"proj_{digest[:16]}"
