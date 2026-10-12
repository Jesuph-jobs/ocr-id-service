"""Tests for tenant queue naming and validation."""

import pytest

from app.queue_worker import parse_app_ids, request_queue_name, response_queue_key


def test_parse_app_ids_normalizes_and_deduplicates() -> None:
    assert parse_app_ids(" billing, Yinvesti ") == ["billing", "yinvesti"]
    with pytest.raises(ValueError, match="duplicate"):
        parse_app_ids("billing, BILLING")


@pytest.mark.parametrize("value", ["", "with space", "bad:queue", "-starts-with-dash", "x" * 64])
def test_parse_app_ids_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_app_ids(value)


def test_tenant_queue_and_response_key_are_app_and_request_scoped() -> None:
    assert request_queue_name("billing") == "ocr-billing-requests"
    assert response_queue_key("billing", "req_123") == "ocr:responses:billing:req_123"
    with pytest.raises(ValueError, match="request ID"):
        response_queue_key("billing", "bad:id")
