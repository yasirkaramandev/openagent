"""The §22 acceptance flow, run through real processes.

    openagent provider probe nvidia-build --model <model>
    # the process exits here
    openagent add --provider nvidia-build --model <model>

The second command must succeed **without** ``--allow-unverified-model``.

``test_probe_persistence.py`` proves the same property against two ``OpenAgentApp`` objects, which
is faster but shares an interpreter: a probe cached in a module-level dict, a class attribute, or
any other process-global would still be visible there and the test would pass. Here the probe and
the add are separate ``python -m openagent`` invocations, so *only* what reached SQLite can be read
back. Real subprocesses, a real HTTP server, a real database file — the flow a user actually runs.

The provider is a real ``nvidia-build`` connection (the mixed catalog whose gate this is) pointed at
a loopback stub, credentialed from an env var so the OS keychain is never touched.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

MODEL = "meta/llama-3.1-8b-instruct"
SENTINEL = "PROBE_OK_7F"  # providers.base._PROBE_SENTINEL — the token the text probe asks for


class _StubNIM(BaseHTTPRequestHandler):
    """Just enough of OpenAI Chat Completions to answer a real capability probe."""

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or "{}")
        if body.get("tools"):
            self._json(_tool_call_response())
        elif body.get("stream"):
            self._sse()
        else:
            self._json(_text_response(SENTINEL))

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ("streamed", None):
            delta = {"content": chunk} if chunk else {}
            payload = {
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": None if chunk else "stop"}
                ]
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _text_response(text: str) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_call_response() -> dict:
    return {
        "id": "c2",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "ping", "arguments": '{"value": 1}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


@pytest.fixture()
def stub_nim() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), _StubNIM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def cli(tmp_path: Path, stub_nim: str):
    """Run `python -m openagent ...` in a sandboxed HOME/data dir with the stub as the endpoint."""

    env = {
        **os.environ,
        "OPENAGENT_DATA_DIR": str(tmp_path / "data"),
        "OPENAGENT_CONFIG_DIR": str(tmp_path / "config"),
        # Credential by env var: no keychain write, so this test cannot touch the real OS keychain.
        "NVIDIA_TEST_KEY": "nvapi-stub-key-not-a-real-credential",
    }
    project = tmp_path / "project"
    project.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "openagent", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=project,
            timeout=120,
        )

    add = run(
        "provider",
        "add",
        "nvidia-build",
        "--type",
        "nvidia-build",
        "--base-url",
        stub_nim,
        "--key-env",
        "NVIDIA_TEST_KEY",
    )
    assert add.returncode == 0, f"provider add failed: {add.stdout}\n{add.stderr}"
    return run


def test_probe_then_add_in_a_separate_process_needs_no_override(cli) -> None:
    """The literal §22 acceptance criterion."""

    probe = cli("provider", "probe", "nvidia-build", "--model", MODEL, "--json")
    assert probe.returncode == 0, f"probe failed: {probe.stdout}\n{probe.stderr}"
    verdict = json.loads(probe.stdout)
    assert verdict["category"] == "verified", verdict
    assert verdict["tool_calling"] is True, verdict

    # A brand-new process. Nothing from the probe survives except what is on disk.
    add = cli(
        "add",
        "--name",
        "probe-gated-agent",
        "--provider",
        "nvidia-build",
        "--model",
        MODEL,
        "--title",
        "Probe gated",
    )

    assert add.returncode == 0, (
        "a model verified in a previous process still demanded --allow-unverified-model:\n"
        f"{add.stdout}\n{add.stderr}"
    )


def test_add_without_a_prior_probe_is_still_refused(cli) -> None:
    """The positive control: the gate must still bite for a model nobody probed.

    Without this, a `cached_probe` that wrongly returned "verified" for everything would sail
    through the test above and look like a pass.
    """

    add = cli(
        "add",
        "--name",
        "ungated-agent",
        "--provider",
        "nvidia-build",
        "--model",
        "nvidia/never-probed-model",
        "--title",
        "Ungated",
    )

    assert add.returncode != 0
    assert "has not been validated" in (add.stdout + add.stderr)
