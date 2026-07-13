"""Security hardening: hash password-reset tokens at rest; add auth rate-limit table.

1. ``user.reset_token`` was stored in PLAINTEXT (unlike the already-hashed
   magic-link token). A database leak could be replayed to reset accounts.
   This migration adds an indexed ``reset_token_hash`` column, backfills it
   with ``sha256(reset_token)`` for any outstanding tokens (so links already
   emailed keep working), then drops the plaintext column. The raw token is
   256-bit, so an unsalted digest is safe and preserves O(1) lookup.

2. Adds ``auth_rate_limit_event`` -- the sliding-window counter table backing
   ``utils/rate_limit.py`` for the login / register / reset / magic-link
   endpoints.

Idempotent + existence-guarded so concurrent autoscale instances racing
through ``flask db upgrade`` converge safely, matching the convention of the
existing migrations.

Revision ID: 0009_security_hardening
Revises: 0008_merge_0007_heads
Create Date: 2026-07-14
"""
import hashlib

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0009_security_hardening'
down_revision = '0008_merge_0007_heads'
branch_labels = None
depends_on = None


# Lightweight table handle for portable read/backfill without full reflection.
_user = sa.table(
    'user',
    sa.column('id', sa.Integer),
    sa.column('reset_token', sa.String),
    sa.column('reset_token_hash', sa.String),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_cols = {c['name'] for c in inspector.get_columns('user')}

    # 1. Add the digest column (nullable).
    if 'reset_token_hash' not in user_cols:
        op.add_column(
            'user',
            sa.Column('reset_token_hash', sa.String(length=64), nullable=True),
        )

    # 2. Backfill from any outstanding plaintext tokens, then drop the column.
    if 'reset_token' in user_cols:
        rows = bind.execute(
            sa.select(_user.c.id, _user.c.reset_token).where(
                _user.c.reset_token.isnot(None)
            )
        ).fetchall()
        for row in rows:
            digest = hashlib.sha256(row.reset_token.encode('utf-8')).hexdigest()
            bind.execute(
                sa.update(_user).where(_user.c.id == row.id).values(
                    reset_token_hash=digest
                )
            )
        # Drop the plaintext column. Its auto index (if present) is dropped
        # first where named; on Postgres drop_column also cascades any
        # remaining dependent index.
        existing_idx = {i['name'] for i in inspector.get_indexes('user')}
        if 'ix_user_reset_token' in existing_idx:
            op.drop_index('ix_user_reset_token', table_name='user')
        op.drop_column('user', 'reset_token')

    # 3. Index the digest column for O(1) lookup-by-token.
    existing_idx = {i['name'] for i in inspector.get_indexes('user')}
    if 'ix_user_reset_token_hash' not in existing_idx:
        op.create_index('ix_user_reset_token_hash', 'user', ['reset_token_hash'])

    # 4. Auth rate-limit counter table.
    if 'auth_rate_limit_event' not in set(inspector.get_table_names()):
        op.create_table(
            'auth_rate_limit_event',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('rate_key', sa.String(length=200), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index(
            'idx_auth_rl_key_ts',
            'auth_rate_limit_event',
            ['rate_key', 'created_at'],
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'auth_rate_limit_event' in set(inspector.get_table_names()):
        op.drop_index('idx_auth_rl_key_ts', table_name='auth_rate_limit_event')
        op.drop_table('auth_rate_limit_event')

    user_cols = {c['name'] for c in inspector.get_columns('user')}
    # Restore the plaintext column shape (data is not recoverable; any
    # outstanding tokens are invalidated on downgrade).
    if 'reset_token' not in user_cols:
        op.add_column(
            'user', sa.Column('reset_token', sa.String(length=100), nullable=True)
        )
        op.create_index('ix_user_reset_token', 'user', ['reset_token'])

    existing_idx = {i['name'] for i in inspector.get_indexes('user')}
    if 'ix_user_reset_token_hash' in existing_idx:
        op.drop_index('ix_user_reset_token_hash', table_name='user')
    if 'reset_token_hash' in {c['name'] for c in inspector.get_columns('user')}:
        op.drop_column('user', 'reset_token_hash')
