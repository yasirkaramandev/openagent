"""Structured provider model discovery keeps empty catalogs distinct from failures."""

from __future__ import annotations

import asyncio

import pytest

from openagent.core.errors import ErrorType
from openagent.core.models import RemoteModel
from openagent.providers.base import ModelCatalogError, parse_model_catalog
from openagent.providers.discovery import discover_models
from openagent.providers.transport import TransportError


class _Adapter:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    async def list_models(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


async def test_valid_empty_catalog_is_not_reported_as_an_error():
    result = await discover_models(_Adapter([]), source="test:/models")
    assert result.ok is True
    assert result.models == []
    assert result.partial is False
    assert result.error_type is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TransportError(ErrorType.AUTHENTICATION_FAILED, "bad key", 401), "unauthorized"),
        (TransportError(ErrorType.PERMISSION_DENIED, "denied", 403), "forbidden"),
        (TransportError(ErrorType.PROVIDER_RATE_LIMITED, "slow down", 429), "rate_limited"),
        (TransportError(ErrorType.TIMEOUT, "timed out"), "timeout"),
        (TransportError(ErrorType.CONNECTION_LOST, "network failed"), "network"),
        (TransportError(ErrorType.MODEL_NOT_FOUND, "missing", 404), "endpoint_unsupported"),
        (
            TransportError(ErrorType.INVALID_REQUEST, "provider returned invalid JSON"),
            "malformed_response",
        ),
        (OSError("DNS name resolution failed"), "network"),
    ],
)
async def test_discovery_failures_are_classified(error: BaseException, expected: str):
    result = await discover_models(_Adapter(error), source="test:/models")
    assert result.ok is False
    assert result.models == []
    assert result.error_type == expected
    assert result.source == "test:/models"


async def test_malformed_catalog_can_return_an_honest_partial_result():
    valid = RemoteModel(id="valid-model")
    result = await discover_models(
        _Adapter(ModelCatalogError("one malformed entry", models=[valid])),
        source="test:/models",
    )
    assert result.ok is False
    assert result.partial is True
    assert result.models == [valid]
    assert result.error_type == "malformed_response"


def test_catalog_parser_distinguishes_empty_partial_and_malformed():
    assert parse_model_catalog({"data": []}) == []
    with pytest.raises(ModelCatalogError) as excinfo:
        parse_model_catalog({"data": [{"id": "good"}, {"owned_by": "broken"}]})
    assert [model.id for model in excinfo.value.models] == ["good"]
    with pytest.raises(ModelCatalogError, match="data array"):
        parse_model_catalog({"data": {"id": "not-a-list"}})


async def test_async_cancellation_is_never_normalized_into_a_discovery_result():
    with pytest.raises(asyncio.CancelledError):
        await discover_models(_Adapter(asyncio.CancelledError()), source="test:/models")
