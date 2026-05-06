"""Baseline schema -- create every model-defined table from scratch.

This migration is the new starting point of the Alembic graph. It uses
SQLAlchemy's ``MetaData.create_all(checkfirst=True)`` to materialize
every table currently declared on ``db.metadata``, which is exactly
what the old startup-time ``db.create_all()`` call used to do at boot.

Why a "current-shape" baseline rather than a true historical baseline:

* The project adopted Alembic mid-life. Migrations ``0001`` -- ``0007``
  each ``ALTER`` tables that were originally created by the now-removed
  ``db.create_all()`` call. Reconstructing each one's pre-migration
  shape would mean six historical baselines, each of which would be
  immediately re-altered by the very next migration.
* Letting the baseline create tables in their **current** shape and
  leaning on the fact that every later migration is **idempotent**
  (each guards its ``ALTER`` / ``CREATE`` with an existence check)
  collapses that to a single file. On a fresh DB:
    1. ``0000_baseline`` creates every table in its final shape.
    2. ``0001`` -- ``0007`` each see "column / table / index already
       present" and no-op.
  The DB ends up at head with one round-trip and no manual
  ``db.create_all() + flask db stamp head`` dance.

Idempotency: ``checkfirst=True`` skips tables that already exist, so
running this migration against the existing production database
(which is already at ``0007_consolidate_startup_alters``) is a complete
no-op. It only ever creates missing tables.

Revision ID: 0000_baseline
Revises:
Create Date: 2026-05-06
"""
from alembic import op
from flask import current_app


revision = '0000_baseline'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Pull the live SQLAlchemy metadata via the Flask-Migrate extension --
    # this is the same object Alembic's env.py uses for autogenerate, so
    # it always reflects the current model definitions in models.py.
    db = current_app.extensions['migrate'].db
    bind = op.get_bind()

    # Make sure every model class is imported so its Table is registered
    # against db.metadata. ``app.py`` already does this at boot, but
    # importing again here is cheap and guarantees the metadata is
    # populated even if a future refactor changes the import order.
    import models  # noqa: F401

    # checkfirst=True -> CREATE TABLE IF NOT EXISTS for each table.
    # Safe on the existing production DB (every table already exists,
    # so this is a no-op) and on a fresh empty DB (creates everything).
    db.metadata.create_all(bind=bind, checkfirst=True)


def downgrade():
    # The baseline cannot be reversed without losing every row in the
    # database. We refuse rather than silently destroying data; if you
    # really need to wipe the schema, ``DROP SCHEMA public CASCADE`` is
    # the explicit, intentional way to do it.
    raise RuntimeError(
        "0000_baseline cannot be downgraded -- it would drop every "
        "table in the database. If you genuinely need to reset the "
        "schema, do it manually (e.g. DROP SCHEMA public CASCADE)."
    )
