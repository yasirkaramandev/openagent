"""Per-provider error refinement (spec §8.4, §15.4, §18.1, §23).

Every provider reports the same handful of real conditions with different words and, worse, with
different HTTP statuses. A 402 is "insufficient balance" at DeepSeek and nothing at all elsewhere; a
401 from Kimi's international endpoint with a China key is a *region* mismatch, and telling the user
their key is invalid sends them to rotate a key that was fine. So the status classification is a
floor, and each provider gets a refiner on top of it.

The two properties worth protecting: a refiner may only ever *narrow* to something more specific,
and a refiner that does not recognise the payload must leave the base classification alone rather
than guess.
"""

from __future__ import annotations

import pytest

from openagent.core.errors import NON_RETRYABLE, RETRYABLE, ErrorType
from openagent.providers.error_mapping import (
    ProviderErrorSignal,
    known_mappers,
    map_provider_error,
)


def signal(**kwargs: object) -> ProviderErrorSignal:
    base = {
        "status": None,
        "message": "",
        "base": ErrorType.UNKNOWN,
        "body": None,
    }
    base.update(kwargs)
    return ProviderErrorSignal(**base)  # type: ignore[arg-type]


class TestUnknownMapperIsInert:
    def test_unknown_mapper_name_returns_the_base_classification(self) -> None:
        result = map_provider_error(
            "a-provider-nobody-wrote-a-refiner-for",
            signal(status=500, base=ErrorType.PROVIDER_OVERLOADED),
        )
        assert result is ErrorType.PROVIDER_OVERLOADED

    def test_openai_mapper_passes_unrecognized_payloads_through(self) -> None:
        result = map_provider_error(
            "openai", signal(status=418, message="teapot", base=ErrorType.UNKNOWN)
        )
        assert result is ErrorType.UNKNOWN


class TestDeepSeek:
    def test_402_is_insufficient_balance_not_permission_denied(self) -> None:
        result = map_provider_error(
            "deepseek",
            signal(status=402, message="Insufficient Balance", base=ErrorType.PERMISSION_DENIED),
        )
        assert result is ErrorType.INSUFFICIENT_BALANCE

    def test_422_is_a_validation_error_distinct_from_a_bad_request(self) -> None:
        result = map_provider_error(
            "deepseek",
            signal(status=422, message="Invalid Parameters", base=ErrorType.UNKNOWN),
        )
        assert result is ErrorType.INVALID_REQUEST

    def test_retired_alias_is_reported_as_deprecated_not_missing(self) -> None:
        result = map_provider_error(
            "deepseek",
            signal(
                status=400,
                message="Model deepseek-chat is deprecated, use deepseek-v3 instead",
                base=ErrorType.INVALID_REQUEST,
            ),
        )
        assert result is ErrorType.MODEL_DEPRECATED

    def test_503_stays_overloaded(self) -> None:
        result = map_provider_error(
            "deepseek", signal(status=503, base=ErrorType.PROVIDER_OVERLOADED)
        )
        assert result is ErrorType.PROVIDER_OVERLOADED


class TestRegionMismatch:
    """A key that is valid for the *other* endpoint (spec §18.1, §16.2)."""

    @pytest.mark.parametrize("mapper", ["kimi", "qwen"])
    def test_401_naming_the_other_region_is_a_region_mismatch(self, mapper: str) -> None:
        result = map_provider_error(
            mapper,
            signal(
                status=401,
                message="This API key belongs to a different region; use the China endpoint",
                base=ErrorType.AUTHENTICATION_FAILED,
            ),
        )
        assert result is ErrorType.PROVIDER_REGION_MISMATCH

    @pytest.mark.parametrize("mapper", ["kimi", "qwen"])
    def test_a_plain_401_is_still_an_auth_failure(self, mapper: str) -> None:
        result = map_provider_error(
            mapper,
            signal(status=401, message="Invalid api key", base=ErrorType.AUTHENTICATION_FAILED),
        )
        assert result is ErrorType.AUTHENTICATION_FAILED

    def test_qwen_workspace_rejection_is_a_region_or_workspace_mismatch(self) -> None:
        result = map_provider_error(
            "qwen",
            signal(
                status=403,
                message="Model.AccessDenied: workspace is not permitted in this region",
                base=ErrorType.PERMISSION_DENIED,
            ),
        )
        assert result is ErrorType.PROVIDER_REGION_MISMATCH


class TestLocalServers:
    @pytest.mark.parametrize("mapper", ["ollama", "lmstudio"])
    def test_connection_refused_is_local_server_unavailable(self, mapper: str) -> None:
        result = map_provider_error(
            mapper,
            signal(message="All connection attempts failed", base=ErrorType.CONNECTION_LOST),
        )
        assert result is ErrorType.LOCAL_SERVER_UNAVAILABLE

    def test_ollama_missing_model_asks_for_a_pull_not_a_catalog_refresh(self) -> None:
        result = map_provider_error(
            "ollama",
            signal(
                status=404,
                message='model "llama4" not found, try pulling it first',
                base=ErrorType.MODEL_NOT_FOUND,
            ),
        )
        assert result is ErrorType.MODEL_NOT_FOUND

    def test_ollama_out_of_memory_is_its_own_state(self) -> None:
        result = map_provider_error(
            "ollama",
            signal(
                status=500,
                message="model requires more system memory (12.0 GiB) than is available",
                base=ErrorType.UNKNOWN,
            ),
        )
        assert result is ErrorType.LOCAL_MODEL_OUT_OF_MEMORY

    def test_lmstudio_unloaded_model_is_recoverable_by_loading_it(self) -> None:
        result = map_provider_error(
            "lmstudio",
            signal(
                status=404,
                message="No models loaded. Load a model with 'lms load'.",
                base=ErrorType.MODEL_NOT_FOUND,
            ),
        )
        assert result is ErrorType.LOCAL_MODEL_NOT_LOADED

    def test_a_local_server_timeout_is_not_reclassified_as_unavailable(self) -> None:
        """A slow local model is not a down daemon, and the remedies differ."""

        result = map_provider_error(
            "ollama", signal(base=ErrorType.TIMEOUT, message="read timeout")
        )
        assert result is ErrorType.TIMEOUT


class TestGemini:
    def test_dead_interaction_is_an_expired_remote_session(self) -> None:
        result = map_provider_error(
            "gemini",
            signal(
                status=400,
                message="previous_interaction_id not found",
                base=ErrorType.INVALID_REQUEST,
            ),
        )
        assert result is ErrorType.REMOTE_SESSION_EXPIRED

    def test_unknown_field_is_a_parameter_problem_not_a_prompt_problem(self) -> None:
        result = map_provider_error(
            "gemini",
            signal(
                status=400,
                message="Invalid JSON payload received. Unknown field: tool_stream",
                base=ErrorType.INVALID_REQUEST,
            ),
        )
        assert result is ErrorType.UNSUPPORTED_PARAMETER


class TestOpenRouterAndGlmAndMiniMax:
    def test_openrouter_no_provider_available_is_overloaded_not_missing(self) -> None:
        result = map_provider_error(
            "openrouter",
            signal(
                status=502,
                message="No allowed providers are available for the selected model",
                base=ErrorType.UNKNOWN,
            ),
        )
        assert result is ErrorType.PROVIDER_OVERLOADED

    def test_openrouter_credit_exhaustion_is_insufficient_balance(self) -> None:
        result = map_provider_error(
            "openrouter",
            signal(status=402, message="Insufficient credits", base=ErrorType.PERMISSION_DENIED),
        )
        assert result is ErrorType.INSUFFICIENT_BALANCE

    def test_glm_rejects_tool_choice_as_a_parameter_error(self) -> None:
        result = map_provider_error(
            "glm",
            signal(
                status=400,
                message="1214: tool_choice required is not supported",
                base=ErrorType.INVALID_REQUEST,
            ),
        )
        assert result is ErrorType.UNSUPPORTED_PARAMETER

    def test_minimax_insufficient_balance_code(self) -> None:
        result = map_provider_error(
            "minimax",
            signal(
                status=200,
                message="",
                base=ErrorType.UNKNOWN,
                body={"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}},
            ),
        )
        assert result is ErrorType.INSUFFICIENT_BALANCE

    def test_minimax_rate_limit_code(self) -> None:
        result = map_provider_error(
            "minimax",
            signal(
                status=200,
                base=ErrorType.UNKNOWN,
                body={"base_resp": {"status_code": 1002, "status_msg": "rate limit"}},
            ),
        )
        assert result is ErrorType.PROVIDER_RATE_LIMITED

    def test_minimax_success_code_does_not_invent_an_error(self) -> None:
        result = map_provider_error(
            "minimax",
            signal(status=200, base=ErrorType.UNKNOWN, body={"base_resp": {"status_code": 0}}),
        )
        assert result is ErrorType.UNKNOWN


class TestRefinersNeverEscapeTheTaxonomy:
    def test_every_mapper_returns_a_classified_error_for_every_status(self) -> None:
        """A refiner may narrow, but never to something outside the taxonomy's two sets.

        A returned type that is in neither RETRYABLE nor NON_RETRYABLE would be silently treated as
        non-retryable by ``is_retryable`` while nothing asserts that was intended.
        """

        classified = RETRYABLE | NON_RETRYABLE
        for mapper in known_mappers():
            for status in (400, 401, 402, 403, 404, 422, 429, 500, 502, 503, 504):
                result = map_provider_error(
                    mapper,
                    signal(
                        status=status,
                        message="region workspace memory balance",
                        base=ErrorType.UNKNOWN,
                    ),
                )
                assert isinstance(result, ErrorType)
                if result is not ErrorType.UNKNOWN:
                    assert result in classified, f"{mapper} -> {result} is unclassified"

    def test_a_refiner_cannot_turn_a_non_retryable_into_a_retryable_one(self) -> None:
        """The dangerous direction: retrying a 401 four times bills nothing but locks accounts."""

        for mapper in known_mappers():
            result = map_provider_error(
                mapper,
                signal(status=401, message="invalid key", base=ErrorType.AUTHENTICATION_FAILED),
            )
            assert result not in RETRYABLE
