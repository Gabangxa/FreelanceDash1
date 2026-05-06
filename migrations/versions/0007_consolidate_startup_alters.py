"""Consolidate startup-time ALTER TABLE blocks into a real Alembic migration.

Historically, ``app.py`` ran a hand-rolled ``ALTER TABLE ADD COLUMN`` block
at startup to keep previously-provisioned databases in sync with the model
when new columns were added (Task #25 user_settings branding/feature flag,
Task #28 time_entry invoiced markers, Task #27 project default hourly rate).
That worked, but it left us with two parallel migration mechanisms -- the
inline ALTERs and Alembic -- which can silently diverge.

This migration takes ownership of all of those columns. It is fully
**idempotent**: every column add inspects the live schema first and skips
columns that already exist, so it is safe to run on:

* Fresh databases (created by any future ``flask db upgrade`` from zero).
* Databases that the inline ALTER block already patched in production.
* SQLite (test harness) -- uses ``batch_alter_table`` for the FK column.

After this lands, ``app.py`` no longer creates schema at boot; the operator
must run ``flask db upgrade`` before serving traffic, and a startup check
warns to stderr if Alembic ``current`` != ``head``.

Revision ID: 0007_consolidate_startup_alters
Revises: 0006_money_to_numeric
Create Date: 2026-05-06
"""
from alembic import op
import sqlalchemy as sa


revision = '0007_consolidate_startup_alters'
down_revision = '0006_money_to_numeric'
branch_labels = None
depends_on = None


# ----------------------------------------------------------------------- #
# Helpers -- every operation is guarded against the column / index already
# existing on the live DB, because the inline ALTER block in app.py has
# been adding these columns at boot for months on the production database.
# ----------------------------------------------------------------------- #
def _existing_columns(table_name: str) -> set:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {c['name'] for c in inspector.get_columns(table_name)}


def _existing_indexes(table_name: str) -> set:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {ix['name'] for ix in inspector.get_indexes(table_name)}


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    """Idempotent ``op.add_column`` -- skips if the column is already there.

    Also short-circuits if the table itself doesn't exist yet, so this
    migration never explodes on partially-bootstrapped databases (e.g.
    a fresh install that hasn't run the baseline schema setup yet).
    """
    if not _table_exists(table_name):
        return
    if column.name in _existing_columns(table_name):
        return
    # Wrap in batch_alter_table so SQLite (test harness) can also apply
    # FK-bearing additions like time_entry.invoice_id.
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(column)


def _create_index_if_missing(index_name: str, table_name: str,
                             columns: list) -> None:
    if not _table_exists(table_name):
        return
    if index_name in _existing_indexes(table_name):
        return
    op.create_index(index_name, table_name, columns)


def _drop_index_if_present(index_name: str, table_name: str) -> None:
    if not _table_exists(table_name):
        return
    if index_name not in _existing_indexes(table_name):
        return
    op.drop_index(index_name, table_name=table_name)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    if not _table_exists(table_name):
        return
    if column_name not in _existing_columns(table_name):
        return
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_column(column_name)


# ----------------------------------------------------------------------- #
# Upgrade
# ----------------------------------------------------------------------- #
def upgrade():
    # --- user_settings (Task #25 branding + time-to-invoice flag) ---
    _add_column_if_missing(
        'user_settings',
        sa.Column('invoice_signature', sa.LargeBinary(), nullable=True),
    )
    _add_column_if_missing(
        'user_settings',
        sa.Column('invoice_signature_mimetype', sa.String(length=30),
                  nullable=True),
    )
    _add_column_if_missing(
        'user_settings',
        sa.Column('invoice_font', sa.String(length=20),
                  server_default=sa.text("'helvetica'"), nullable=True),
    )
    # NOT NULL + default TRUE: server_default backfills existing rows so
    # the NOT NULL constraint is satisfiable on databases that already
    # have user_settings rows.
    _add_column_if_missing(
        'user_settings',
        sa.Column('time_to_invoice_enabled', sa.Boolean(),
                  server_default=sa.true(), nullable=False),
    )

    # --- time_entry (Task #28 invoiced markers) ---
    _add_column_if_missing(
        'time_entry',
        sa.Column('invoiced_at', sa.DateTime(), nullable=True),
    )
    _create_index_if_missing(
        'ix_time_entry_invoiced_at', 'time_entry', ['invoiced_at'],
    )

    _add_column_if_missing(
        'time_entry',
        sa.Column(
            'invoice_id', sa.Integer(),
            sa.ForeignKey('invoice.id', name='fk_time_entry_invoice_id'),
            nullable=True,
        ),
    )
    _create_index_if_missing(
        'ix_time_entry_invoice_id', 'time_entry', ['invoice_id'],
    )

    # --- project (Task #27 default hourly rate) ---
    _add_column_if_missing(
        'project',
        sa.Column('default_hourly_rate', sa.Numeric(precision=12, scale=2),
                  nullable=True),
    )


# ----------------------------------------------------------------------- #
# Downgrade
# ----------------------------------------------------------------------- #
def downgrade():
    _drop_index_if_present('ix_time_entry_invoice_id', 'time_entry')
    _drop_index_if_present('ix_time_entry_invoiced_at', 'time_entry')

    _drop_column_if_present('project', 'default_hourly_rate')
    _drop_column_if_present('time_entry', 'invoice_id')
    _drop_column_if_present('time_entry', 'invoiced_at')
    _drop_column_if_present('user_settings', 'time_to_invoice_enabled')
    _drop_column_if_present('user_settings', 'invoice_font')
    _drop_column_if_present('user_settings', 'invoice_signature_mimetype')
    _drop_column_if_present('user_settings', 'invoice_signature')
