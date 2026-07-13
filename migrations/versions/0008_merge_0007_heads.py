"""Merge the two 0007 branch heads into one.

Two revisions branched off 0006 -- ``0007_consolidate_startup_alters`` and
``0007_subscription_user_id_unique`` -- leaving the Alembic graph with two
heads. ``flask db upgrade`` (which the Replit autoscale deploy runs via the
``.replit`` run command) then fails with:

    Multiple head revisions are present for given argument 'head'

and because the deploy command is ``flask db upgrade && exec gunicorn ...``,
the ``&&`` short-circuits and the server never boots. This revision unifies
the two heads so ``upgrade`` resolves to a single head again. No schema
changes.

Revision ID: 0008_merge_0007_heads
Revises: 0007_consolidate_startup_alters, 0007_subscription_user_id_unique
Create Date: 2026-07-14
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision = '0008_merge_0007_heads'
down_revision = ('0007_consolidate_startup_alters', '0007_subscription_user_id_unique')
branch_labels = None
depends_on = None


def upgrade():
    # Pure merge revision -- no operations.
    pass


def downgrade():
    # Pure merge revision -- no operations.
    pass
