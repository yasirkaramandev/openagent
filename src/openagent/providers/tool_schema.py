"""Tool schema normalization (spec §8.1).

Every provider accepts "JSON Schema" and every provider means something slightly different by it.
The differences are not evenly dangerous, and the whole point of this module is to stop treating
them as if they were.

Dropping ``title`` costs nothing: it annotates, it does not decide which arguments are valid.
Dropping ``pattern`` is a different act entirely — it *widens* the set of arguments the model is
told are acceptable, and it does so invisibly. The tool still runs, the schema still looks right in
the wizard, and the only symptom is that the model starts emitting arguments the author meant to
forbid. That is the failure this module exists to prevent, so the two cases are never mixed:
annotations land in ``dropped_keywords`` and constraints land in ``incompatible_keywords``, which
the caller is expected to surface rather than swallow.

Three things follow from that split:

* A narrowing loss is always reported. It is not silent, which is what spec §8.1 requires.
* A narrowing loss is not automatically fatal. OpenAgent validates tool arguments locally against
  the *original* schema before executing anything (``tools/registry.py``), so a constraint the
  provider could not express is still enforced at the point it matters. The cost is a worse prompt
  and more rejected calls, not an unchecked argument.
* Something structurally broken *is* fatal. ``executable=False`` means the tool must not be offered
  at all, because no correct request can be built from it — an unsatisfiable ``required``, a name
  the endpoint will reject, a schema deeper or larger than the endpoint accepts.

The normalizer never rewrites a constraint into a weaker one, and never invents a constraint that
was not there. When it cannot express something faithfully it says so and leaves the decision to
the caller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .compat.profiles_v2 import CompatibilityProfile

#: A tool schema larger than this is refused rather than sent. The ceiling is per-tool; a request
#: carrying many tools is bounded by the caller's own history/request budget.
MAX_SCHEMA_BYTES = 128 * 1024

#: Absolute nesting ceiling, independent of what a profile claims. A profile may lower it; nothing
#: may raise it past this, because the recursion below has to terminate on adversarial input.
MAX_SCHEMA_DEPTH = 32

#: Keywords that annotate without constraining. Removing one cannot change which inputs validate,
#: so these are safe to drop for an endpoint that rejects them.
ANNOTATION_KEYWORDS = frozenset(
    {
        "title",
        "$comment",
        "examples",
        "readOnly",
        "writeOnly",
        "deprecated",
        "$id",
        "$schema",
        "$anchor",
        "contentMediaType",
        "contentEncoding",
    }
)

#: Keywords that decide validity. Removing one widens what the schema accepts, so removal is
#: recorded as an incompatibility rather than a drop — see the module docstring.
CONSTRAINT_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "patternProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "dependentSchemas",
        "dependencies",
        "items",
        "prefixItems",
        "additionalItems",
        "contains",
        "minContains",
        "maxContains",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "$ref",
        "$defs",
        "definitions",
    }
)

#: Where a subschema may legally appear, so the walk recurses into schemas and not into arbitrary
#: user data (a ``default`` or ``enum`` value may itself be an object and must never be treated as
#: a schema).
_SUBSCHEMA_KEYS = frozenset({"items", "contains", "not", "if", "then", "else", "additionalItems"})
_SUBSCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SUBSCHEMA_MAP_KEYS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)

_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


@dataclass
class ToolSchemaNormalizationResult:
    """The outcome of normalizing one tool for one endpoint (spec §8.1).

    ``executable`` is the field callers must branch on. ``incompatible_keywords`` being non-empty
    is a *reportable degradation*, not a refusal: the schema sent to the provider is less precise
    than the one OpenAgent enforces locally.
    """

    tool_name: str
    normalized_schema: dict[str, Any] = field(default_factory=dict)
    #: Annotation keywords removed. No effect on which arguments validate.
    dropped_keywords: list[str] = field(default_factory=list)
    #: Constraint keywords the endpoint cannot express, removed from the wire schema. Each one
    #: widens what the model is told it may send; local validation still enforces the original.
    incompatible_keywords: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: ``False`` when no correct request can be built from this tool at all.
    executable: bool = True

    @property
    def narrows_validation(self) -> bool:
        """Whether the wire schema accepts strictly more than the original."""

        return bool(self.incompatible_keywords)

    def summary(self) -> str:
        if not self.executable:
            return (
                f"{self.tool_name}: not executable — {'; '.join(self.warnings) or 'invalid schema'}"
            )
        parts = []
        if self.incompatible_keywords:
            parts.append(f"{len(self.incompatible_keywords)} constraint(s) not expressible")
        if self.dropped_keywords:
            parts.append(f"{len(self.dropped_keywords)} annotation(s) dropped")
        return f"{self.tool_name}: " + (", ".join(parts) if parts else "sent unchanged")


def normalize_tool_schema(
    schema: dict[str, Any],
    profile: CompatibilityProfile,
    *,
    _seen_names: set[str] | None = None,
) -> ToolSchemaNormalizationResult:
    """Normalize one tool definition (``{"name", "description", "parameters"}``) for ``profile``.

    Returns a result rather than raising: one unusable tool should disable that tool, not fail the
    whole run. Callers pass ``_seen_names`` only via :func:`normalize_tool_schemas`.
    """

    name = schema.get("name")
    # A blank name is labelled the same as a missing one: this label is what a "tool withheld"
    # report shows the user, and `''` names nothing they can act on.
    result = ToolSchemaNormalizationResult(
        tool_name=name if isinstance(name, str) and name.strip() else "<unnamed>"
    )

    if not isinstance(name, str) or not name.strip():
        result.executable = False
        result.warnings.append("tool has no name")
        return result

    _check_name(name, profile, result, _seen_names)

    parameters = schema.get("parameters")
    if parameters is None:
        # A tool taking no arguments is legitimate; give the endpoint the empty object shape it
        # expects rather than omitting the field and letting each provider guess.
        parameters = {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        result.executable = False
        result.warnings.append("tool parameters must be a JSON Schema object")
        return result

    # Depth first, then size. _measure_depth stops as soon as it passes the limit, so it is bounded
    # by the profile's own ceiling; serializing is not, and a 500-deep hostile schema exhausts the
    # stack inside json.dumps before anything gets to reject it. The cheap bounded structural check
    # has to gate the expensive unbounded one, not follow it.
    if not _check_depth(parameters, profile, result):
        return result
    if not _check_size(parameters, result):
        return result

    normalized = _walk(parameters, profile, result, path="parameters", depth=0)
    if not isinstance(normalized, dict):  # pragma: no cover - _walk returns a dict for a dict input
        result.executable = False
        result.warnings.append("tool parameters did not normalize to an object")
        return result

    _check_root_object(normalized, result)
    _check_required_agreement(normalized, result, path="parameters")
    _apply_additional_properties_policy(normalized, profile, result)

    result.normalized_schema = {
        "name": name,
        "description": schema.get("description", "") or "",
        "parameters": normalized,
    }
    return result


def normalize_tool_schemas(
    schemas: list[dict[str, Any]], profile: CompatibilityProfile
) -> list[ToolSchemaNormalizationResult]:
    """Normalize a whole tool list, so duplicate names are visible across tools.

    A duplicate name is fatal for the *later* tool only. Disabling both would be a worse outcome
    than keeping the first definition, and picking silently is exactly what this module refuses to
    do — so the collision is reported on the tool that caused it.
    """

    seen: set[str] = set()
    return [normalize_tool_schema(schema, profile, _seen_names=seen) for schema in schemas]


# ------------------------------------------------------------------------------- checks


def _check_name(
    name: str,
    profile: CompatibilityProfile,
    result: ToolSchemaNormalizationResult,
    seen: set[str] | None,
) -> None:
    if len(name) > profile.tool_name_max_length:
        result.executable = False
        result.warnings.append(
            f"tool name is {len(name)} characters; this endpoint accepts at most "
            f"{profile.tool_name_max_length}"
        )
    try:
        matches = re.fullmatch(profile.tool_name_pattern, name) is not None
    except re.error:  # pragma: no cover - a malformed profile pattern is a bug, not user input
        matches = True
        result.warnings.append("provider profile has an invalid tool-name pattern; name unchecked")
    if not matches:
        result.executable = False
        result.warnings.append(
            f"tool name does not match this endpoint's grammar {profile.tool_name_pattern!r}"
        )
    if seen is not None:
        if name in seen:
            result.executable = False
            result.warnings.append(f"duplicate tool name {name!r}; an earlier tool already uses it")
        else:
            seen.add(name)


def _check_size(parameters: dict[str, Any], result: ToolSchemaNormalizationResult) -> bool:
    try:
        size = len(json.dumps(parameters, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        result.executable = False
        result.warnings.append("tool parameters are not JSON-serializable")
        return False
    if size > MAX_SCHEMA_BYTES:
        result.executable = False
        result.warnings.append(
            f"tool schema is {size} bytes, over the {MAX_SCHEMA_BYTES}-byte ceiling"
        )
        return False
    return True


def _check_depth(
    parameters: dict[str, Any], profile: CompatibilityProfile, result: ToolSchemaNormalizationResult
) -> bool:
    limit = min(profile.schema_max_depth, MAX_SCHEMA_DEPTH)
    depth = _measure_depth(parameters, limit)
    if depth > limit:
        result.executable = False
        result.warnings.append(
            f"tool schema nests deeper than {limit} levels, which this endpoint does not accept"
        )
        return False
    return True


def _measure_depth(node: Any, limit: int, depth: int = 0) -> int:
    """Structural depth, short-circuited at ``limit + 1``.

    Bounded on purpose: the caller only needs to know *whether* the limit was passed, and walking
    an adversarially deep schema to the bottom to produce an exact number is the same denial of
    service the limit exists to prevent.
    """

    if depth > limit:
        return depth
    if isinstance(node, dict):
        if not node:
            return depth
        return max(_measure_depth(v, limit, depth + 1) for v in node.values())
    if isinstance(node, list):
        if not node:
            return depth
        return max(_measure_depth(v, limit, depth + 1) for v in node)
    return depth


def _check_root_object(normalized: dict[str, Any], result: ToolSchemaNormalizationResult) -> None:
    root_type = normalized.get("type")
    if root_type is None:
        # Tool arguments are a named-argument mapping in every protocol here, so an absent root
        # type is unambiguous and safe to state explicitly rather than leave to the provider.
        normalized["type"] = "object"
        result.warnings.append("tool schema had no root type; assumed 'object'")
    elif root_type != "object":
        result.executable = False
        result.warnings.append(
            f"tool schema root type is {root_type!r}; tool arguments must be an object"
        )
    properties = normalized.get("properties")
    if properties is None:
        normalized["properties"] = {}
    elif not isinstance(properties, dict):
        result.executable = False
        result.warnings.append("tool schema 'properties' must be an object")


def _check_required_agreement(
    node: dict[str, Any], result: ToolSchemaNormalizationResult, *, path: str
) -> None:
    """Every ``required`` name must be declared, and must be *satisfiable*.

    An undeclared required name is legal JSON Schema (it means "present but unconstrained") and is
    usually a typo. Combined with ``additionalProperties: false`` it is worse than a typo: the
    property may not be supplied and must be supplied, so no input validates and every call the
    model makes will be rejected. That case is fatal; the looser one is a warning.
    """

    required = node.get("required")
    if required is None:
        return
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        result.executable = False
        result.warnings.append(f"{path}.required must be a list of property names")
        return
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return
    missing = [item for item in required if item not in properties]
    if not missing:
        return
    if node.get("additionalProperties") is False:
        result.executable = False
        result.warnings.append(
            f"{path} requires {', '.join(sorted(missing))} but neither declares them nor allows "
            f"additional properties, so no valid arguments exist"
        )
    else:
        result.warnings.append(
            f"{path} requires undeclared propert{'y' if len(missing) == 1 else 'ies'} "
            f"{', '.join(sorted(missing))}"
        )


def _apply_additional_properties_policy(
    node: dict[str, Any], profile: CompatibilityProfile, result: ToolSchemaNormalizationResult
) -> None:
    """Reconcile the schema's ``additionalProperties`` with what the endpoint demands.

    Tightening (adding ``false`` where the schema was open) is a *narrowing* of what the model may
    send. It cannot make an otherwise-valid call invalid at execution time, because OpenAgent's
    local validation uses the original schema — but the model is now told it may not pass extras it
    previously could, so the change is recorded rather than made quietly.
    """

    if profile.schema_forbids_additional_properties:
        _strip_additional_properties(node, result)
        return
    if profile.schema_requires_additional_properties_false:
        _require_additional_properties_false(node, result, path="parameters")


def _strip_additional_properties(node: Any, result: ToolSchemaNormalizationResult) -> None:
    if isinstance(node, dict):
        if "additionalProperties" in node:
            removed = node.pop("additionalProperties")
            # Removing `false` opens the object up; removing `true` changes nothing.
            if removed is not True and "additionalProperties" not in result.incompatible_keywords:
                result.incompatible_keywords.append("additionalProperties")
                result.warnings.append(
                    "this endpoint rejects 'additionalProperties'; the wire schema no longer "
                    "forbids extra properties (local validation still does)"
                )
        for value in node.values():
            _strip_additional_properties(value, result)
    elif isinstance(node, list):
        for value in node:
            _strip_additional_properties(value, result)


def _require_additional_properties_false(
    node: dict[str, Any], result: ToolSchemaNormalizationResult, *, path: str
) -> None:
    if node.get("type") == "object" or "properties" in node:
        if node.get("additionalProperties") is not False:
            existed = "additionalProperties" in node
            node["additionalProperties"] = False
            if existed:
                result.warnings.append(
                    f"{path} allowed additional properties; this endpoint requires them to be "
                    f"forbidden, so the wire schema is stricter than the original"
                )
    properties = node.get("properties")
    if isinstance(properties, dict):
        for key, value in properties.items():
            if isinstance(value, dict):
                _require_additional_properties_false(value, result, path=f"{path}.{key}")


# ------------------------------------------------------------------------------- walk


def _walk(
    node: dict[str, Any],
    profile: CompatibilityProfile,
    result: ToolSchemaNormalizationResult,
    *,
    path: str,
    depth: int,
) -> dict[str, Any]:
    """Copy a subschema, removing what the endpoint cannot take and recording why."""

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in profile.schema_unsupported_keywords:
            _record_removal(key, result, path=path)
            continue
        if key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {
                sub_key: (
                    _walk(sub, profile, result, path=f"{path}.{key}.{sub_key}", depth=depth + 1)
                    if isinstance(sub, dict)
                    else sub
                )
                for sub_key, sub in value.items()
            }
            continue
        if key in _SUBSCHEMA_LIST_KEYS and isinstance(value, list):
            out[key] = [
                (
                    _walk(sub, profile, result, path=f"{path}.{key}[{index}]", depth=depth + 1)
                    if isinstance(sub, dict)
                    else sub
                )
                for index, sub in enumerate(value)
            ]
            continue
        if key in _SUBSCHEMA_KEYS and isinstance(value, dict):
            out[key] = _walk(value, profile, result, path=f"{path}.{key}", depth=depth + 1)
            continue
        out[key] = value

    _check_type_value(out, result, path=path)
    _check_enum(out, profile, result, path=path)
    _check_default(out, result, path=path)
    if path != "parameters":
        _check_required_agreement(out, result, path=path)
    return out


def _record_removal(key: str, result: ToolSchemaNormalizationResult, *, path: str) -> None:
    """File a removed keyword under the consequence of removing it, not under its name."""

    if key in ANNOTATION_KEYWORDS:
        if key not in result.dropped_keywords:
            result.dropped_keywords.append(key)
        return
    if key not in result.incompatible_keywords:
        result.incompatible_keywords.append(key)
        if key in CONSTRAINT_KEYWORDS:
            result.warnings.append(
                f"this endpoint does not accept {key!r}; {path} is sent without it, so the model "
                f"is told less than the schema requires (local validation still enforces it)"
            )
        else:
            # An unrecognised keyword may be an extension that constrains something. Assuming it is
            # harmless is the assumption this module is written to avoid.
            result.warnings.append(
                f"this endpoint does not accept the unrecognised keyword {key!r} at {path}; it was "
                f"removed and its effect, if any, is not preserved"
            )


def _check_type_value(
    node: dict[str, Any], result: ToolSchemaNormalizationResult, *, path: str
) -> None:
    declared = node.get("type")
    if declared is None:
        return
    values = declared if isinstance(declared, list) else [declared]
    unknown = [v for v in values if not isinstance(v, str) or v not in _JSON_TYPES]
    if unknown:
        result.executable = False
        result.warnings.append(f"{path}.type declares an unknown JSON type")


def _check_enum(
    node: dict[str, Any],
    profile: CompatibilityProfile,
    result: ToolSchemaNormalizationResult,
    *,
    path: str,
) -> None:
    values = node.get("enum")
    if values is None:
        return
    if not isinstance(values, list) or not values:
        result.executable = False
        result.warnings.append(f"{path}.enum must be a non-empty list")
        return
    if len(values) > profile.schema_max_enum_values:
        # Truncating would silently drop legal values, so the tool is refused instead.
        result.executable = False
        result.warnings.append(
            f"{path}.enum has {len(values)} values; this endpoint accepts at most "
            f"{profile.schema_max_enum_values}"
        )


def _check_default(
    node: dict[str, Any], result: ToolSchemaNormalizationResult, *, path: str
) -> None:
    """Drop a ``default`` that its own subschema rejects.

    ``default`` is an annotation, so removing it changes nothing about validity — but leaving a
    malformed one in place invites the model to send it, producing a call that fails local
    validation for a reason the model has no way to see.
    """

    if "default" not in node:
        return
    subschema = {k: v for k, v in node.items() if k != "default"}
    if not subschema:
        return
    try:
        jsonschema.validate(node["default"], subschema)
    except jsonschema.ValidationError:
        node.pop("default")
        if "default" not in result.dropped_keywords:
            result.dropped_keywords.append("default")
        result.warnings.append(f"{path}.default does not satisfy its own schema; it was removed")
    except jsonschema.SchemaError:
        result.executable = False
        result.warnings.append(f"{path} is not a valid JSON Schema")
