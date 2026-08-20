from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rolling_budget_api.db import (
    Category,
    ConfigVersion,
    ConfigVersionRule,
    ConfigVersionStatus,
    RuleVersion,
    SyncState,
)
from rolling_budget_api.db.session import begin_write_transaction
from rolling_budget_api.schemas.config import (
    CategoryConfigInput,
    CategoryConfigView,
    ConfigPutRequest,
    ConfigVersionView,
    ConfigView,
)
from rolling_budget_api.services.errors import ConflictError, DomainError
from rolling_budget_api.services.hashing import sha256_hex

_POSTGRES_CONFIG_LOCK_ID = 1_380_073_044


def _validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise DomainError("Unknown IANA timezone", code="invalid_timezone") from exc


def lock_config_state(db: Session, *, shared: bool = False) -> None:
    """Serialize config creation, replacement, refresh targeting, and activation.

    SQLite writers are already serialized by BEGIN IMMEDIATE; readers explicitly begin a
    snapshot so their multiple SELECTs cannot straddle a commit. PostgreSQL needs an advisory
    lock that exists even before the first ConfigVersion row. Shared read locks keep one response
    on one committed snapshot, while every writer uses the same exclusive acquisition order.
    """

    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        if shared:
            connection = db.connection()
            driver_connection = connection.connection.driver_connection
            if not getattr(driver_connection, "in_transaction", False):
                connection.exec_driver_sql("BEGIN")
        return
    if dialect_name == "postgresql":
        lock_function = (
            func.pg_advisory_xact_lock_shared
            if shared
            else func.pg_advisory_xact_lock
        )
        db.execute(select(lock_function(_POSTGRES_CONFIG_LOCK_ID)))


def _rule_hash(category: CategoryConfigInput) -> str:
    return sha256_hex(
        {
            "category_key": category.key,
            "lookback_days": category.lookback_days,
            "classification_instruction": category.classification_instruction,
            "enabled": category.enabled,
        }
    )


def _config_hash(request: ConfigPutRequest, rule_hashes: dict[str, str]) -> str:
    # Budget/display-only fields intentionally do not participate: changing them does not
    # require a transaction rescan. Rules, window semantics and currency do. The financial
    # connector supplies the current account set for each refresh run.
    return sha256_hex(
        {
            "timezone": request.timezone,
            "display_currency": request.display_currency,
            "aggregation_version": request.aggregation_version,
            "rules": [
                {"category_key": key, "rule_hash": rule_hashes[key]} for key in sorted(rule_hashes)
            ],
        }
    )


def semantic_hash_for_config(db: Session, config: ConfigVersion) -> str:
    """Hash current rule semantics without legacy account configuration."""

    linked_rules = rules_for_config(db, config.id)
    return sha256_hex(
        {
            "timezone": config.timezone,
            "display_currency": config.display_currency,
            "aggregation_version": config.aggregation_version,
            "rules": [
                {"category_key": category.key, "rule_hash": rule.rule_hash}
                for _link, rule, category in sorted(
                    linked_rules,
                    key=lambda row: row[2].key,
                )
            ],
        }
    )


def _normalized_edit_payload(request: ConfigPutRequest) -> dict[str, object]:
    """Return every user-editable field in a stable order for optimistic locking."""

    payload = request.model_dump(mode="json")
    categories = payload["categories"]
    assert isinstance(categories, list)
    for category in categories:
        # Money is stored at four decimal places. Normalize request values to that scale so
        # a PUT response and a later DB-backed GET produce the same optimistic-lock hash.
        category["budget_limit"] = format(
            Decimal(str(category["budget_limit"])).quantize(Decimal("0.0001")),
            "f",
        )
    payload["categories"] = sorted(categories, key=lambda item: item["key"])
    return payload


def _active_snapshot(db: Session, config: ConfigVersion) -> ConfigPutRequest:
    return ConfigPutRequest(
        timezone=config.timezone,
        display_currency=config.display_currency,
        aggregation_version=config.aggregation_version,
        categories=[
            CategoryConfigInput(
                key=category.key,
                name=category.name,
                icon=category.icon,
                sort_order=category.sort_order,
                budget_limit=category.budget_limit,
                budget_currency=category.budget_currency,
                lookback_days=rule.lookback_days,
                classification_instruction=rule.classification_instruction,
                enabled=rule.is_enabled,
            )
            for _link, rule, category in rules_for_config(db, config.id)
        ],
    )


def editable_snapshot_for_config(db: Session, config: ConfigVersion) -> ConfigPutRequest:
    """Return the complete user-editable snapshot represented by a config version.

    Active category budgets and presentation fields are deliberately mutable so they can take
    effect without reclassifying transactions. Pending values must stay isolated until their
    full rebuild commits, so their immutable source_config is the authority.
    """

    if config.status == ConfigVersionStatus.PENDING:
        return ConfigPutRequest.model_validate(config.source_config)
    return _active_snapshot(db, config)


def edit_hash_for_config(db: Session, config: ConfigVersion) -> str:
    """Hash the complete editable snapshot used by ETag and MCP compare-and-set writes."""

    return sha256_hex(_normalized_edit_payload(editable_snapshot_for_config(db, config)))


def _edit_hash_for_request(request: ConfigPutRequest) -> str:
    return sha256_hex(_normalized_edit_payload(request))


def apply_config_presentation(db: Session, config: ConfigVersion) -> None:
    """Publish a config snapshot's display and budget fields to the live category rows."""

    snapshot = editable_snapshot_for_config(db, config)
    categories = {
        category.key: category
        for category in db.scalars(
            select(Category).where(Category.key.in_([item.key for item in snapshot.categories]))
        )
    }
    for item in snapshot.categories:
        category = categories.get(item.key)
        if category is None:
            raise DomainError(
                "Configuration references a missing category",
                code="config_category_missing",
            )
        category.name = item.name
        category.icon = item.icon
        category.sort_order = item.sort_order
        category.budget_limit = item.budget_limit
        category.budget_currency = item.budget_currency


def _get_versions(
    db: Session,
    *,
    for_update: bool = False,
) -> tuple[ConfigVersion | None, ConfigVersion | None]:
    active_query = select(ConfigVersion).where(
        ConfigVersion.status == ConfigVersionStatus.ACTIVE
    )
    pending_query = (
        select(ConfigVersion)
        .where(ConfigVersion.status == ConfigVersionStatus.PENDING)
        .order_by(ConfigVersion.version.desc())
    )
    if for_update:
        # The active row serializes configuration writers on PostgreSQL. SQLite
        # is already protected by BEGIN IMMEDIATE in begin_write_transaction.
        active_query = active_query.with_for_update()
        pending_query = pending_query.with_for_update()
    active = db.scalar(active_query)
    pending = db.scalar(pending_query)
    return active, pending


def put_config(
    db: Session,
    request: ConfigPutRequest,
    *,
    if_match: str | None = None,
) -> ConfigView:
    return _put_config(
        db,
        request,
        if_match=if_match,
        check_expected_base=False,
        expected_base_hash=None,
    )


def put_config_checked(
    db: Session,
    request: ConfigPutRequest,
    *,
    expected_config_hash: str | None,
) -> ConfigView:
    """Replace config only if the currently edited active/pending snapshot matches."""

    return _put_config(
        db,
        request,
        if_match=None,
        check_expected_base=True,
        expected_base_hash=expected_config_hash,
    )


def _commit_config_view(db: Session) -> ConfigView:
    """Commit one config write while returning the exact snapshot produced by that write."""

    db.flush()
    view = get_config(db)
    db.commit()
    return view


def _put_config(
    db: Session,
    request: ConfigPutRequest,
    *,
    if_match: str | None,
    check_expected_base: bool,
    expected_base_hash: str | None,
) -> ConfigView:
    begin_write_transaction(db)
    lock_config_state(db)
    _validate_timezone(request.timezone)
    for requested_category in request.categories:
        if requested_category.budget_currency != request.display_currency:
            raise DomainError(
                "Each category budget_currency must match display_currency in v1",
                code="mixed_budget_currency_not_supported",
            )

    active, current_pending = _get_versions(db, for_update=True)
    if check_expected_base:
        base = current_pending or active
        if base is None:
            if expected_base_hash is not None:
                raise ConflictError(
                    "No configuration exists; fetch it and retry with a null base hash",
                    code="config_version_conflict",
                )
        elif expected_base_hash is None:
            raise ConflictError(
                "A current configuration hash is required; fetch it and retry",
                code="config_version_required",
            )
        elif expected_base_hash.strip('"') != edit_hash_for_config(db, base):
            raise ConflictError(
                "The configuration being edited changed; fetch it and retry",
                code="config_version_conflict",
            )

    base = current_pending or active
    if not check_expected_base and base is not None:
        if if_match is None:
            raise ConflictError(
                "A current configuration hash is required; fetch it and retry",
                code="config_version_required",
            )
        if if_match.strip('"') != edit_hash_for_config(db, base):
            raise ConflictError(
                "The configuration being edited changed; fetch it and retry",
                code="config_version_conflict",
            )
    elif not check_expected_base and if_match is not None:
        raise ConflictError(
            "No configuration exists; retry without If-Match",
            code="config_version_conflict",
        )

    rule_hashes = {item.key: _rule_hash(item) for item in request.categories}
    desired_hash = _config_hash(request, rule_hashes)
    desired_edit_hash = _edit_hash_for_request(request)

    if active is not None and semantic_hash_for_config(db, active) == desired_hash:
        if current_pending is not None:
            current_pending.status = ConfigVersionStatus.SUPERSEDED
            current_pending.superseded_at = datetime.now(UTC)
        # Budget and presentation edits do not require a rescan. They become live only when
        # the requested rule semantics still match the active version.
        active_by_key = {
            category.key: category
            for _link, _rule, category in rules_for_config(db, active.id)
        }
        for item in request.categories:
            live_category = active_by_key[item.key]
            live_category.name = item.name
            live_category.icon = item.icon
            live_category.sort_order = item.sort_order
            live_category.budget_limit = item.budget_limit
            live_category.budget_currency = item.budget_currency
        return _commit_config_view(db)

    if current_pending is not None:
        pending_semantic_hash = semantic_hash_for_config(db, current_pending)
        if (
            pending_semantic_hash == desired_hash
            and edit_hash_for_config(db, current_pending) == desired_edit_hash
        ):
            return _commit_config_view(db)

    loaded_categories: list[Category] = list(
        db.scalars(
            select(Category).where(Category.key.in_([item.key for item in request.categories]))
        )
    )
    existing_categories: dict[str, Category] = {loaded.key: loaded for loaded in loaded_categories}
    selected_rules: dict[str, RuleVersion] = {}

    for item in request.categories:
        category = existing_categories.get(item.key)
        if category is None:
            category = Category(
                key=item.key,
                name=item.name,
                icon=item.icon,
                sort_order=item.sort_order,
                budget_limit=item.budget_limit,
                budget_currency=item.budget_currency,
            )
            db.add(category)
            db.flush()
            existing_categories[item.key] = category
        rule = db.scalar(
            select(RuleVersion).where(
                RuleVersion.category_id == category.id,
                RuleVersion.rule_hash == rule_hashes[item.key],
            )
        )
        if rule is None:
            next_version = (
                db.scalar(
                    select(func.coalesce(func.max(RuleVersion.version), 0)).where(
                        RuleVersion.category_id == category.id
                    )
                )
                or 0
            ) + 1
            rule = RuleVersion(
                category_id=category.id,
                version=next_version,
                lookback_days=item.lookback_days,
                classification_instruction=item.classification_instruction,
                is_enabled=item.enabled,
                rule_hash=rule_hashes[item.key],
            )
            db.add(rule)
            db.flush()
        selected_rules[item.key] = rule

    now = datetime.now(UTC)
    if current_pending is not None:
        current_pending.status = ConfigVersionStatus.SUPERSEDED
        current_pending.superseded_at = now

    next_config_version = (db.scalar(select(func.max(ConfigVersion.version))) or 0) + 1
    is_first_config = active is None
    config = ConfigVersion(
        version=next_config_version,
        status=(ConfigVersionStatus.ACTIVE if is_first_config else ConfigVersionStatus.PENDING),
        timezone=request.timezone,
        display_currency=request.display_currency,
        aggregation_version=request.aggregation_version,
        config_hash=desired_hash,
        source_config=request.model_dump(mode="json"),
        activated_at=now if is_first_config else None,
    )
    db.add(config)
    db.flush()
    for key, rule in selected_rules.items():
        db.add(
            ConfigVersionRule(
                config_version_id=config.id,
                category_id=existing_categories[key].id,
                rule_version_id=rule.id,
            )
        )
    if is_first_config:
        # The first configuration is immediately active, but still needs an initial full
        # transaction refresh. Its presentation fields can therefore be published now.
        db.flush()
        apply_config_presentation(db, config)
    return _commit_config_view(db)


def _version_view(db: Session, config: ConfigVersion | None) -> ConfigVersionView | None:
    if config is None:
        return None
    snapshot = editable_snapshot_for_config(db, config)
    rows = rules_for_config(db, config.id)
    rows_by_key = {
        category.key: (rule, category) for _link, rule, category in rows
    }
    categories = [
        CategoryConfigView(
            id=str(category.id),
            key=item.key,
            name=item.name,
            icon=item.icon,
            sort_order=item.sort_order,
            budget_limit=item.budget_limit,
            budget_currency=item.budget_currency,
            lookback_days=item.lookback_days,
            classification_instruction=item.classification_instruction,
            enabled=item.enabled,
            rule_version=rule.version,
            rule_hash=rule.rule_hash,
        )
        for item in sorted(snapshot.categories, key=lambda value: (value.sort_order, value.key))
        for rule, category in [rows_by_key[item.key]]
    ]
    sync_state = db.get(SyncState, 1)
    requires_full_rebuild = config.status == ConfigVersionStatus.PENDING or (
        config.status == ConfigVersionStatus.ACTIVE
        and (sync_state is None or sync_state.config_version_id != config.id)
    )
    return ConfigVersionView(
        id=str(config.id),
        version=config.version,
        status=config.status.value.upper(),
        timezone=snapshot.timezone,
        display_currency=snapshot.display_currency,
        aggregation_version=snapshot.aggregation_version,
        config_hash=edit_hash_for_config(db, config),
        requires_full_rebuild=requires_full_rebuild,
        created_at=config.created_at,
        activated_at=config.activated_at,
        categories=categories,
    )


def get_config(db: Session) -> ConfigView:
    lock_config_state(db, shared=True)
    active, pending = _get_versions(db)
    return ConfigView(active=_version_view(db, active), pending=_version_view(db, pending))


def get_config_by_id(db: Session, config_id: UUID) -> ConfigVersion | None:
    return db.get(ConfigVersion, config_id)


def rules_for_config(
    db: Session,
    config_id: UUID,
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
