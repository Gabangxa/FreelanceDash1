"""Enforce one Subscription per user via a unique index on user_id.

The application has always treated subscriptions as one-per-user
(``User.subscription`` is ``uselist=False`` and routes call ``.first()``),
but the DB only had a non-unique index on ``subscription.user_id``. With 4
gunicorn workers, a user racing through checkout twice could trigger two
``subscription.created`` deliveries that both insert distinct rows for the
same user; which one "wins" on subsequent reads was nondeterministic.

This migration:

* Cleans up any pre-existing duplicates by keeping the row most likely to
  reflect the user's *real* current plan -- preferring an ``active`` row,
  then the most-recently updated, then the highest id. SubscriptionLog
  rows pointing at the losing duplicates are reparented to the winner so
  we don't lose audit history.
* Adds a unique index on ``subscription.user_id``. From here on, the DB
  guarantees the invariant and the IntegrityError recovery path in
  ``polar.routes._process_subscription_upsert`` handles racing webhooks.

Idempotent + SQLite-safe via ``batch_alter_table`` so the test harness
(in-memory SQLite) survives.

Revision ID: 0007_subscription_user_id_unique
Revises: 0006_money_to_numeric
Create Date: 2026-05-03
"""
from alembic import op
import sqlalchemy as sa


revision = '0007_subscription_user_id_unique'
down_revision = '0006_money_to_numeric'
branch_labels = None
depends_on = None


UNIQUE_INDEX_NAME = 'uq_subscription_user_id'
LEGACY_INDEX_NAME = 'ix_subscription_user_id'


def _has_index(bind, table, name):
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(idx['name'] == name for idx in inspector.get_indexes(table))


def _has_unique_constraint(bind, table, name):
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(
        uc.get('name') == name
        for uc in inspector.get_unique_constraints(table)
    )


def _dedupe_subscriptions(bind):
    """Collapse duplicate subscriptions per user to one winning row.

    Selection order (best -> worst): ``status='active'`` first, then the
    most-recently updated, then the highest id (insert order). The
    ordering is done in SQL so we don't have to care whether the bound
    DB hands us ``updated_at`` as a Python ``datetime`` (Postgres) or a
    string (SQLite) -- both back-ends sort the column the same way
    server-side. Logs are reparented to the winner; losing Subscription
    rows are deleted.
    """
    duplicate_user_ids = bind.execute(sa.text(
        "SELECT user_id FROM subscription "
        "GROUP BY user_id HAVING COUNT(*) > 1"
    )).fetchall()
    for (user_id,) in duplicate_user_ids:
        sibs = bind.execute(sa.text(
            # Lowest sort key wins. NULL updated_at is sorted last
            # explicitly so it doesn't beat a real timestamp on either
            # back-end (SQLite sorts NULLs first by default).
            "SELECT id FROM subscription WHERE user_id = :uid "
            "ORDER BY "
            "  CASE WHEN status = 'active' THEN 0 ELSE 1 END ASC, "
            "  CASE WHEN updated_at IS NULL THEN 1 ELSE 0 END ASC, "
            "  updated_at DESC, "
            "  id DESC"
        ), {"uid": user_id}).fetchall()
        if len(sibs) <= 1:
            continue
        winner_id = sibs[0].id
        loser_ids = [r.id for r in sibs[1:]]
        bind.execute(sa.text(
            "UPDATE subscription_log SET subscription_id = :win "
            "WHERE subscription_id IN :losers"
        ).bindparams(sa.bindparam('losers', expanding=True)),
            {"win": winner_id, "losers": loser_ids},
        )
        bind.execute(sa.text(
            "DELETE FROM subscription WHERE id IN :losers"
        ).bindparams(sa.bindparam('losers', expanding=True)),
            {"losers": loser_ids},
        )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscription' not in inspector.get_table_names():
        # Fresh DB bootstrapped via db.create_all() with the new model
        # already in place -- nothing to migrate.
        return

    _dedupe_subscriptions(bind)

    if _has_index(bind, 'subscription', UNIQUE_INDEX_NAME) or \
       _has_unique_constraint(bind, 'subscription', UNIQUE_INDEX_NAME):
        return

    # Drop the legacy non-unique index first if present; the unique index
    # subsumes it (Postgres can satisfy equality lookups from a unique
    # index just as well).
    if _has_index(bind, 'subscription', LEGACY_INDEX_NAME):
        with op.batch_alter_table('subscription') as batch_op:
            batch_op.drop_index(LEGACY_INDEX_NAME)

    with op.batch_alter_table('subscription') as batch_op:
        batch_op.create_index(
            UNIQUE_INDEX_NAME, ['user_id'], unique=True,
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscription' not in inspector.get_table_names():
        return

    if _has_index(bind, 'subscription', UNIQUE_INDEX_NAME):
        with op.batch_alter_table('subscription') as batch_op:
            batch_op.drop_index(UNIQUE_INDEX_NAME)

    if not _has_index(bind, 'subscription', LEGACY_INDEX_NAME):
        with op.batch_alter_table('subscription') as batch_op:
            batch_op.create_index(
                LEGACY_INDEX_NAME, ['user_id'], unique=False,
            )
