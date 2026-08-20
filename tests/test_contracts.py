from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rolling_budget_api.main import create_app
from rolling_budget_api.schemas.config import CategoryConfigInput, ConfigPutRequest
from rolling_budget_api.schemas.refresh import (
    RefreshBeginRequest,
    RefreshCommitRequest,
    TransactionUpload,
)


def _transaction(**overrides: object) -> dict[str, object]:
    transaction: dict[str, object] = {
        "account_id": "synthetic-checking",
        "account_name": "Everyday Checking",
        "source_id": "synthetic-tx-001",
        "date": date(2026, 8, 19),
        "amount": Decimal("42.50"),
        "currency": "USD",
        "pending": False,
        "name": "Synthetic Bistro",
        "merchant": "Synthetic Bistro",
        "categories": ["restaurant"],
        "refunded": False,
        "refund_amount": Decimal("0"),
    }
    transaction.update(overrides)
    return transaction


def _category(key: str) -> CategoryConfigInput:
    return CategoryConfigInput(
        key=key,
        name=key.title(),
        budget_limit=Decimal("500"),
        lookback_days=30,
        classification_instruction=f"Synthetic rule for {key}",
    )


def test_unmatched_transactions_are_omitted_and_upload_requires_categories() -> None:
    with pytest.raises(ValidationError):
        TransactionUpload.model_validate(_transaction(categories=[]))


def test_multilabel_is_supported_but_duplicate_labels_are_rejected() -> None:
    multilabel = TransactionUpload.model_validate(_transaction(categories=["restaurant", "dating"]))
    assert multilabel.categories == ["dating", "restaurant"]

    with pytest.raises(ValidationError, match="cannot contain duplicates"):
        TransactionUpload.model_validate(_transaction(categories=["restaurant", "restaurant"]))


@pytest.mark.parametrize(
    ("refunded", "refund_amount"),
    [(False, Decimal("1")), (True, Decimal("0")), (True, Decimal("50"))],
)
def test_refund_flags_and_amount_must_agree(
    refunded: bool,
    refund_amount: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        TransactionUpload.model_validate(
            _transaction(refunded=refunded, refund_amount=refund_amount)
        )


def test_configuration_rejects_duplicate_category_keys() -> None:
    with pytest.raises(ValidationError, match="category keys must be unique"):
        ConfigPutRequest(
            categories=[_category("restaurant"), _category("restaurant")],
        )


def test_single_user_configuration_has_no_account_or_named_scope() -> None:
    request = ConfigPutRequest(categories=[_category("restaurant")])

    assert "account_ids" not in request.model_dump(mode="json")
    assert "scope_key" not in request.model_dump(mode="json")
    assert "account_ids" not in ConfigPutRequest.model_json_schema()["properties"]
    assert "scope_key" not in ConfigPutRequest.model_json_schema()["properties"]


def test_source_connector_metadata_is_not_part_of_upload_or_begin_contracts() -> None:
    begin_properties = RefreshBeginRequest.model_json_schema()["properties"]
    transaction_properties = TransactionUpload.model_json_schema()["properties"]

    assert "cursor_before" not in begin_properties
    assert "supersedes_source_transaction_id" not in transaction_properties
    assert {
        "decision",
        "status",
        "category_keys",
        "merchant_name",
        "description",
        "source_transaction_id",
        "transaction_date",
    }.isdisjoint(transaction_properties)
    assert "pending_source_id" in transaction_properties


def test_commit_only_accepts_batch_count_and_completed_accounts() -> None:
    request = RefreshCommitRequest(
        expected_batch_count=1,
        completed_accounts=["synthetic-savings", "synthetic-checking"],
    )

    assert request.completed_accounts == ["synthetic-checking", "synthetic-savings"]
    assert set(RefreshCommitRequest.model_json_schema()["properties"]) == {
        "expected_batch_count",
        "completed_accounts",
    }

    with pytest.raises(ValidationError, match="completed_accounts must be unique"):
        RefreshCommitRequest(
            expected_batch_count=1,
            completed_accounts=["synthetic-checking", "synthetic-checking"],
        )


def test_openapi_only_exposes_the_simplified_refresh_protocol() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    transaction_fields = set(schemas["TransactionUpload"]["properties"])
    assert transaction_fields == {
        "source_id",
        "account_id",
        "account_name",
        "date",
        "amount",
        "categories",
        "pending",
        "pending_source_id",
        "name",
        "merchant",
        "currency",
        "refunded",
        "refund_amount",
    }
    assert set(schemas["RefreshCommitRequest"]["properties"]) == {
        "expected_batch_count",
        "completed_accounts",
    }
    assert set(schemas["RefreshBatchResponse"]["properties"]) == {
        "run_id",
        "batch_index",
        "item_count",
        "replayed",
    }
    assert set(schemas["RefreshRunView"]["properties"]) == {
        "run_id",
        "state",
        "mode",
        "config_version_id",
        "batch_count",
        "item_count",
        "receipt",
        "created_at",
        "committed_at",
        "error_code",
    }
    assert "completeness" not in schemas["DashboardFreshness"]["properties"]
