"""Tool schema normalization (spec §8.1).

The assertions worth reading twice are the ones about *where* a removed keyword is filed. A test
that only checked "keyword is gone from the wire schema" would pass for both the safe case and the
dangerous one, which is precisely the conflation the normalizer exists to prevent.
"""

from __future__ import annotations

import pytest

from openagent.providers.compat.profiles_v2 import CompatibilityProfile
from openagent.providers.tool_schema import (
    MAX_SCHEMA_BYTES,
    normalize_tool_schema,
    normalize_tool_schemas,
)

pytestmark = pytest.mark.unit


def _profile(**kwargs: object) -> CompatibilityProfile:
    return CompatibilityProfile("test", **kwargs)  # type: ignore[arg-type]


def _tool(parameters: dict, name: str = "read_file", description: str = "d") -> dict:
    return {"name": name, "description": description, "parameters": parameters}


OBJ = {
    "type": "object",
    "properties": {"path": {"type": "string", "maxLength": 4096}},
    "required": ["path"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- happy path


def test_conforming_schema_passes_through_unchanged():
    result = normalize_tool_schema(_tool(OBJ), _profile())

    assert result.executable
    assert result.dropped_keywords == []
    assert result.incompatible_keywords == []
    assert result.normalized_schema["parameters"] == OBJ
    assert result.normalized_schema["name"] == "read_file"


def test_absent_parameters_become_an_empty_object_schema():
    result = normalize_tool_schema({"name": "now", "description": "d"}, _profile())

    assert result.executable
    assert result.normalized_schema["parameters"] == {"type": "object", "properties": {}}


def test_missing_root_type_is_stated_rather_than_left_to_the_provider():
    result = normalize_tool_schema(_tool({"properties": {"a": {"type": "string"}}}), _profile())

    assert result.executable
    assert result.normalized_schema["parameters"]["type"] == "object"
    assert any("assumed 'object'" in w for w in result.warnings)


# --------------------------------------------------------------------------- structure


def test_non_object_root_is_not_executable():
    result = normalize_tool_schema(
        _tool({"type": "array", "items": {"type": "string"}}), _profile()
    )

    assert not result.executable
    assert any("must be an object" in w for w in result.warnings)


def test_non_dict_properties_is_not_executable():
    result = normalize_tool_schema(_tool({"type": "object", "properties": ["path"]}), _profile())

    assert not result.executable
    assert any("'properties' must be an object" in w for w in result.warnings)


def test_parameters_that_are_not_a_schema_object_are_not_executable():
    result = normalize_tool_schema({"name": "t", "parameters": "string"}, _profile())

    assert not result.executable


def test_unnamed_tool_is_not_executable():
    result = normalize_tool_schema({"parameters": OBJ}, _profile())

    assert not result.executable
    assert result.tool_name == "<unnamed>"


def test_unknown_json_type_is_not_executable():
    schema = {"type": "object", "properties": {"a": {"type": "strng"}}}
    result = normalize_tool_schema(_tool(schema), _profile())

    assert not result.executable
    assert any(".type declares an unknown JSON type" in w for w in result.warnings)


# --------------------------------------------------------------------------- required agreement


def test_required_naming_an_undeclared_property_under_closed_object_is_unsatisfiable():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    }
    result = normalize_tool_schema(_tool(schema), _profile())

    assert not result.executable
    assert any("no valid arguments exist" in w for w in result.warnings)


def test_required_naming_an_undeclared_property_under_open_object_only_warns():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "b"]}
    result = normalize_tool_schema(_tool(schema), _profile())

    assert result.executable
    assert any("requires undeclared" in w for w in result.warnings)


def test_required_must_be_a_list_of_names():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"}
    result = normalize_tool_schema(_tool(schema), _profile())

    assert not result.executable


def test_required_agreement_is_checked_on_nested_objects():
    schema = {
        "type": "object",
        "properties": {
            "cfg": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["y"],
                "additionalProperties": False,
            }
        },
    }
    result = normalize_tool_schema(_tool(schema), _profile())

    assert not result.executable
    assert any("parameters.properties.cfg" in w for w in result.warnings)


# --------------------------------------------------------------------------- names


def test_name_longer_than_the_endpoint_allows_is_not_executable():
    result = normalize_tool_schema(_tool(OBJ, name="x" * 65), _profile(tool_name_max_length=64))

    assert not result.executable
    assert any("at most 64" in w for w in result.warnings)


def test_name_outside_the_endpoint_grammar_is_not_executable():
    result = normalize_tool_schema(_tool(OBJ, name="read file!"), _profile())

    assert not result.executable
    assert any("grammar" in w for w in result.warnings)


def test_duplicate_names_disable_the_later_tool_only():
    results = normalize_tool_schemas([_tool(OBJ, name="read"), _tool(OBJ, name="read")], _profile())

    assert results[0].executable
    assert not results[1].executable
    assert any("duplicate tool name" in w for w in results[1].warnings)


def test_a_single_normalize_call_does_not_track_duplicates():
    # Duplicate detection needs cross-tool context; a lone call must not invent it.
    first = normalize_tool_schema(_tool(OBJ, name="read"), _profile())
    second = normalize_tool_schema(_tool(OBJ, name="read"), _profile())

    assert first.executable and second.executable


# --------------------------------------------------------------------------- bounds


def test_schema_over_the_byte_ceiling_is_not_executable():
    big = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string", "description": "x" * 200} for i in range(2000)},
    }
    result = normalize_tool_schema(_tool(big), _profile())

    assert not result.executable
    assert any(str(MAX_SCHEMA_BYTES) in w for w in result.warnings)


def test_schema_deeper_than_the_endpoint_allows_is_not_executable():
    node: dict = {"type": "string"}
    for _ in range(12):
        node = {"type": "object", "properties": {"n": node}}
    result = normalize_tool_schema(_tool(node), _profile(schema_max_depth=6))

    assert not result.executable
    assert any("nests deeper than 6" in w for w in result.warnings)


def test_depth_measurement_terminates_on_a_deeply_nested_schema():
    node: dict = {"type": "string"}
    for _ in range(500):
        node = {"type": "object", "properties": {"n": node}}
    result = normalize_tool_schema(_tool(node), _profile())

    assert not result.executable


def test_enum_over_the_endpoint_limit_is_refused_rather_than_truncated():
    schema = {
        "type": "object",
        "properties": {"c": {"type": "string", "enum": [str(i) for i in range(50)]}},
    }
    result = normalize_tool_schema(_tool(schema), _profile(schema_max_enum_values=10))

    assert not result.executable
    # Truncation would silently drop legal values, so nothing partial is emitted.
    assert any("at most 10" in w for w in result.warnings)


def test_empty_enum_is_not_executable():
    schema = {"type": "object", "properties": {"c": {"enum": []}}}
    result = normalize_tool_schema(_tool(schema), _profile())

    assert not result.executable


# --------------------------------------------------------------------------- the core split


def test_dropping_an_annotation_keyword_is_recorded_as_a_drop_not_an_incompatibility():
    schema = {"type": "object", "properties": {"a": {"type": "string", "title": "A"}}}
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"title"}))
    )

    assert result.executable
    assert result.dropped_keywords == ["title"]
    assert result.incompatible_keywords == []
    assert not result.narrows_validation
    assert "title" not in result.normalized_schema["parameters"]["properties"]["a"]


def test_dropping_a_constraint_keyword_is_recorded_as_an_incompatibility_with_a_warning():
    schema = {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}}
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"pattern"}))
    )

    # Still executable — local validation enforces the original schema — but never silent.
    assert result.executable
    assert result.incompatible_keywords == ["pattern"]
    assert result.dropped_keywords == []
    assert result.narrows_validation
    assert any("told less than the schema requires" in w for w in result.warnings)
    assert "pattern" not in result.normalized_schema["parameters"]["properties"]["a"]


def test_an_unrecognised_unsupported_keyword_is_treated_as_constraining():
    schema = {"type": "object", "properties": {"a": {"type": "string", "x-vendor-rule": "y"}}}
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"x-vendor-rule"}))
    )

    assert result.incompatible_keywords == ["x-vendor-rule"]
    assert any("not preserved" in w for w in result.warnings)


def test_removal_is_reported_once_even_when_the_keyword_appears_repeatedly():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "pattern": "^x"},
            "b": {"type": "string", "pattern": "^y"},
        },
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"pattern"}))
    )

    assert result.incompatible_keywords == ["pattern"]


def test_unsupported_keywords_are_removed_inside_composition_branches():
    schema = {
        "type": "object",
        "properties": {
            "a": {"anyOf": [{"type": "string", "pattern": "^x"}, {"type": "integer"}]},
        },
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"pattern"}))
    )

    branch = result.normalized_schema["parameters"]["properties"]["a"]["anyOf"][0]
    assert "pattern" not in branch
    assert result.incompatible_keywords == ["pattern"]


def test_unsupported_keywords_are_removed_inside_array_items():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "array", "items": {"type": "string", "format": "uri"}}},
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"format"}))
    )

    assert "format" not in result.normalized_schema["parameters"]["properties"]["a"]["items"]


def test_enum_values_are_not_walked_as_subschemas():
    # An enum value may itself be an object; treating it as a schema would corrupt the data.
    schema = {
        "type": "object",
        "properties": {"a": {"enum": [{"properties": {"nested": 1}, "title": "not a schema"}]}},
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"title"}))
    )

    # `title` is stripped from schemas; the identical key inside an enum *value* is data.
    assert result.normalized_schema["parameters"]["properties"]["a"]["enum"] == [
        {"properties": {"nested": 1}, "title": "not a schema"}
    ]
    assert result.dropped_keywords == []


# --------------------------------------------------------------------------- defaults


def test_a_default_that_violates_its_own_schema_is_removed():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1, "default": 0}},
    }
    result = normalize_tool_schema(_tool(schema), _profile())

    assert result.executable
    assert "default" not in result.normalized_schema["parameters"]["properties"]["n"]
    assert result.dropped_keywords == ["default"]
    assert any("does not satisfy its own schema" in w for w in result.warnings)


def test_a_valid_default_is_preserved():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "default": 3}}}
    result = normalize_tool_schema(_tool(schema), _profile())

    assert result.normalized_schema["parameters"]["properties"]["n"]["default"] == 3
    assert result.dropped_keywords == []


def test_an_invalid_subschema_is_not_executable():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": "x", "default": 1}},
    }
    result = normalize_tool_schema(_tool(schema), _profile())

    assert not result.executable
    assert any("not a valid JSON Schema" in w for w in result.warnings)


# --------------------------------------------------------------------------- additionalProperties


def test_endpoint_requiring_closed_objects_tightens_every_object_and_says_so():
    schema = {
        "type": "object",
        "properties": {"cfg": {"type": "object", "properties": {"x": {"type": "string"}}}},
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_requires_additional_properties_false=True)
    )

    params = result.normalized_schema["parameters"]
    assert params["additionalProperties"] is False
    assert params["properties"]["cfg"]["additionalProperties"] is False
    assert result.executable


def test_tightening_an_explicitly_open_object_is_reported():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_requires_additional_properties_false=True)
    )

    assert result.normalized_schema["parameters"]["additionalProperties"] is False
    assert any("stricter than the original" in w for w in result.warnings)


def test_endpoint_forbidding_the_keyword_loses_the_closed_object_guarantee_loudly():
    result = normalize_tool_schema(_tool(OBJ), _profile(schema_forbids_additional_properties=True))

    assert "additionalProperties" not in result.normalized_schema["parameters"]
    assert result.incompatible_keywords == ["additionalProperties"]
    assert any("local validation still does" in w for w in result.warnings)


def test_removing_a_permissive_additional_properties_is_not_an_incompatibility():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_forbids_additional_properties=True)
    )

    assert result.incompatible_keywords == []


# --------------------------------------------------------------------------- reporting


def test_summary_reports_the_unchanged_case():
    assert "sent unchanged" in normalize_tool_schema(_tool(OBJ), _profile()).summary()


def test_summary_reports_a_non_executable_tool_with_its_reason():
    result = normalize_tool_schema(_tool(OBJ, name="bad name"), _profile())

    assert "not executable" in result.summary()


def test_summary_counts_both_removal_kinds():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "pattern": "^x", "title": "A"}},
    }
    result = normalize_tool_schema(
        _tool(schema), _profile(schema_unsupported_keywords=frozenset({"pattern", "title"}))
    )

    assert "1 constraint(s) not expressible" in result.summary()
    assert "1 annotation(s) dropped" in result.summary()


def test_original_schema_is_never_mutated():
    schema = {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}}
    tool = _tool(schema)
    normalize_tool_schema(tool, _profile(schema_unsupported_keywords=frozenset({"pattern"})))

    assert tool["parameters"]["properties"]["a"]["pattern"] == "^x"
