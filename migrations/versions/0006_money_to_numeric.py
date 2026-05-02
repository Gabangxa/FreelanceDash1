"""Convert money/quantity columns from Float to Numeric.

The original schema stored ``Invoice.amount`` and the four ``InvoiceItem``
numeric columns (plus ``Subscription.amount``) as ``Float``. Floats use
binary fractions, so simple sums like ``0.10 + 0.20`` produce
``0.30000000000000004`` -- a 1e-17 cents drift that adds up over many
items and trips up downstream reconciliation.

This migration switches those columns to ``Numeric(precision=12, scale=2)``
for money and ``Numeric(precision=12, scale=4)`` for quantity (so
fractional hours like 1.25h round-trip exactly). It is:

* **Idempotent** -- inspects each column and only alters it if the
  current type isn't already ``Numeric``.
* **SQLite-safe** -- uses ``batch_alter_table`` so the test harness
  (in-memory SQLite) and any dev-mode SQLite users survive.
* **Reversible** -- ``downgrade`` flips the columns back to ``Float``.

Revision ID: 0006_money_to_numeric
Revises: 0005_add_user_oauth_columns
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


revision = '0006_money_to_numeric'
down_revision = '0005_add_user_oauth_columns'
branch_labels = None
depends_on = None


# (table_name, column_name, target_type, nullable)
# Quantity gets scale=4; money gets scale=2.
_MONEY_COLUMNS = [
    ('invoice', 'amount', sa.Numeric(precision=12, scale=2), False),
    ('invoice_item', 'quantity', sa.Numeric(precision=12, scale=4), False),
    ('invoice_item', 'rate', sa.Numeric(precision=12, scale=2), False),
    ('invoice_item', 'amount', sa.Numeric(precision=12, scale=2), False),
    ('subscription', 'amount', sa.Numeric(precision=12, scale=2), False),
]


def _column_type_name(bind, table, column):
    """Return the inspector-reported type name for the column, or None
    if the table/column doesn't exist yet (fresh DBs that ran
    ``db.create_all()`` already have the new types -- nothing to do)."""
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return None
    for col in inspector.get_columns(table):
        if col['name'] == column:
            return type(col['type']).__name__
    return None


def upgrade():
    bind = op.get_bind()
    for table, column, new_type, nullable in _MONEY_COLUMNS:
        type_name = _column_type_name(bind, table, column)
        if type_name is None:
            # Table/column missing -- either the table was just created
            # by ``db.create_all()`` with the new types, or the install
            # genuinely doesn't have it yet. Either way: nothing to do.
            continue
        if 'NUMERIC' in type_name.upper() or 'DECIMAL' in type_name.upper():
            # Already converted -- migration ran before, or models were
            # bootstrapped via create_all() with the new types. Skip.
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.Float(),
                type_=new_type,
                existing_nullable=nullable,
                postgresql_using=f"{column}::numeric",
            )


def downgrade():
    bind = op.get_bind()
    for table, column, _new_type, nullable in _MONEY_COLUMNS:
        type_name = _column_type_name(bind, table, column)
        if type_name is None:
            continue
        if 'FLOAT' in type_name.upper() or 'REAL' in type_name.upper():
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.Numeric(precision=12, scale=4 if column == 'quantity' else 2),
                type_=sa.Float(),
                existing_nullable=nullable,
                postgresql_using=f"{column}::double precision",
            )
