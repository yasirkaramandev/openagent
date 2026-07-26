#!/usr/bin/env python3
"""RC soak health check (spec §25).

Records one soak observation and enforces the final-release gate. Soak requires **real elapsed
time** — a minimum of 5 (recommended 7) calendar days with no P0/P1, no data loss, no updater
failure, and no migration failure. This script never fabricates elapsed days: ``--gate`` computes
the real span between the first and the latest observation and refuses to pass early.

Usage:
    python scripts/rc_soak_check.py            # record one observation
    python scripts/rc_soak_check.py --gate     # evaluate the final-release gate
    python scripts/rc_soak_check.py --json      # machine-readable single observation
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MIN_DAYS = 5
RECOMMENDED_DAYS = 7


def _openagent_home() -> Path:
    import os

    override = os.environ.get("OPENAGENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".openagent"


def _soak_state_path() -> Path:
    return _openagent_home() / "rc_soak.json"


def _run(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{exc.__class__.__name__}: {exc}"


def _installed_commit() -> str | None:
    meta = _openagent_home() / "install.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    commit = data.get("installed_commit")
    return commit if isinstance(commit, str) else None


def observe() -> dict:
    """One health observation of the active install. Read-only; never mutates OpenAgent state."""

    version_rc, version_out = _run(["openagent", "version"])
    doctor_rc, doctor_out = _run(["openagent", "doctor", "--json"])
    doctor_payload: object = None
    try:
        doctor_payload = json.loads(doctor_out)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    doctor_exit = doctor_payload.get("exit_code") if isinstance(doctor_payload, dict) else None
    critical = []
    if version_rc != 0:
        critical.append("version command failed")
    if doctor_rc not in (0, 1):
        critical.append(f"doctor exited {doctor_rc}")
    if isinstance(doctor_exit, int) and doctor_exit not in (0, 1):
        critical.append(f"doctor reported exit_code {doctor_exit}")
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "version": version_out if version_rc == 0 else None,
        "commit": _installed_commit(),
        "doctor_exit": doctor_exit if isinstance(doctor_exit, int) else doctor_rc,
        "critical_failures": critical,
    }


def _load_state() -> dict:
    path = _soak_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("sessions", [])
            data.setdefault("critical_failures", [])
            return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"started_at": None, "candidate_commit": None, "sessions": [], "critical_failures": []}


def _save_state(state: dict) -> None:
    path = _soak_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def record() -> dict:
    state = _load_state()
    ob = observe()
    if state["started_at"] is None:
        state["started_at"] = ob["at"]
        state["candidate_commit"] = ob["commit"]
    # A candidate-commit change restarts the clock: soak measures one specific commit (spec §25).
    if ob["commit"] and state.get("candidate_commit") and ob["commit"] != state["candidate_commit"]:
        state = {
            "started_at": ob["at"],
            "candidate_commit": ob["commit"],
            "sessions": [],
            "critical_failures": [],
        }
    state["sessions"].append(ob)
    if ob["critical_failures"]:
        state["critical_failures"].extend(
            {"at": ob["at"], "failure": f} for f in ob["critical_failures"]
        )
    _save_state(state)
    return state


def _days_observed(state: dict) -> float:
    sessions = state.get("sessions") or []
    if len(sessions) < 2:
        return 0.0
    try:
        first = datetime.fromisoformat(sessions[0]["at"])
        last = datetime.fromisoformat(sessions[-1]["at"])
    except (KeyError, ValueError):
        return 0.0
    return (last - first).total_seconds() / 86400.0


def gate(state: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    days = _days_observed(state)
    if days < MIN_DAYS:
        reasons.append(
            f"only {days:.2f} calendar days observed; minimum {MIN_DAYS} "
            f"(recommended {RECOMMENDED_DAYS}) required — real time must elapse"
        )
    if state.get("critical_failures"):
        reasons.append(f"{len(state['critical_failures'])} critical failure(s) recorded")
    if not state.get("candidate_commit"):
        reasons.append("no candidate commit recorded; run the check on the installed candidate")
    return (not reasons), reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="RC soak health check")
    parser.add_argument("--gate", action="store_true", help="Evaluate the final-release gate")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    if args.gate:
        state = _load_state()
        passed, reasons = gate(state)
        payload = {
            "passed": passed,
            "days_observed": round(_days_observed(state), 2),
            "min_days": MIN_DAYS,
            "recommended_days": RECOMMENDED_DAYS,
            "sessions": len(state.get("sessions") or []),
            "critical_failures": state.get("critical_failures") or [],
            "reasons": reasons,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"soak gate: {'PASS' if passed else 'BLOCKED'}")
            print(f"  days observed: {payload['days_observed']} (min {MIN_DAYS})")
            for reason in reasons:
                print(f"  - {reason}")
        return 0 if passed else 1

    state = record()
    latest = state["sessions"][-1]
    if args.json:
        print(json.dumps(latest, indent=2))
    else:
        print(f"recorded soak observation at {latest['at']}")
        print(f"  version: {latest['version']}  commit: {(latest['commit'] or '?')[:12]}")
        print(f"  doctor_exit: {latest['doctor_exit']}  days_observed: {_days_observed(state):.2f}")
        if latest["critical_failures"]:
            print(f"  CRITICAL: {latest['critical_failures']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
