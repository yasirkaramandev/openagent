"""Capability-aware catalog filtering for the wizard (spec §14.2, §22.3).

The interesting question is not how to filter — it is what to do with a model whose capability is
**unknown**, which is most models at most providers, because most catalogs advertise nothing.

Two obvious answers are both wrong. Excluding unknowns hides every model that actually works at a
provider with a thin catalog, which is the majority. Including them silently presents a model that
cannot call tools as though it could, and the user finds out when their agent does nothing.

So a filter *partitions* rather than filters: models that demonstrably match, models that cannot be
ruled out, and models that were ruled out. The wizard shows the first group, offers the second under a
label that says why it is separate, and drops the third. That way "we do not know" stays visible as
its own answer instead of being rounded to yes or no.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .compat.evidence import Capability
from .model_catalog import CatalogEntry


@dataclass(frozen=True)
class ModelFilter:
    """What the user asked the catalog for (spec §14.2)."""

    requires_tools: bool = False
    requires_reasoning: bool = False
    requires_structured_output: bool = False
    requires_image_input: bool = False
    min_context: int | None = None
    exclude_deprecated: bool = True
    #: Price ceilings, per token, as the catalog reports them. ``None`` means no ceiling. A model
    #: whose price the catalog did not state is never silently treated as free.
    max_prompt_price: float | None = None
    max_completion_price: float | None = None
    #: Substring match on id or display name.
    search: str | None = None
    publisher: str | None = None

    @property
    def required_capabilities(self) -> tuple[Capability, ...]:
        wanted: list[Capability] = []
        if self.requires_tools:
            wanted.append(Capability.TOOL_CALLING)
        if self.requires_reasoning:
            wanted.append(Capability.REASONING)
        if self.requires_structured_output:
            wanted.append(Capability.JSON_SCHEMA_OUTPUT)
        if self.requires_image_input:
            wanted.append(Capability.IMAGE_INPUT)
        return tuple(wanted)

    @property
    def is_empty(self) -> bool:
        return self == ModelFilter()


@dataclass
class FilterResult:
    """A partition, not a list."""

    #: Every required capability is determined and supported.
    matched: list[CatalogEntry] = field(default_factory=list)
    #: At least one required capability is UNKNOWN and none is known-unsupported. Offered separately
    #: with the reason, because a probe is the only thing that can settle it.
    unknown: list[CatalogEntry] = field(default_factory=list)
    #: A required capability is known to be unsupported, or a hard bound was violated.
    excluded: list[CatalogEntry] = field(default_factory=list)
    #: Per-model reason for landing in ``unknown``, keyed by model id.
    unknown_reasons: dict[str, list[str]] = field(default_factory=dict)

    @property
    def selectable(self) -> list[CatalogEntry]:
        """Everything the wizard may offer, matches first."""

        return [*self.matched, *self.unknown]

    def summary(self) -> str:
        parts = [f"{len(self.matched)} match"]
        if self.unknown:
            parts.append(f"{len(self.unknown)} unverified")
        if self.excluded:
            parts.append(f"{len(self.excluded)} excluded")
        return ", ".join(parts)


def apply_filter(entries: list[CatalogEntry], model_filter: ModelFilter) -> FilterResult:
    """Partition ``entries`` against ``model_filter``.

    Hard bounds (context window, price, deprecation, text search) exclude outright: they are facts the
    catalog stated, so a violation is a real negative. Capability requirements go through the ledger,
    where the three-way answer is preserved.
    """

    result = FilterResult()
    needle = (model_filter.search or "").strip().lower()
    publisher = (model_filter.publisher or "").strip().lower()

    for entry in entries:
        model = entry.model
        if (
            needle
            and needle not in model.id.lower()
            and needle not in (model.display_name or "").lower()
        ):
            continue  # not a match and not a rejection — simply out of scope for this query
        if publisher and (model.owned_by or "").lower() != publisher:
            continue

        if model_filter.exclude_deprecated and entry.deprecated:
            result.excluded.append(entry)
            continue
        if model_filter.min_context is not None:
            if model.context_window is None:
                # An unstated context window cannot be asserted to meet a minimum. Treated as
                # unverified rather than excluded, so a thin catalog does not empty the list.
                result.unknown.append(entry)
                result.unknown_reasons.setdefault(model.id, []).append(
                    "the catalog did not state a context window"
                )
                continue
            if model.context_window < model_filter.min_context:
                result.excluded.append(entry)
                continue
        if _over_price(entry.prompt_price, model_filter.max_prompt_price) or _over_price(
            entry.completion_price, model_filter.max_completion_price
        ):
            result.excluded.append(entry)
            continue

        ledger = entry.ledger()
        unknown_reasons: list[str] = []
        rejected = False
        for capability in model_filter.required_capabilities:
            supported = ledger.supports(capability)
            if supported is False:
                rejected = True
                break
            if supported is None:
                unknown_reasons.append(
                    f"{capability.value} is not established for this model; a live probe would settle it"
                )
        if rejected:
            result.excluded.append(entry)
        elif unknown_reasons:
            result.unknown.append(entry)
            result.unknown_reasons.setdefault(model.id, []).extend(unknown_reasons)
        else:
            result.matched.append(entry)
    return result


def _over_price(actual: float | None, ceiling: float | None) -> bool:
    """Whether a price ceiling is violated.

    An *unstated* price is not over the ceiling: it is unknown, and excluding it would hide models
    from providers that publish no pricing at all. It is also not treated as free — the caller sees
    ``prompt_price is None`` and can say so.
    """

    if ceiling is None or actual is None:
        return False
    return actual > ceiling
