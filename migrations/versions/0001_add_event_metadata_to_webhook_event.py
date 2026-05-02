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
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Use batch_alter_table so the migration also works on SQLite (used by
    # the test suite). On PostgreSQL this is a plain ALTER TABLE ADD COLUMN.
    with op.batch_alter_table('webhook_event') as batch_op:
        batch_op.add_column(sa.Column('event_metadata', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('webhook_event') as batch_op:
        batch_op.drop_column('event_metadata')
