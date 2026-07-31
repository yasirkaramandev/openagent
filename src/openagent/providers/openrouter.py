"""OpenRouter routing policy (spec §14.3).

OpenRouter is the one v0.2 provider where "which model" does not determine "which machine". The same
model id can be served by several upstreams with different prices, context limits, quantizations and
data-retention terms, and the choice is made by a routing policy sent with the request.

Two decisions here, and both are about where the policy *lives*.

**Not in the credential.** A credential is a secret; a routing policy is configuration the user should
be able to read, diff and change without touching a key. Embedding one in the other means rotating a
key rewrites routing, and reading routing requires reading a secret.

**Not chosen automatically.** There is no automatic model or provider selection here (spec §14.3): a
heuristic that silently picks the cheapest upstream also silently picks its data-retention terms and
its quantization. The policy says exactly what the user asked for, and nothing infers the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Values OpenRouter documents for ``provider.sort``.
SORT_VALUES = ("price", "throughput", "latency")
#: Values OpenRouter documents for ``provider.data_collection``.
DATA_POLICY_VALUES = ("allow", "deny")


@dataclass(frozen=True)
class MaxPrice:
    """Per-million-token ceilings, as OpenRouter expresses them."""

    prompt: float | None = None
    completion: float | None = None

    def to_fields(self) -> dict[str, float]:
        fields: dict[str, float] = {}
        if self.prompt is not None:
            fields["prompt"] = self.prompt
        if self.completion is not None:
            fields["completion"] = self.completion
        return fields


@dataclass(frozen=True)
class OpenRouterRoutePolicy:
    """Which upstreams may serve a request, and on what terms (spec §14.3)."""

    #: Preference order. Upstreams not listed remain eligible unless ``allow_fallbacks`` is False.
    provider_order: tuple[str, ...] = ()
    #: An allowlist. Non-empty means *only* these.
    provider_only: tuple[str, ...] = ()
    provider_ignore: tuple[str, ...] = ()
    #: Whether OpenRouter may fall back to an upstream outside ``provider_order``.
    allow_fallbacks: bool = True
    #: Require the upstream to support every parameter in the request. Off by default: on, a request
    #: with one unsupported knob fails instead of being served with that knob dropped — which is the
    #: right behaviour when the knob matters and the wrong one when it does not, so the user chooses.
    require_parameters: bool = False
    #: ``"deny"`` restricts routing to upstreams that do not train on or retain prompts.
    data_policy: str | None = None
    #: Shorthand for the strictest retention posture. Implies ``data_policy="deny"``.
    zero_data_retention: bool = False
    sort: str | None = None
    max_price: MaxPrice | None = None
    #: Free-form extras for fields OpenRouter adds after this was written. Passed through untouched so
    #: a new option does not require a release, and validated only for shape.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sort is not None and self.sort not in SORT_VALUES:
            raise ValueError(f"sort must be one of {SORT_VALUES}, not {self.sort!r}")
        if self.data_policy is not None and self.data_policy not in DATA_POLICY_VALUES:
            raise ValueError(
                f"data_policy must be one of {DATA_POLICY_VALUES}, not {self.data_policy!r}"
            )
        overlap = set(self.provider_only) & set(self.provider_ignore)
        if overlap:
            # A provider both required and forbidden has no defensible interpretation, and picking
            # one silently would route requests somewhere the user believes is excluded.
            raise ValueError(f"providers cannot be both required and ignored: {sorted(overlap)}")

    @property
    def is_default(self) -> bool:
        return self == OpenRouterRoutePolicy()

    def to_request_fields(self) -> dict[str, Any]:
        """The ``provider`` object to merge into a chat request. Empty when nothing was asked for."""

        provider: dict[str, Any] = {}
        if self.provider_order:
            provider["order"] = list(self.provider_order)
        if self.provider_only:
            provider["only"] = list(self.provider_only)
        if self.provider_ignore:
            provider["ignore"] = list(self.provider_ignore)
        if not self.allow_fallbacks:
            provider["allow_fallbacks"] = False
        if self.require_parameters:
            provider["require_parameters"] = True
        policy = "deny" if self.zero_data_retention else self.data_policy
        if policy is not None:
            provider["data_collection"] = policy
        if self.sort is not None:
            provider["sort"] = self.sort
        if self.max_price is not None:
            fields = self.max_price.to_fields()
            if fields:
                provider["max_price"] = fields
        provider.update(self.extra)
        return {"provider": provider} if provider else {}

    def describe(self) -> str:
        """A one-line, secret-free summary for the wizard's review step and Doctor."""

        if self.is_default:
            return "OpenRouter's default routing"
        parts: list[str] = []
        if self.provider_only:
            parts.append(f"only {', '.join(self.provider_only)}")
        elif self.provider_order:
            suffix = "" if self.allow_fallbacks else " (no fallbacks)"
            parts.append(f"prefer {', '.join(self.provider_order)}{suffix}")
        if self.provider_ignore:
            parts.append(f"never {', '.join(self.provider_ignore)}")
        if self.zero_data_retention:
            parts.append("zero data retention")
        elif self.data_policy:
            parts.append(f"data collection {self.data_policy}")
        if self.sort:
            parts.append(f"sorted by {self.sort}")
        if self.max_price is not None and self.max_price.to_fields():
            parts.append("price capped")
        if self.require_parameters:
            parts.append("all parameters required")
        return "; ".join(parts) or "OpenRouter's default routing"


def policy_from_mapping(data: dict[str, Any]) -> OpenRouterRoutePolicy:
    """Rebuild a policy from stored configuration, rejecting unknown keys.

    Strict on purpose: a typo'd key in a persisted routing policy would be silently ignored and the
    request routed under different terms than the file appears to describe.
    """

    known = {
        "provider_order",
        "provider_only",
        "provider_ignore",
        "allow_fallbacks",
        "require_parameters",
        "data_policy",
        "zero_data_retention",
        "sort",
        "max_price",
        "extra",
    }
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown routing policy fields: {sorted(unknown)}")
    price = data.get("max_price")
    return OpenRouterRoutePolicy(
        provider_order=tuple(data.get("provider_order") or ()),
        provider_only=tuple(data.get("provider_only") or ()),
        provider_ignore=tuple(data.get("provider_ignore") or ()),
        allow_fallbacks=bool(data.get("allow_fallbacks", True)),
        require_parameters=bool(data.get("require_parameters", False)),
        data_policy=data.get("data_policy"),
        zero_data_retention=bool(data.get("zero_data_retention", False)),
        sort=data.get("sort"),
        max_price=(
            MaxPrice(prompt=price.get("prompt"), completion=price.get("completion"))
            if isinstance(price, dict)
            else None
        ),
        extra=dict(data.get("extra") or {}),
    )
