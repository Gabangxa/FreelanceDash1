"""Add email_delivery_log table.

The ``EmailDeliveryLog`` table tracks every outbound email send attempt
(recipient, subject, status, attempt count, last error, timestamps). It is
the foundation for the queue-based email worker planned for the next phase.

The table may already have been auto-created by ``db.create_all()`` in
existing environments, so the upgrade is idempotent: it inspects the
database and only creates the table if it's missing. This lets the
migration coexist with the bootstrap-time ``create_all()`` until that is
fully removed.

Revision ID: 0002_add_email_delivery_log
Revises: 0001_add_event_metadata
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002_add_email_delivery_log'
down_revision = '0001_add_event_metadata'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'email_delivery_log' in inspector.get_table_names():
        # Already created via db.create_all() -- nothing to do.
        return

    op.create_table(
        'email_delivery_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('recipient', sa.String(length=254), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_email_delivery_log_recipient', 'email_delivery_log', ['recipient'])
    op.create_index('ix_email_delivery_log_status', 'email_delivery_log', ['status'])
    op.create_index('ix_email_delivery_log_created_at', 'email_delivery_log', ['created_at'])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'email_delivery_log' not in inspector.get_table_names():
        return
    op.drop_index('ix_email_delivery_log_created_at', table_name='email_delivery_log')
    op.drop_index('ix_email_delivery_log_status', table_name='email_delivery_log')
    op.drop_index('ix_email_delivery_log_recipient', table_name='email_delivery_log')
    op.drop_table('email_delivery_log')
