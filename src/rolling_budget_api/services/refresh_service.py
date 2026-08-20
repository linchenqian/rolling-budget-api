from datetime import UTC, datetime, timedelta
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
    StagedTransaction,
    StagedTransactionCategory,
    SyncState,
    Transaction,
    TransactionCategory,
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
from rolling_budget_api.services.config_service import (
    apply_config_presentation,
    lock_config_state,
)
from rolling_budget_api.services.errors import ConflictError, DomainError, NotFoundError
from rolling_budget_api.services.hashing import (
    canonical_json,
    checksum_chain,
    sha256_hex,
    stable_receipt,
)


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
    lock_config_state(db)
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
    if mode == RefreshMode.FULL:
        rules = _rules_for_config(db, target.id)
        max_lookback = max(
            (
                rule.lookback_days
                for _link, rule, _category in rules
                if rule.is_enabled
            ),
            default=1,
        )
        required_start = request.source_to_date - timedelta(days=max_lookback - 1)
        if request.source_from_date > required_start:
            raise ConflictError(
                f"FULL_REBUILD must start on or before {required_start.isoformat()}",
                code="full_window_incomplete",
            )

    sync_state = db.get(SyncState, 1)
    if mode == RefreshMode.INCREMENTAL and sync_state is None:
        raise ConflictError(
            "The first successful refresh must be a complete FULL_REBUILD",
            code="full_rebuild_required",
        )
    if mode == RefreshMode.INCREMENTAL and sync_state is not None:
        previous_run = db.get(RefreshRun, sync_state.last_refresh_run_id)
        if previous_run is not None and set(previous_run.expected_accounts) != set(
            request.expected_accounts
        ):
            raise ConflictError(
                "The connected account set changed; retry with a complete FULL_REBUILD",
                code="account_scope_changed",
            )
    run = RefreshRun(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        mode=mode,
        state=RefreshRunState.CREATED,
        config_version_id=target.id,
        source_from_date=request.source_from_date,
        source_to_date=request.source_to_date,
        expected_accounts=sorted(request.expected_accounts),
        sync_revision_before=(sync_state.revision if sync_state is not None else None),
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
    max_request_bytes: int,
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
    if len(canonical_json(request)) > max_request_bytes:
        raise DomainError(
            f"Batch exceeds the {max_request_bytes}-byte request limit",
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
    config = db.get(ConfigVersion, run.config_version_id)
    if config is None:  # protected by FK; defensive for damaged databases
        raise ConflictError("Refresh configuration no longer exists", code="config_missing")
    if run.source_from_date is None or run.source_to_date is None:
        raise ConflictError("Refresh run has no source window", code="invalid_refresh_window")
    identities: set[tuple[str, str]] = set()
    for item in request.transactions:
        identity = (item.account_id, item.source_id)
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
        if item.currency != config.display_currency:
            raise DomainError(
                "Transaction currency must match the configured display currency",
                code="currency_mismatch",
            )
        outside_window = item.date > run.source_to_date or (
            run.mode == RefreshMode.FULL and item.date < run.source_from_date
        )
        if outside_window:
            raise DomainError(
                "Transaction date is outside the refresh source window",
                code="transaction_outside_run_window",
            )
        unknown = sorted(set(item.categories) - allowed.keys())
        if unknown:
            raise DomainError(
                f"Unknown or disabled categories: {', '.join(unknown)}",
                code="unknown_category",
            )
        staged_key = (run.id, item.account_id, item.source_id)
        if db.get(StagedTransaction, staged_key) is not None:
            raise ConflictError(
                "A transaction identity was already uploaded in another batch",
                code="duplicate_transaction",
            )
    batch = RefreshBatch(
        run_id=run.id,
        batch_index=batch_index,
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        checksum=checksum,
        item_count=len(request.transactions),
    )
    db.add(batch)
    db.flush()

    for item in request.transactions:
        staged = StagedTransaction(
            run_id=run.id,
            config_version_id=run.config_version_id,
            account_id=item.account_id,
            account_name=item.account_name,
            source_id=item.source_id,
            batch_index=batch_index,
            transaction_date=item.date,
            amount=item.amount,
            currency=item.currency,
            pending=item.pending,
            pending_source_id=item.pending_source_id,
            name=item.name,
            merchant=item.merchant,
            refunded=item.refunded,
            refund_amount=item.refund_amount,
            source_hash=sha256_hex(item),
        )
        db.add(staged)
        for key in item.categories:
            category_id, rule_version_id = allowed[key]
            db.add(
                StagedTransactionCategory(
                    run_id=run.id,
                    config_version_id=run.config_version_id,
                    account_id=item.account_id,
                    source_id=item.source_id,
                    category_id=category_id,
                    rule_version_id=rule_version_id,
                )
            )

    run.received_batch_count += 1
    run.actual_item_count += batch.item_count
    run.state = RefreshRunState.UPLOADED
    run.uploaded_at = datetime.now(UTC)
    db.commit()
    return _batch_response(batch, replayed=False)


def _batch_response(batch: RefreshBatch, *, replayed: bool) -> RefreshBatchResponse:
    return RefreshBatchResponse(
        run_id=batch.run_id,
        batch_index=batch.batch_index,
        item_count=batch.item_count,
        replayed=replayed,
    )


def commit_refresh(
    db: Session,
    run_id: UUID,
    request: RefreshCommitRequest,
) -> RefreshRunView:
    begin_write_transaction(db)
    lock_config_state(db)
    run = db.scalar(select(RefreshRun).where(RefreshRun.id == run_id).with_for_update())
    if run is None:
        raise NotFoundError("Refresh run not found", code="refresh_run_not_found")
    if run.state == RefreshRunState.COMMITTED:
        same_commit = (
            run.expected_batch_count == request.expected_batch_count
            and sorted(run.completed_accounts or []) == request.completed_accounts
        )
        if not same_commit:
            raise ConflictError(
                "Committed run was retried with different completion data",
                code="commit_replay_conflict",
            )
        return _run_view(run)
    empty_created_run = run.state == RefreshRunState.CREATED and request.expected_batch_count == 0
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
    active, pending = _active_and_pending(db)
    if run.mode == RefreshMode.INCREMENTAL and pending is not None:
        raise ConflictError(
            "A pending configuration was created after this refresh began",
            code="full_rebuild_required",
        )
    current_target = pending if run.mode == RefreshMode.FULL and pending is not None else active
    if current_target is None or current_target.id != config.id:
        raise ConflictError(
            "The target configuration changed after this refresh began",
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

    item_count = sum(batch.item_count for batch in batches)
    if request.completed_accounts != sorted(run.expected_accounts):
        raise ConflictError(
            "completed_accounts does not match expected_accounts",
            code="completed_accounts_mismatch",
        )

    sync_state = db.scalar(select(SyncState).where(SyncState.id == 1).with_for_update())
    current_sync_revision = sync_state.revision if sync_state is not None else None
    if current_sync_revision != run.sync_revision_before:
        raise ConflictError(
            "Another refresh committed after this run began",
            code="refresh_run_superseded",
        )
    if run.mode == RefreshMode.INCREMENTAL:
        previous_run = (
            db.get(RefreshRun, sync_state.last_refresh_run_id) if sync_state is not None else None
        )
        if previous_run is None or set(previous_run.expected_accounts) != set(
            run.expected_accounts
        ):
            raise ConflictError(
                "The connected account set changed after this run began; retry with a complete "
                "FULL_REBUILD",
                code="account_scope_changed",
            )

    run.state = RefreshRunState.VALIDATED
    run.validated_at = datetime.now(UTC)
    run.uploaded_at = run.uploaded_at or run.validated_at
    run.expected_batch_count = request.expected_batch_count
    run.received_batch_count = len(batches)
    run.actual_item_count = item_count
    run.computed_checksum = computed_chain
    run.completed_accounts = request.completed_accounts
    # The database state-machine intentionally rejects UPLOADED -> COMMITTED jumps.
    # Flush VALIDATED as its own guarded transition while keeping the outer transaction open.
    db.flush()

    replacement_targets = _validate_pending_replacements(db, run)
    if run.mode == RefreshMode.FULL:
        _replace_all_transactions(db, run, replacement_targets=replacement_targets)
    else:
        _merge_incremental(db, run, replacement_targets=replacement_targets)
    _prune_outside_windows(db, run)

    now = datetime.now(UTC)
    # Mark the run committed while the outer transaction is still private. SQLite has no
    # deferred constraint triggers, so config activation and sync-state advancement validate
    # against this state in the steps below. A rollback still removes every change.
    run.state = RefreshRunState.COMMITTED
    run.committed_at = now
    db.flush()

    if config.status == ConfigVersionStatus.PENDING:
        # Pending budget/presentation fields remain isolated from the active dashboard until
        # the same transaction that publishes the rebuilt classifications activates them.
        apply_config_presentation(db, config)
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

    if sync_state is None:
        sync_state = SyncState(
            id=1,
            config_version_id=config.id,
            last_refresh_run_id=run.id,
            revision=1,
        )
        db.add(sync_state)
    else:
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


def _validate_pending_replacements(
    db: Session,
    run: RefreshRun,
) -> set[tuple[str, str]]:
    staged_items = list(
        db.scalars(select(StagedTransaction).where(StagedTransaction.run_id == run.id))
    )
    staged_by_identity = {
        (staged.account_id, staged.source_id): staged for staged in staged_items
    }
    replacement_targets: set[tuple[str, str]] = set()
    for staged in staged_items:
        pending_source_id = staged.pending_source_id
        if pending_source_id is None:
            continue
        target = (staged.account_id, pending_source_id)
        if staged.pending or target == (staged.account_id, staged.source_id):
            raise ConflictError(
                "A pending replacement link is invalid",
                code="pending_replacement_conflict",
            )
        if target in replacement_targets:
            raise ConflictError(
                "More than one transaction replaces the same pending transaction",
                code="pending_replacement_conflict",
            )
        replacement_targets.add(target)

        staged_target = staged_by_identity.get(target)
        if staged_target is not None and not staged_target.pending:
            raise ConflictError(
                "A pending replacement link targets a non-pending staged transaction",
                code="pending_replacement_conflict",
            )
        live_target = db.get(Transaction, target)
        if live_target is not None and not live_target.pending:
            raise ConflictError(
                "A pending replacement link targets a non-pending live transaction",
                code="pending_replacement_conflict",
            )
    return replacement_targets


def _replace_all_transactions(
    db: Session,
    run: RefreshRun,
    *,
    replacement_targets: set[tuple[str, str]],
) -> None:
    db.execute(delete(TransactionCategory))
    db.execute(delete(Transaction))
    db.flush()
    for staged in db.scalars(select(StagedTransaction).where(StagedTransaction.run_id == run.id)):
        if (staged.account_id, staged.source_id) in replacement_targets:
            continue
        _store_staged(db, run, staged, existing=None)


def _merge_incremental(
    db: Session,
    run: RefreshRun,
    *,
    replacement_targets: set[tuple[str, str]],
) -> None:
    staged_items = list(
        db.scalars(select(StagedTransaction).where(StagedTransaction.run_id == run.id))
    )
    for staged in staged_items:
        if (staged.account_id, staged.source_id) in replacement_targets:
            continue
        if staged.pending_source_id is not None:
            _delete_live_identity(db, staged.account_id, staged.pending_source_id)
        key = (staged.account_id, staged.source_id)
        existing = db.get(Transaction, key)
        _store_staged(db, run, staged, existing=existing)


def _delete_live_identity(db: Session, account: str, source_id: str) -> None:
    db.execute(
        delete(TransactionCategory).where(
            TransactionCategory.account_id == account,
            TransactionCategory.source_id == source_id,
        )
    )
    db.execute(
        delete(Transaction).where(
            Transaction.account_id == account,
            Transaction.source_id == source_id,
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
        or staged.pending is None
    ):
        raise ConflictError("Staged transaction is incomplete", code="invalid_staging_data")
    now = datetime.now(UTC)
    if existing is None:
        existing = Transaction(
            account_id=staged.account_id,
            account_name=staged.account_name,
            source_id=staged.source_id,
            transaction_date=staged.transaction_date,
            amount=staged.amount,
            currency=staged.currency,
            pending=staged.pending,
            pending_source_id=staged.pending_source_id,
            name=staged.name,
            merchant=staged.merchant,
            refunded=staged.refunded,
            refund_amount=staged.refund_amount,
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
            staged.account_id,
            staged.source_id,
        )
        db.flush()
        # Preserve first-seen provenance across deterministic replacement.
        replacement = Transaction(
            account_id=staged.account_id,
            account_name=staged.account_name,
            source_id=staged.source_id,
            transaction_date=staged.transaction_date,
            amount=staged.amount,
            currency=staged.currency,
            pending=staged.pending,
            pending_source_id=staged.pending_source_id,
            name=staged.name,
            merchant=staged.merchant,
            refunded=staged.refunded,
            refund_amount=staged.refund_amount,
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
                StagedTransactionCategory.account_id == staged.account_id,
                StagedTransactionCategory.source_id == staged.source_id,
            )
        )
    )
    if not categories:
        raise ConflictError("Staged transaction has no categories", code="invalid_staging_data")
    for category in categories:
        db.add(
            TransactionCategory(
                account_id=staged.account_id,
                source_id=staged.source_id,
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
                    (Transaction.account_id == TransactionCategory.account_id)
                    & (Transaction.source_id == TransactionCategory.source_id),
                )
                .where(
                    TransactionCategory.category_id == category.id,
                    Transaction.transaction_date < cutoff,
                )
            )
        )
        for link in old_links:
            db.delete(link)
    db.flush()

    live_items = list(db.scalars(select(Transaction)))
    for transaction in live_items:
        category_count = db.scalar(
            select(func.count())
            .select_from(TransactionCategory)
            .where(
                TransactionCategory.account_id == transaction.account_id,
                TransactionCategory.source_id == transaction.source_id,
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
        item_count=run.actual_item_count,
        receipt=receipt,
        created_at=run.created_at,
        committed_at=run.committed_at,
        error_code=run.error_code,
    )
