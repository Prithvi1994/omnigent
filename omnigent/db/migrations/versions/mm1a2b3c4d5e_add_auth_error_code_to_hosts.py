"""add auth_error_code to hosts

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-09-30 00:00:00.000000

Adds ``hosts.auth_error_code`` — why the host's own sign-in to the server
stopped working, as reported in a ``host.auth_status`` frame (e.g.
``"host_auth_expired"``). NULL means healthy or never reported. Surfaced via
``GET /v1/hosts`` so the web UI can tell the user to sign in again on the host.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``auth_error_code`` column to ``hosts``.

    Batch mode so the DDL runs on SQLite too, and so the migration-safety
    test (which requires schema changes to go through ``batch_alter_table``)
    passes.
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("auth_error_code", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Drop the ``auth_error_code`` column from ``hosts``."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("auth_error_code")
