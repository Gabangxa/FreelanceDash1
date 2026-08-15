"""Add durable-outbox columns to email_delivery_log.

FD-020: email was sent from a daemon thread that gunicorn's worker recycle
could kill mid-send, silently losing password resets and magic links. The
fix turns ``email_delivery_log`` into a real outbox: ``mail.send_email``
enqueues a row and a scheduled drainer sends it. For a separate process to
send a message the original request never delivered, the row must carry the
rendered message and a timestamp for reclaiming crashed-drainer rows:

* ``sender``      -- envelope From (nullable; falls back to the app default)
* ``text_body``   -- plain-text body
* ``html_body``   -- optional HTML body
* ``updated_at``  -- bumped on every state change; the drainer reclaims rows
                     stuck in 'sending' by a crashed drainer using this.

All columns are nullable so the migration needs no backfill of existing
rows. Idempotent (introspects current columns first), matching the
convention of the surrounding migrations.

Revision ID: 0010_email_outbox_columns
Revises: 0009_security_hardening
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0010_email_outbox_columns'
down_revision = '0009_security_hardening'
branch_labels = None
depends_on = None


def _existing_columns(bind, table):
    inspector = sa.inspect(bind)
    return {c['name'] for c in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    cols = _existing_columns(bind, 'email_delivery_log')

    if 'sender' not in cols:
        op.add_column('email_delivery_log', sa.Column('sender', sa.String(length=254), nullable=True))
    if 'text_body' not in cols:
        op.add_column('email_delivery_log', sa.Column('text_body', sa.Text(), nullable=True))
    if 'html_body' not in cols:
        op.add_column('email_delivery_log', sa.Column('html_body', sa.Text(), nullable=True))
    if 'updated_at' not in cols:
        op.add_column('email_delivery_log', sa.Column('updated_at', sa.DateTime(), nullable=True))


def downgrade():
    bind = op.get_bind()
    cols = _existing_columns(bind, 'email_delivery_log')

    # SQLite cannot DROP COLUMN before 3.35; use batch so tests/dev on SQLite
    # rebuild the table while Postgres emits plain ALTERs.
    with op.batch_alter_table('email_delivery_log') as batch_op:
        for name in ('updated_at', 'html_body', 'text_body', 'sender'):
            if name in cols:
                batch_op.drop_column(name)
