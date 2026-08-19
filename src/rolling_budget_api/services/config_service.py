from datetime import UTC, datetime
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


def _validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise DomainError("Unknown IANA timezone", code="invalid_timezone") from exc


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
    # require a transaction rescan. Rules, window semantics, currency and scope do.
    return sha256_hex(
        {
            "timezone": request.timezone,
            "display_currency": request.display_currency,
            "aggregation_version": request.aggregation_version,
            "scope_key": request.scope_key,
            "account_ids": sorted(request.account_ids),
            "rules": [
                {"category_key": key, "rule_hash": rule_hashes[key]} for key in sorted(rule_hashes)
            ],
        }
    )


def _get_versions(db: Session) -> tuple[ConfigVersion | None, ConfigVersion | None]:
    active = db.scalar(
        select(ConfigVersion).where(ConfigVersion.status == ConfigVersionStatus.ACTIVE)
    )
    pending = db.scalar(
        select(ConfigVersion)
        .where(ConfigVersion.status == ConfigVersionStatus.PENDING)
        .order_by(ConfigVersion.version.desc())
    )
    return active, pending


def put_config(
    db: Session,
    request: ConfigPutRequest,
    *,
    if_match: str | None = None,
) -> ConfigView:
    begin_write_transaction(db)
    _validate_timezone(request.timezone)
    for requested_category in request.categories:
        if requested_category.budget_currency != request.display_currency:
            raise DomainError(
                "Each category budget_currency must match display_currency in v1",
                code="mixed_budget_currency_not_supported",
            )

    active, current_pending = _get_versions(db)
    if if_match is not None and active is not None:
        normalized = if_match.strip('"')
        if normalized not in {str(active.version), active.config_hash}:
            raise ConflictError(
                "Active configuration changed; fetch it and retry",
                code="config_version_conflict",
            )

    loaded_categories: list[Category] = list(
        db.scalars(
            select(Category).where(Category.key.in_([item.key for item in request.categories]))
        )
    )
    existing_categories: dict[str, Category] = {loaded.key: loaded for loaded in loaded_categories}
    rule_hashes = {item.key: _rule_hash(item) for item in request.categories}
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
        else:
            # These presentation/budget fields are deliberately mutable and do not
            # invalidate historical classification.
            category.name = item.name
            category.icon = item.icon
            category.sort_order = item.sort_order
            category.budget_limit = item.budget_limit
            category.budget_currency = item.budget_currency

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

    desired_hash = _config_hash(request, rule_hashes)
    if active is not None and active.config_hash == desired_hash:
        if current_pending is not None:
            current_pending.status = ConfigVersionStatus.SUPERSEDED
            current_pending.superseded_at = datetime.now(UTC)
        db.commit()
        return get_config(db)

    if current_pending is not None and current_pending.config_hash == desired_hash:
        db.commit()
        return get_config(db)

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
    db.commit()
    return get_config(db)


def _version_view(db: Session, config: ConfigVersion | None) -> ConfigVersionView | None:
    if config is None:
        return None
    rows = db.execute(
        select(ConfigVersionRule, RuleVersion, Category)
        .join(
            RuleVersion,
            RuleVersion.id == ConfigVersionRule.rule_version_id,
        )
        .join(Category, Category.id == ConfigVersionRule.category_id)
        .where(ConfigVersionRule.config_version_id == config.id)
        .order_by(Category.sort_order, Category.key)
    ).all()
    categories = [
        CategoryConfigView(
            id=str(category.id),
            key=category.key,
            name=category.name,
            icon=category.icon,
            sort_order=category.sort_order,
            budget_limit=category.budget_limit,
            budget_currency=category.budget_currency,
            lookback_days=rule.lookback_days,
            classification_instruction=rule.classification_instruction,
            enabled=rule.is_enabled,
            rule_version=rule.version,
            rule_hash=rule.rule_hash,
        )
        for _link, rule, category in rows
    ]
    return ConfigVersionView(
        id=str(config.id),
        version=config.version,
        status=config.status.value.upper(),
        timezone=config.timezone,
        display_currency=config.display_currency,
        aggregation_version=config.aggregation_version,
        scope_key=str(config.source_config.get("scope_key", "personal")),
        account_ids=list(config.source_config.get("account_ids", [])),
        config_hash=config.config_hash,
        requires_full_rebuild=config.status == ConfigVersionStatus.PENDING,
        created_at=config.created_at,
        activated_at=config.activated_at,
        categories=categories,
    )


def get_config(db: Session) -> ConfigView:
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
