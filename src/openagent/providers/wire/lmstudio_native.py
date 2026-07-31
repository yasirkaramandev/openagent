"""LM Studio's native REST wire (spec §13.2).

LM Studio's ``/api/v0`` endpoints are OpenAI Chat-shaped with extra fields — per-request timing and
throughput stats, and the loaded model's architecture and quantization. So this is the chat wire with
a different path and the extra material read, not a second serializer: the request shape, the stream
framing, the tool-call assembly and the continuation rules are all identical, and forking them would
mean fixing every future streaming bug twice.

This is the *last* transport preference for LM Studio (spec §13.2): Responses first, because it can
hold state server-side; then OpenAI Chat; then Anthropic Messages; then this. It exists for the case
where the others are unavailable and for reading the stats nothing else exposes.
"""

from __future__ import annotations

from typing import Any

from ...core.models import Protocol
from .openai_chat import OpenAIChatWire


class LmStudioNativeWire(OpenAIChatWire):
    """The chat wire pointed at ``/api/v0``, plus LM Studio's own runtime stats."""

    protocol = Protocol.LMSTUDIO_NATIVE_CHAT
    path = "/api/v0/chat/completions"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        #: Timing/throughput for the last turn, as LM Studio reported it. Diagnostics only: nothing
        #: in the agent loop reads it, and it is not capability evidence — a fast turn proves nothing
        #: about what the model can do.
        self.last_stats: dict[str, Any] = {}
        #: Architecture/quantization of the model that served the turn, when reported. Recorded
        #: because a local model's identity is its *file*, and two "same-name" quantizations behave
        #: differently enough that a capability verified against one is not evidence about the other.
        self.last_model_info: dict[str, Any] = {}

    def _record_response_extras(self, data: dict[str, Any]) -> None:
        """Capture ``stats`` and ``model_info`` from the response the base class already parsed."""

        stats = data.get("stats")
        if isinstance(stats, dict):
            self.last_stats = dict(stats)
        info = data.get("model_info")
        if isinstance(info, dict):
            self.last_model_info = dict(info)

    @property
    def model_revision(self) -> str | None:
        """A stable identity for the *loaded* model, for capability evidence (spec §9).

        Architecture plus quantization, because that pair is what actually changes behaviour between
        two local files a user would call by the same name. ``None`` when LM Studio said nothing —
        an absent revision is honest, an invented one silently validates the wrong file.
        """

        arch = self.last_model_info.get("arch")
        quant = self.last_model_info.get("quantization")
        parts = [str(value) for value in (arch, quant) if isinstance(value, (str, int))]
        return "/".join(parts) if parts else None
