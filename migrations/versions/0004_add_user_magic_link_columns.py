"""Add magic-link sign-in columns to the user table.

Backs the passwordless "Email me a sign-in link" flow added in Task #16.
Two columns on ``user``:

* ``magic_link_token_hash`` -- werkzeug password hash of the outstanding
  one-shot token. Hashed at rest so a DB leak alone can't be replayed.
* ``magic_link_token_expiry`` -- UTC timestamp after which the token is
  no longer accepted (default issuance is 15 minutes).

The migration is idempotent (introspects current columns first) so it
coexists safely with environments that already added these columns
manually before the migration was authored, and reversible.

Revision ID: 0004_add_user_magic_link
Revises: 0003_add_webhook_rl_tables
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
# NOTE: alembic_version.version_num is varchar(32); keep this string short.
revision = '0004_add_user_magic_link'
down_revision = '0003_add_webhook_rl_tables'
branch_labels = None
depends_on = None


def _existing_columns(bind, table):
    inspector = sa.inspect(bind)
    return {c['name'] for c in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    cols = _existing_columns(bind, 'user')

    if 'magic_link_token_hash' not in cols:
        op.add_column(
            'user',
            sa.Column('magic_link_token_hash', sa.String(length=256), nullable=True),
        )
    if 'magic_link_token_expiry' not in cols:
        op.add_column(
            'user',
            sa.Column('magic_link_token_expiry', sa.DateTime(), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    cols = _existing_columns(bind, 'user')

    if 'magic_link_token_expiry' in cols:
        op.drop_column('user', 'magic_link_token_expiry')
    if 'magic_link_token_hash' in cols:
        op.drop_column('user', 'magic_link_token_hash')
