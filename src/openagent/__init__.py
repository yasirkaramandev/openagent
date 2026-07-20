"""OpenAgent — local-first control plane for AI APIs, coding CLIs, and autonomous agents."""

#: The single source of truth for the project version (spec item 8). ``pyproject.toml`` declares the
#: version ``dynamic`` and hatchling reads it from this line, so the built wheel's metadata and this
#: constant are always the same string. ``openagent version`` prints this value directly.
__version__ = "0.1.6rc1"
