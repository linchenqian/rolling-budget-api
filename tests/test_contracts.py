from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rolling_budget_api.schemas.config import CategoryConfigInput, ConfigPutRequest
from rolling_budget_api.schemas.refresh import RefreshCommitRequest, TransactionUpload


def _transaction(**overrides: object) -> dict[str, object]:
    transaction: dict[str, object] = {
        "account_id": "synthetic-checking",
        "source_transaction_id": "synthetic-tx-001",
        "decision": "STORE",
        "transaction_date": date(2026, 8, 19),
        "amount": Decimal("42.50"),
        "currency": "USD",
        "status": "POSTED",
        "merchant_name": "Synthetic Bistro",
        "description": "Synthetic fixture only",
        "category_keys": ["restaurant"],
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


def test_store_requires_categories_and_skip_forbids_them() -> None:
    with pytest.raises(ValidationError, match="STORE requires"):
        TransactionUpload.model_validate(_transaction(category_keys=[]))

    with pytest.raises(ValidationError, match="SKIP cannot"):
        TransactionUpload.model_validate(_transaction(decision="SKIP"))

    skipped = TransactionUpload.model_validate(_transaction(decision="SKIP", category_keys=[]))
    assert skipped.decision == "SKIP"


def test_multilabel_is_supported_but_duplicate_labels_are_rejected() -> None:
    multilabel = TransactionUpload.model_validate(
        _transaction(category_keys=["restaurant", "dating"])
    )
    assert multilabel.category_keys == ["restaurant", "dating"]

    with pytest.raises(ValidationError, match="cannot contain duplicates"):
        TransactionUpload.model_validate(_transaction(category_keys=["restaurant", "restaurant"]))


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
            account_ids=["synthetic-checking"],
            categories=[_category("restaurant"), _category("restaurant")],
        )


def test_commit_counts_must_balance() -> None:
    with pytest.raises(ValidationError, match=r"store_count \+ skip_count"):
        RefreshCommitRequest(
            expected_batch_count=1,
            expected_item_count=3,
            expected_store_count=1,
            expected_skip_count=1,
            ordered_batch_checksum="a" * 64,
            accounts=[
                {
                    "account_id": "synthetic-checking",
                    "pages_complete": True,
                    "observed_count": 3,
                    "source_reported_count": 3,
                }
            ],
        )
