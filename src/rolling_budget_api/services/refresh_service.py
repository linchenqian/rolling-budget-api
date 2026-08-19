from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from rolling_budget_api.db import (
    Category,
    ConfigVersion,
    ConfigVersionRule,
    ConfigVersionStatus,
    RefreshBatch,
    RefreshMode,
    RefreshRun,
    RefreshRunState,
    RuleVersion,
    StagedDecision,
    StagedTransaction,
    StagedTransactionCategory,
    SyncState,
    Transaction,
    TransactionCategory,
    TransactionStatus,
)
from rolling_budget_api.db.session import begin_write_transaction
from rolling_budget_api.schemas.refresh import (
    RefreshBatchRequest,
    RefreshBatchResponse,
    RefreshBeginRequest,
    RefreshBeginResponse,
    RefreshCommitRequest,
    RefreshRunView,
)
from rolling_budget_api.services.errors import ConflictError, DomainError, NotFoundError
from rolling_budget_api.services.hashing import checksum_chain, sha256_hex, stable_receipt


def _mode(value: str) -> RefreshMode:
    return RefreshMode.FULL if value == "FULL_REBUILD" else RefreshMode.INCREMENTAL


def _active_and_pending(db: Session) -> tuple[ConfigVersion | None, ConfigVersion | None]:
    active = db.scalar(
        select(ConfigVersion).where(ConfigVersion.status == ConfigVersionStatus.ACTIVE)
    )
    pending = db.scalar(
        select(ConfigVersion)
        .where(ConfigVersion.status == ConfigVersionStatus.PENDING)
        .order_by(ConfigVersion.version.desc())
    )
    return active, pending


def _rules_for_config(
    db: Session, config_id: UUID
) -> list[tuple[ConfigVersionRule, RuleVersion, Category]]:
    return list(
        db.execute(
            select(ConfigVersionRule, RuleVersion, Category)
            .join(RuleVersion, RuleVersion.id == ConfigVersionRule.rule_version_id)
            .join(Category, Category.id == ConfigVersionRule.category_id)
            .where(ConfigVersionRule.config_version_id == config_id)
            .order_by(Category.sort_order, Category.key)
        )
        .tuples()
        .all()
    )


def begin_refresh(
    db: Session,
    request: RefreshBeginRequest,
    *,
    idempotency_key: str,
    max_batch_items: int,
    max_request_bytes: int,
) -> RefreshBeginResponse:
    begin_write_transaction(db)
    request_hash = sha256_hex(request)
    existing = db.scalar(select(RefreshRun).where(RefreshRun.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ConflictError(
                "Idempotency-Key was already used for a different refresh request",
                code="idempotency_conflict",
            )
        config = db.get(ConfigVersion, existing.config_version_id)
        if config is None:  # protected by FK; defensive for damaged databases
            raise ConflictError("Refresh configuration no longer exists", code="config_missing")
        return _begin_response(
            db,
            existing,
            config,
            max_batch_items=max_batch_items,
            max_request_bytes=max_request_bytes,
        )

    active, pending = _active_and_pending(db)
    if active is None:
        raise ConflictError("Create a configuration before refreshing", code="config_required")

    mode = _mode(request.mode)
    if mode == RefreshMode.INCREMENTAL and pending is not None:
        raise ConflictError(
            "A pending configuration requires a FULL_REBUILD first",
            code="full_rebuild_required",
        )
    target = pending if mode == RefreshMode.FULL and pending is not None else active
    configured_accounts = set(target.source_config.get("account_ids", []))
    if configured_accounts and set(request.expected_accounts) != configured_accounts:
        raise ConflictError(
            "expected_accounts must exactly match the target configuration",
            code="account_scope_mismatch",
        )
    if mode == RefreshMode.FULL:
        rules = _rules_for_config(db, target.id)
        max_lookback = max((rule.lookback_days for _link, rule, _category in rules), default=1)
        required_start = request.source_to_date - timedelta(days=max_lookback - 1)
        if request.source_from_date > required_start:
            raise ConflictError(
                f"FULL_REBUILD must start on or before {required_start.isoformat()}",
                code="full_window_incomplete",
            )

    sync_state = db.get(SyncState, request.scope_key)
    current_cursor = sync_state.cursor if sync_state is not None else None
    if mode == RefreshMode.INCREMENTAL and request.cursor_before != current_cursor:
        raise ConflictError(
            "cursor_before does not match the last committed cursor",
            code="cursor_conflict",
        )

    run = RefreshRun(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        mode=mode,
        state=RefreshRunState.CREATED,
        config_version_id=target.id,
        scope_key=request.scope_key,
        source_from_date=request.source_from_date,
        source_to_date=request.source_to_date,
        expected_accounts=sorted(request.expected_accounts),
        cursor_before=request.cursor_before,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return _begin_response(
        db,
        run,
        target,
        max_batch_items=max_batch_items,
        max_request_bytes=max_request_bytes,
    )


def _begin_response(
    db: Session,
    run: RefreshRun,
    config: ConfigVersion,
    *,
    max_batch_items: int,
    max_request_bytes: int,
) -> RefreshBeginResponse:
    rules = [
        {
            "category_key": category.key,
            "category_name": category.name,
            "lookback_days": rule.lookback_days,
            "classification_instruction": rule.classification_instruction,
            "enabled": rule.is_enabled,
            "rule_version": rule.version,
            "rule_hash": rule.rule_hash,
        }
        for _link, rule, category in _rules_for_config(db, config.id)
    ]
    return RefreshBeginResponse(
        run_id=run.id,
        state=run.state.value.upper(),
        mode="FULL_REBUILD" if run.mode == RefreshMode.FULL else "INCREMENTAL",
        config_version_id=config.id,
        config_version=config.version,
        rules=rules,
        max_batch_items=max_batch_items,
        max_request_bytes=max_request_bytes,
    )


def upload_batch(
    db: Session,
    run_id: UUID,
    batch_index: int,
    request: RefreshBatchRequest,
    *,
    max_batch_items: int,
) -> RefreshBatchResponse:
    begin_write_transaction(db)
    if batch_index < 0:
        raise DomainError("batch_index must be nonnegative", code="invalid_batch_index")
    if len(request.transactions) > max_batch_items:
        raise DomainError(
            f"Batch contains more than {max_batch_items} transactions",
            code="batch_too_large",
            status_code=413,
        )

    request_hash = sha256_hex(request)
    checksum = sha256_hex([item.model_dump(mode="json") for item in request.transactions])
    run = db.scalar(select(RefreshRun).where(RefreshRun.id == run_id).with_for_update())
    if run is None:
        raise NotFoundError("Refresh run not found", code="refresh_run_not_found")

    existing = db.get(RefreshBatch, (run_id, batch_index))
    if existing is not None:
        if existing.request_hash != request_hash or existing.checksum != checksum:
            raise ConflictError(
                "This batch index already contains different content",
                code="batch_content_conflict",
            )
        return _batch_response(existing, replayed=True)

    reused_key = db.scalar(
        select(RefreshBatch).where(
            RefreshBatch.run_id == run_id,
            RefreshBatch.idempotency_key == request.idempotency_key,
        )
    )
    if reused_key is not None:
        raise ConflictError(
            "Batch idempotency_key was already used at another index",
            code="batch_idempotency_conflict",
        )
    if run.state not in {RefreshRunState.CREATED, RefreshRunState.UPLOADED}:
        raise ConflictError(
            f"Cannot upload to a {run.state.value} refresh run",
            code="refresh_run_not_uploadable",
        )

    allowed = {
        category.key: (category.id, rule.id)
        for _link, rule, category in _rules_for_config(db, run.config_version_id)
        if rule.is_enabled
    }
    identities: set[tuple[str, str]] = set()
    store_count = 0
    for item in request.transactions:
        identity = (item.account_id, item.source_transaction_id)
        if identity in identities:
            raise ConflictError(
                "A transaction identity occurs more than once in the batch",
                code="duplicate_transaction",
            )
        identities.add(identity)
        if item.account_id not in run.expected_accounts:
            raise ConflictError(
                "Transaction account is not in expected_accounts",
                code="unexpected_account",
            )
        unknown = sorted(set(item.category_keys) - allowed.keys())
        if unknown:
            raise DomainError(
                f"Unknown or disabled categories: {', '.join(unknown)}",
                code="unknown_category",
            )
        staged_key = (run.id, run.scope_key, item.account_id, item.source_transaction_id)
        if db.get(StagedTransaction, staged_key) is not None:
            raise ConflictError(
                "A transaction identity was already uploaded in another batch",
                code="duplicate_transaction",
            )
        if item.decision == "STORE":
            store_count += 1

    batch = RefreshBatch(
        run_id=run.id,
        batch_index=batch_index,
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        checksum=checksum,
        item_count=len(request.transactions),
        store_count=store_count,
        skip_count=len(request.transactions) - store_count,
    )
    db.add(batch)
    db.flush()

    for item in request.transactions:
        staged = StagedTransaction(
            run_id=run.id,
            scope_key=run.scope_key,
            config_version_id=run.config_version_id,
            account_id=item.account_id,
            source_transaction_id=item.source_transaction_id,
            batch_index=batch_index,
            decision=(StagedDecision.STORE if item.decision == "STORE" else StagedDecision.SKIP),
            transaction_date=item.transaction_date,
            amount=item.amount,
            currency=item.currency,
            status=(
                TransactionStatus.PENDING if item.status == "PENDING" else TransactionStatus.POSTED
            ),
            merchant=item.merchant_name,
            description=item.description,
            refunded=item.refunded,
            refund_amount=item.refund_amount,
            supersedes_source_transaction_id=item.supersedes_source_transaction_id,
            source_hash=sha256_hex(item),
        )
        db.add(staged)
        for key in item.category_keys:
            category_id, rule_version_id = allowed[key]
            db.add(
                StagedTransactionCategory(
                    run_id=run.id,
                    scope_key=run.scope_key,
                    config_version_id=run.config_version_id,
                    account_id=item.account_id,
                    source_transaction_id=item.source_transaction_id,
                    category_id=category_id,
                    rule_version_id=rule_version_id,
                )
            )

    run.received_batch_count += 1
    run.actual_source_count += batch.item_count
    run.actual_store_count += batch.store_count
    run.actual_skip_count += batch.skip_count
    run.state = RefreshRunState.UPLOADED
    run.uploaded_at = datetime.now(UTC)
    db.commit()
    return _batch_response(batch, replayed=False)


def _batch_response(batch: RefreshBatch, *, replayed: bool) -> RefreshBatchResponse:
    return RefreshBatchResponse(
        run_id=batch.run_id,
        batch_index=batch.batch_index,
        checksum=batch.checksum,
        item_count=batch.item_count,
        store_count=batch.store_count,
        skip_count=batch.skip_count,
        replayed=replayed,
    )


def commit_refresh(
    db: Session,
    run_id: UUID,
    request: RefreshCommitRequest,
) -> RefreshRunView:
    begin_write_transaction(db)
    run = db.scalar(select(RefreshRun).where(RefreshRun.id == run_id).with_for_update())
    if run is None:
        raise NotFoundError("Refresh run not found", code="refresh_run_not_found")
    if run.state == RefreshRunState.COMMITTED:
        stored_accounts = sorted(run.account_manifest or [], key=lambda item: item["account_id"])
        replay_accounts = sorted(
            [item.model_dump(mode="json") for item in request.accounts],
            key=lambda item: item["account_id"],
        )
        same_commit = (
            run.expected_batch_count == request.expected_batch_count
            and run.expected_source_count == request.expected_item_count
            and run.expected_store_count == request.expected_store_count
            and run.expected_skip_count == request.expected_skip_count
            and run.input_checksum == request.ordered_batch_checksum
            and run.cursor_after == request.cursor_after
            and stored_accounts == replay_accounts
            and request.source_complete
        )
        if not same_commit:
            raise ConflictError(
                "Committed run was retried with a different manifest",
                code="commit_replay_conflict",
            )
        return _run_view(run)
    empty_created_run = (
        run.state == RefreshRunState.CREATED
        and request.expected_batch_count == 0
        and request.expected_item_count == 0
        and request.expected_store_count == 0
        and request.expected_skip_count == 0
    )
    if run.state != RefreshRunState.UPLOADED and not empty_created_run:
        raise ConflictError(
            f"Cannot commit a {run.state.value} refresh run",
            code="refresh_run_not_committable",
        )

    config = db.scalar(
        select(ConfigVersion).where(ConfigVersion.id == run.config_version_id).with_for_update()
    )
    if config is None:
        raise ConflictError("Refresh configuration no longer exists", code="config_missing")
    if config.status == ConfigVersionStatus.SUPERSEDED:
        raise ConflictError(
            "The target configuration was superseded during this run",
            code="config_version_conflict",
        )
    if run.mode == RefreshMode.INCREMENTAL and config.status != ConfigVersionStatus.ACTIVE:
        raise ConflictError(
            "Incremental refresh target is no longer active",
            code="config_version_conflict",
        )

    batches = list(
        db.scalars(
            select(RefreshBatch)
            .where(RefreshBatch.run_id == run.id)
            .order_by(RefreshBatch.batch_index)
        )
    )
    expected_indices = list(range(request.expected_batch_count))
    if [batch.batch_index for batch in batches] != expected_indices:
        raise ConflictError("One or more upload batches are missing", code="missing_batch")
    computed_chain = checksum_chain(batch.checksum for batch in batches)
    if computed_chain != request.ordered_batch_checksum:
        raise ConflictError("Batch checksum chain does not match", code="checksum_mismatch")

    totals = {
        "items": sum(batch.item_count for batch in batches),
        "store": sum(batch.store_count for batch in batches),
        "skip": sum(batch.skip_count for batch in batches),
    }
    expected_totals = {
        "items": request.expected_item_count,
        "store": request.expected_store_count,
        "skip": request.expected_skip_count,
    }
    if totals != expected_totals:
        raise ConflictError("Manifest counts do not match uploaded batches", code="count_mismatch")
    if (
        run.actual_source_count != totals["items"]
        or run.actual_store_count != totals["store"]
        or run.actual_skip_count != totals["skip"]
        or run.received_batch_count != len(batches)
    ):
        raise ConflictError("Stored run counters do not match batches", code="count_mismatch")

    manifest_accounts = {item.account_id for item in request.accounts}
    if manifest_accounts != set(run.expected_accounts):
        raise ConflictError(
            "Account manifest does not match expected_accounts",
            code="account_manifest_mismatch",
        )
    staged_counts = Counter(
        account_id
        for account_id in db.scalars(
            select(StagedTransaction.account_id).where(StagedTransaction.run_id == run.id)
        )
    )
    for account in request.accounts:
        if not account.pages_complete:
            raise ConflictError(
                f"Account {account.account_id} is missing one or more pages",
                code="account_incomplete",
            )
        if staged_counts[account.account_id] != account.observed_count:
            raise ConflictError(
                f"Observed count does not match account {account.account_id}",
                code="account_count_mismatch",
            )
        if (
            account.source_reported_count is not None
            and account.source_reported_count != account.observed_count
        ):
            raise ConflictError(
                f"Source count does not match account {account.account_id}",
                code="source_count_mismatch",
            )
    if not request.source_complete:
        raise ConflictError("Source marked the refresh incomplete", code="source_incomplete")

    sync_state = db.scalar(
        select(SyncState).where(SyncState.scope_key == run.scope_key).with_for_update()
    )
    if run.mode == RefreshMode.INCREMENTAL:
        committed_cursor = sync_state.cursor if sync_state is not None else None
        if committed_cursor != run.cursor_before:
            raise ConflictError(
                "Another refresh advanced the cursor first",
                code="cursor_conflict",
            )
    elif sync_state is not None:
        previous_run = db.get(RefreshRun, sync_state.last_refresh_run_id)
        if previous_run is not None and previous_run.created_at > run.created_at:
            raise ConflictError(
                "A newer refresh committed first",
                code="refresh_run_superseded",
            )

    run.state = RefreshRunState.VALIDATED
    run.validated_at = datetime.now(UTC)
    run.uploaded_at = run.uploaded_at or run.validated_at
    run.expected_batch_count = request.expected_batch_count
    run.expected_source_count = request.expected_item_count
    run.expected_store_count = request.expected_store_count
    run.expected_skip_count = request.expected_skip_count
    run.source_complete = True
    run.input_checksum = request.ordered_batch_checksum
    run.computed_checksum = computed_chain
    run.cursor_after = request.cursor_after
    run.account_manifest = [item.model_dump(mode="json") for item in request.accounts]
    # The database state-machine intentionally rejects UPLOADED -> COMMITTED jumps.
    # Flush VALIDATED as its own guarded transition while keeping the outer transaction open.
    db.flush()

    if run.mode == RefreshMode.FULL:
        _replace_scope(db, run)
    else:
        _merge_incremental(db, run)
    _prune_outside_windows(db, run)

    now = datetime.now(UTC)
    # Mark the run committed while the outer transaction is still private. SQLite has no
    # deferred constraint triggers, so config activation and cursor advancement validate
    # against this state in the steps below. A rollback still removes every change.
    run.state = RefreshRunState.COMMITTED
    run.committed_at = now
    db.flush()

    if config.status == ConfigVersionStatus.PENDING:
        old_active = db.scalar(
            select(ConfigVersion)
            .where(ConfigVersion.status == ConfigVersionStatus.ACTIVE)
            .with_for_update()
        )
        if old_active is not None and old_active.id != config.id:
            old_active.status = ConfigVersionStatus.SUPERSEDED
            old_active.superseded_at = now
            # The one-active partial unique index is immediate. Flush the old row first;
            # both changes are still hidden inside the same transaction.
            db.flush()
        config.status = ConfigVersionStatus.ACTIVE
        config.activated_at = now
        db.flush()

    next_cursor: dict[str, Any] = request.cursor_after or {}
    if sync_state is None:
        sync_state = SyncState(
            scope_key=run.scope_key,
            cursor=next_cursor,
            cursor_hash=sha256_hex(next_cursor),
            config_version_id=config.id,
            last_refresh_run_id=run.id,
            revision=1,
        )
        db.add(sync_state)
    else:
        sync_state.cursor = next_cursor
        sync_state.cursor_hash = sha256_hex(next_cursor)
        sync_state.config_version_id = config.id
        sync_state.last_refresh_run_id = run.id
        sync_state.revision += 1
        sync_state.updated_at = now

    db.flush()

    # Raw staging data is unnecessary after atomic commit. Batch hashes and counts remain
    # for retry receipts and integrity auditing.
    db.execute(delete(StagedTransaction).where(StagedTransaction.run_id == run.id))
    db.commit()
    db.refresh(run)
    return _run_view(run)


def _replace_scope(db: Session, run: RefreshRun) -> None:
    db.execute(delete(TransactionCategory).where(TransactionCategory.scope_key == run.scope_key))
    db.execute(delete(Transaction).where(Transaction.scope_key == run.scope_key))
    db.flush()
    for staged in db.scalars(
        select(StagedTransaction).where(
            StagedTransaction.run_id == run.id,
            StagedTransaction.decision == StagedDecision.STORE,
        )
    ):
        _store_staged(db, run, staged, existing=None)


def _merge_incremental(db: Session, run: RefreshRun) -> None:
    staged_items = list(
        db.scalars(select(StagedTransaction).where(StagedTransaction.run_id == run.id))
    )
    for staged in staged_items:
        if staged.supersedes_source_transaction_id:
            _delete_live_identity(
                db,
                run.scope_key,
                staged.account_id,
                staged.supersedes_source_transaction_id,
            )
        key = (run.scope_key, staged.account_id, staged.source_transaction_id)
        existing = db.get(Transaction, key)
        if staged.decision == StagedDecision.SKIP:
            _delete_live_identity(
                db,
                run.scope_key,
                staged.account_id,
                staged.source_transaction_id,
            )
            continue
        _store_staged(db, run, staged, existing=existing)


def _delete_live_identity(db: Session, scope: str, account: str, source_id: str) -> None:
    db.execute(
        delete(TransactionCategory).where(
            TransactionCategory.scope_key == scope,
            TransactionCategory.account_id == account,
            TransactionCategory.source_transaction_id == source_id,
        )
    )
    db.execute(
        delete(Transaction).where(
            Transaction.scope_key == scope,
            Transaction.account_id == account,
            Transaction.source_transaction_id == source_id,
        )
    )


def _store_staged(
    db: Session,
    run: RefreshRun,
    staged: StagedTransaction,
    *,
    existing: Transaction | None,
) -> None:
    if (
        staged.transaction_date is None
        or staged.amount is None
        or staged.currency is None
        or staged.status is None
    ):
        raise ConflictError("STORE transaction is incomplete", code="invalid_staging_data")
    now = datetime.now(UTC)
    if existing is None:
        existing = Transaction(
            scope_key=run.scope_key,
            account_id=staged.account_id,
            source_transaction_id=staged.source_transaction_id,
            transaction_date=staged.transaction_date,
            amount=staged.amount,
            currency=staged.currency,
            status=staged.status,
            merchant=staged.merchant,
            description=staged.description,
            refunded=staged.refunded,
            refund_amount=staged.refund_amount,
            supersedes_source_transaction_id=staged.supersedes_source_transaction_id,
            source_hash=staged.source_hash,
            config_version_id=run.config_version_id,
            first_refresh_run_id=run.id,
            last_refresh_run_id=run.id,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(existing)
        db.flush()
    else:
        _delete_live_identity(
            db,
            run.scope_key,
            staged.account_id,
            staged.source_transaction_id,
        )
        db.flush()
        # Preserve first-seen provenance across deterministic replacement.
        replacement = Transaction(
            scope_key=run.scope_key,
            account_id=staged.account_id,
            source_transaction_id=staged.source_transaction_id,
            transaction_date=staged.transaction_date,
            amount=staged.amount,
            currency=staged.currency,
            status=staged.status,
            merchant=staged.merchant,
            description=staged.description,
            refunded=staged.refunded,
            refund_amount=staged.refund_amount,
            supersedes_source_transaction_id=staged.supersedes_source_transaction_id,
            source_hash=staged.source_hash,
            config_version_id=run.config_version_id,
            first_refresh_run_id=existing.first_refresh_run_id,
            last_refresh_run_id=run.id,
            first_seen_at=existing.first_seen_at,
            last_seen_at=now,
        )
        db.add(replacement)
        existing = replacement
        db.flush()

    categories = list(
        db.scalars(
            select(StagedTransactionCategory).where(
                StagedTransactionCategory.run_id == run.id,
                StagedTransactionCategory.scope_key == run.scope_key,
                StagedTransactionCategory.account_id == staged.account_id,
                StagedTransactionCategory.source_transaction_id == staged.source_transaction_id,
            )
        )
    )
    if not categories:
        raise ConflictError("STORE transaction has no categories", code="invalid_staging_data")
    for category in categories:
        db.add(
            TransactionCategory(
                scope_key=run.scope_key,
                account_id=staged.account_id,
                source_transaction_id=staged.source_transaction_id,
                category_id=category.category_id,
                config_version_id=run.config_version_id,
                rule_version_id=category.rule_version_id,
            )
        )


def _prune_outside_windows(db: Session, run: RefreshRun) -> None:
    if run.source_to_date is None:
        return
    # Session autoflush is intentionally disabled; make every newly created live/category
    # row visible to the pruning queries, including the final staged transaction.
    db.flush()
    for _link, rule, category in _rules_for_config(db, run.config_version_id):
        cutoff = run.source_to_date - timedelta(days=rule.lookback_days - 1)
        old_links = list(
            db.scalars(
                select(TransactionCategory)
                .join(
                    Transaction,
                    (Transaction.scope_key == TransactionCategory.scope_key)
                    & (Transaction.account_id == TransactionCategory.account_id)
                    & (
                        Transaction.source_transaction_id
                        == TransactionCategory.source_transaction_id
                    ),
                )
                .where(
                    TransactionCategory.scope_key == run.scope_key,
                    TransactionCategory.category_id == category.id,
                    Transaction.transaction_date < cutoff,
                )
            )
        )
        for link in old_links:
            db.delete(link)
    db.flush()

    live_items = list(db.scalars(select(Transaction).where(Transaction.scope_key == run.scope_key)))
    for transaction in live_items:
        category_count = db.scalar(
            select(func.count())
            .select_from(TransactionCategory)
            .where(
                TransactionCategory.scope_key == transaction.scope_key,
                TransactionCategory.account_id == transaction.account_id,
                TransactionCategory.source_transaction_id == transaction.source_transaction_id,
            )
        )
        if not category_count:
            db.delete(transaction)


def get_refresh_run(db: Session, run_id: UUID) -> RefreshRunView:
    run = db.get(RefreshRun, run_id)
    if run is None:
        raise NotFoundError("Refresh run not found", code="refresh_run_not_found")
    return _run_view(run)


def _run_view(run: RefreshRun) -> RefreshRunView:
    receipt = None
    if run.committed_at is not None and run.computed_checksum is not None:
        receipt = stable_receipt(run.id, run.computed_checksum, run.committed_at.isoformat())
    return RefreshRunView(
        run_id=run.id,
        state=run.state.value.upper(),
        mode="FULL_REBUILD" if run.mode == RefreshMode.FULL else "INCREMENTAL",
        config_version_id=run.config_version_id,
        batch_count=run.received_batch_count,
        item_count=run.actual_source_count,
        store_count=run.actual_store_count,
        skip_count=run.actual_skip_count,
        input_checksum=run.input_checksum,
        receipt=receipt,
        created_at=run.created_at,
        committed_at=run.committed_at,
        error_code=run.error_code,
    )
