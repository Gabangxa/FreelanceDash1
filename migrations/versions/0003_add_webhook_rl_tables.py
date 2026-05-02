"""Add webhook rate-limit, failed-attempt, and cache tables.

These three tables back the DB fallback path of the new pluggable webhook
storage backend (``webhooks/storage.py``). They are only used when
``REDIS_URL`` is unset; with Redis the same logical state is kept in
sorted sets and this schema is unused but harmless.

* ``webhook_rate_limit_event(rate_key, created_at)`` -- one row per
  webhook request, scanned by ``WebhookSecurity.check_rate_limit`` to
  count requests inside the trailing window.
* ``webhook_failed_attempt(attempt_key, created_at)`` -- same shape but
  for failed signature/IP/etc. validations.
* ``webhook_cache_entry(cache_key PK, value, expires_at)`` -- tiny KV
  cache used by ``webhooks/ip_ranges`` to memoise the upstream
  GitHub/Stripe IP allowlists for 6h.

The migration is idempotent (so it coexists with ``db.create_all()`` in
existing environments) and reversible.

Revision ID: 0003_add_webhook_rl_tables
Revises: 0002_add_email_delivery_log
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
# NOTE: alembic_version.version_num is varchar(32); keep this string short.
revision = '0003_add_webhook_rl_tables'
down_revision = '0002_add_email_delivery_log'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if 'webhook_rate_limit_event' not in existing:
        op.create_table(
            'webhook_rate_limit_event',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('rate_key', sa.String(length=200), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index(
            'idx_webhook_rl_key_ts',
            'webhook_rate_limit_event',
            ['rate_key', 'created_at'],
        )

    if 'webhook_failed_attempt' not in existing:
        op.create_table(
            'webhook_failed_attempt',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('attempt_key', sa.String(length=200), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index(
            'idx_webhook_fa_key_ts',
            'webhook_failed_attempt',
            ['attempt_key', 'created_at'],
        )

    if 'webhook_cache_entry' not in existing:
        op.create_table(
            'webhook_cache_entry',
            sa.Column('cache_key', sa.String(length=200), primary_key=True),
            sa.Column('value', sa.Text(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
        )
        op.create_index(
            'ix_webhook_cache_entry_expires_at',
            'webhook_cache_entry',
            ['expires_at'],
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if 'webhook_cache_entry' in existing:
        op.drop_index(
            'ix_webhook_cache_entry_expires_at',
            table_name='webhook_cache_entry',
        )
        op.drop_table('webhook_cache_entry')

    if 'webhook_failed_attempt' in existing:
        op.drop_index(
            'idx_webhook_fa_key_ts',
            table_name='webhook_failed_attempt',
        )
        op.drop_table('webhook_failed_attempt')

    if 'webhook_rate_limit_event' in existing:
        op.drop_index(
            'idx_webhook_rl_key_ts',
            table_name='webhook_rate_limit_event',
        )
        op.drop_table('webhook_rate_limit_event')
