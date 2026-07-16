"""A CLI's stderr must not be able to exhaust memory (spec §13).

``ManagedProcess`` accumulated stderr into an unbounded ``list[str]``::

    self._stderr: list[str] = []
    async for raw in self._proc.stderr:
        self._stderr.append(...)

A backend that logs hundreds of megabytes to stderr (a crash loop, a progress spinner, a debug flag,
a broken dependency) is buffered in full, in RAM, for the whole run — and then joined into one string
on access, briefly doubling it. Nothing bounded it.

stdout already had a real byte cap (``run_capture``'s bounded reader). stderr did not.

The last N KB is what actually diagnoses a failure, so the buffer is a ring: it keeps the tail, counts
everything, and says plainly that it truncated.
"""

from __future__ import annotations

import sys
from pathlib import Path

from openagent.security.process import ManagedProcess, minimal_environment

#: Emit ~48 MB of stderr in big chunks, fast. If the buffer is unbounded this is all retained.
_NOISY = """
import sys
line = "E" * 1000
for _ in range(48_000):
    sys.stderr.write(line + "\\n")
sys.stderr.flush()
sys.stdout.write("done\\n")
sys.stdout.flush()
"""

#: One single enormous line with no newline at all — the pathological case for a line-based reader.
_ONE_HUGE_LINE = """
import sys
sys.stderr.write("X" * 20_000_000)
sys.stderr.flush()
sys.stdout.write("done\\n")
sys.stdout.flush()
"""


async def _drain(script: str, tmp_path: Path) -> ManagedProcess:
    path = tmp_path / "noisy.py"
    path.write_text(script)
    proc = ManagedProcess([sys.executable, str(path)], cwd=tmp_path, env=minimal_environment())
    await proc.start()
    async for _line in proc.stream_stdout():
        pass
    await proc.wait()
    return proc


async def test_massive_stderr_is_bounded(tmp_path: Path):
    proc = await _drain(_NOISY, tmp_path)
    text = proc.stderr
    # ~48 MB was produced; only the tail (plus a short notice) may be retained.
    assert len(text) < 200_000, f"stderr retained {len(text)} bytes — the buffer is not bounded"
    assert "truncated" in text.lower(), "truncation must be stated, not silent"
    # The *tail* is what diagnoses a failure, so it must be the part that survived.
    assert text.rstrip().endswith("E")


async def test_single_huge_line_is_bounded(tmp_path: Path):
    """A 20 MB line with no newline must not defeat the bound (or the reader)."""

    proc = await _drain(_ONE_HUGE_LINE, tmp_path)
    text = proc.stderr
    assert len(text) < 200_000, f"stderr retained {len(text)} bytes for a single line"
    assert "truncated" in text.lower()


async def test_small_stderr_is_kept_verbatim_and_not_marked_truncated(tmp_path: Path):
    """The common case must be unchanged: a short error is preserved exactly, with no notice."""

    script = (
        "import sys\nsys.stderr.write('boom: something broke\\n')\nsys.stdout.write('done\\n')\n"
    )
    proc = await _drain(script, tmp_path)
    assert proc.stderr.strip() == "boom: something broke"
    assert "truncated" not in proc.stderr.lower()


async def test_stderr_total_is_reported(tmp_path: Path):
    """The notice must say how much was actually produced, not just that some was dropped."""

    proc = await _drain(_NOISY, tmp_path)
    assert proc.stderr_total_bytes > 40_000_000
    assert str(proc.stderr_total_bytes) in proc.stderr
