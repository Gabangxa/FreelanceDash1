"""Add event_metadata column to webhook_event.

The previous code wrote to ``WebhookEvent.metadata`` which collides with
SQLAlchemy's reserved ``MetaData`` registry on DeclarativeBase, so the value
was silently never persisted. This migration adds the correctly-named
``event_metadata`` TEXT column so the audit/security payload (client IP,
payload size, validation time, security_version) round-trips through the
database.

Revision ID: 0001_add_event_metadata
Revises:
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0001_add_event_metadata'
down_revision = '0000_baseline'
branch_labels = None
depends_on = None


def _existing_columns(table_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {c['name'] for c in inspector.get_columns(table_name)}


def upgrade():
    # Idempotent: on a fresh DB the baseline (0000) already created
    # webhook_event with event_metadata in its final shape, so this is
    # a no-op. On a database that pre-dated the baseline, the column
    # is genuinely missing and we add it. Use batch_alter_table so the
    # migration also works on SQLite (used by the test suite).
    if 'event_metadata' in _existing_columns('webhook_event'):
        return
    with op.batch_alter_table('webhook_event') as batch_op:
        batch_op.add_column(sa.Column('event_metadata', sa.Text(), nullable=True))


def downgrade():
    if 'event_metadata' not in _existing_columns('webhook_event'):
        return
    with op.batch_alter_table('webhook_event') as batch_op:
        batch_op.drop_column('event_metadata')
