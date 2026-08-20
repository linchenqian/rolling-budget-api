"""Add owner-only OAuth authorization and opaque-token storage.

Revision ID: 0002_oauth
Revises: 0001_initial
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_oauth"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(
    name: str,
    *,
    nullable: bool = False,
    current_default: bool = False,
) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP") if current_default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "oauth_authorization_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("resource", sa.String(length=1024), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=43), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("credential_generation", sa.String(length=64), nullable=False),
        _timestamp("created_at", current_default=True),
        _timestamp("expires_at"),
        _timestamp("consumed_at", nullable=True),
        sa.CheckConstraint(
            "length(code_digest) = 64",
            name="ck_oauth_authorization_codes_code_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(code_challenge) = 43",
            name="ck_oauth_authorization_codes_code_challenge_s256",
        ),
        sa.CheckConstraint(
            "length(credential_generation) = 64",
            name="ck_oauth_authorization_codes_credential_generation_sha256",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_authorization_codes"),
        sa.UniqueConstraint(
            "code_digest",
            name="uq_oauth_authorization_codes_code_digest",
        ),
    )
    op.create_index(
        "ix_oauth_authorization_codes_expires_at",
        "oauth_authorization_codes",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "oauth_token_families",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("resource", sa.String(length=1024), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("credential_generation", sa.String(length=64), nullable=False),
        _timestamp("created_at", current_default=True),
        _timestamp("expires_at"),
        _timestamp("revoked_at", nullable=True),
        _timestamp("compromise_detected_at", nullable=True),
        sa.CheckConstraint(
            "length(credential_generation) = 64",
            name="ck_oauth_token_families_credential_generation_sha256",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_token_families"),
    )
    op.create_index(
        "ix_oauth_token_families_expires_at",
        "oauth_token_families",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_token_families_revoked_at",
        "oauth_token_families",
        ["revoked_at"],
        unique=False,
    )

    op.create_table(
        "oauth_access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        _timestamp("created_at", current_default=True),
        _timestamp("expires_at"),
        _timestamp("revoked_at", nullable=True),
        sa.CheckConstraint(
            "length(token_digest) = 64",
            name="ck_oauth_access_tokens_token_digest_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["oauth_token_families.id"],
            name="fk_oauth_access_tokens_family_id_oauth_token_families",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_access_tokens"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_oauth_access_tokens_token_digest",
        ),
    )
    op.create_index(
        "ix_oauth_access_tokens_expires_at",
        "oauth_access_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_access_tokens_family_id",
        "oauth_access_tokens",
        ["family_id"],
        unique=False,
    )

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        _timestamp("created_at", current_default=True),
        _timestamp("expires_at"),
        _timestamp("consumed_at", nullable=True),
        _timestamp("revoked_at", nullable=True),
        sa.CheckConstraint(
            "length(token_digest) = 64",
            name="ck_oauth_refresh_tokens_token_digest_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["oauth_token_families.id"],
            name="fk_oauth_refresh_tokens_family_id_oauth_token_families",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_refresh_tokens"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_oauth_refresh_tokens_token_digest",
        ),
    )
    op.create_index(
        "ix_oauth_refresh_tokens_expires_at",
        "oauth_refresh_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_refresh_tokens_family_id",
        "oauth_refresh_tokens",
        ["family_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oauth_refresh_tokens_family_id",
        table_name="oauth_refresh_tokens",
    )
    op.drop_index(
        "ix_oauth_refresh_tokens_expires_at",
        table_name="oauth_refresh_tokens",
    )
    op.drop_table("oauth_refresh_tokens")

    op.drop_index(
        "ix_oauth_access_tokens_family_id",
        table_name="oauth_access_tokens",
    )
    op.drop_index(
        "ix_oauth_access_tokens_expires_at",
        table_name="oauth_access_tokens",
    )
    op.drop_table("oauth_access_tokens")

    op.drop_index(
        "ix_oauth_token_families_revoked_at",
        table_name="oauth_token_families",
    )
    op.drop_index(
        "ix_oauth_token_families_expires_at",
        table_name="oauth_token_families",
    )
    op.drop_table("oauth_token_families")

    op.drop_index(
        "ix_oauth_authorization_codes_expires_at",
        table_name="oauth_authorization_codes",
    )
    op.drop_table("oauth_authorization_codes")
