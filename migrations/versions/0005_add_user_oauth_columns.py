"""Add OAuth provider linkage columns to the user table.

Backs the "Continue with Google" sign-in flow added in Task #17. Two
new nullable columns plus a composite unique index on the ``user`` table:

* ``oauth_provider`` -- short provider key (e.g. ``"google"``). NULL for
  accounts that were created purely via email/password.
* ``oauth_provider_id`` -- the provider's stable subject identifier
  (Google's ``sub`` claim, never the email -- emails can change inside
  the provider account).

A composite ``UNIQUE (oauth_provider, oauth_provider_id)`` constraint
guarantees that two app accounts can never both claim the same Google
identity. The matching composite index doubles as the lookup index for
the OAuth callback (which queries by both columns together).

The migration is idempotent (introspects current columns / indexes
first) so it coexists safely with environments that already added these
columns manually before the migration was authored, and reversible.

Revision ID: 0005_add_user_oauth_columns
Revises: 0004_add_user_magic_link
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
# NOTE: alembic_version.version_num is varchar(32); keep this string short.
revision = '0005_add_user_oauth_columns'
down_revision = '0004_add_user_magic_link'
branch_labels = None
depends_on = None


def _existing_columns(bind, table):
    inspector = sa.inspect(bind)
    return {c['name'] for c in inspector.get_columns(table)}


def _existing_indexes(bind, table):
    inspector = sa.inspect(bind)
    return {i['name'] for i in inspector.get_indexes(table)}


def _existing_unique_constraints(bind, table):
    inspector = sa.inspect(bind)
    try:
        return {c['name'] for c in inspector.get_unique_constraints(table)}
    except NotImplementedError:
        return set()


def upgrade():
    bind = op.get_bind()
    cols = _existing_columns(bind, 'user')

    if 'oauth_provider' not in cols:
        op.add_column(
            'user',
            sa.Column('oauth_provider', sa.String(length=32), nullable=True),
        )
    if 'oauth_provider_id' not in cols:
        op.add_column(
            'user',
            sa.Column('oauth_provider_id', sa.String(length=255), nullable=True),
        )

    indexes = _existing_indexes(bind, 'user')
    uniques = _existing_unique_constraints(bind, 'user')

    if 'uq_user_oauth_provider_subject' not in uniques:
        # SQLite (used by tests) cannot ALTER ADD CONSTRAINT, so use the
        # batch operation which rebuilds the table on SQLite and emits a
        # plain ALTER on Postgres/MySQL.
        with op.batch_alter_table('user') as batch_op:
            batch_op.create_unique_constraint(
                'uq_user_oauth_provider_subject',
                ['oauth_provider', 'oauth_provider_id'],
            )

    if 'ix_user_oauth_provider_subject' not in indexes:
        op.create_index(
            'ix_user_oauth_provider_subject',
            'user',
            ['oauth_provider', 'oauth_provider_id'],
        )


def downgrade():
    bind = op.get_bind()
    indexes = _existing_indexes(bind, 'user')
    uniques = _existing_unique_constraints(bind, 'user')
    cols = _existing_columns(bind, 'user')

    if 'ix_user_oauth_provider_subject' in indexes:
        op.drop_index('ix_user_oauth_provider_subject', table_name='user')
    if 'uq_user_oauth_provider_subject' in uniques:
        with op.batch_alter_table('user') as batch_op:
            batch_op.drop_constraint(
                'uq_user_oauth_provider_subject', type_='unique',
            )
    if 'oauth_provider_id' in cols:
        op.drop_column('user', 'oauth_provider_id')
    if 'oauth_provider' in cols:
        op.drop_column('user', 'oauth_provider')
