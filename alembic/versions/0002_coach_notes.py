"""add coach_notes for long-term coach memory

Revision ID: 0002_coach_notes
Revises: 0001_initial
Create Date: 2026-02-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_coach_notes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="note"),
        sa.Column("title", sa.String(length=128), nullable=True),
        sa.Column("note_json", sa.Text(), nullable=True),
        sa.Column("note_text", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_coach_notes_user_id", "coach_notes", ["user_id"], unique=False)
    op.create_index("ix_coach_notes_user_created", "coach_notes", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_coach_notes_user_created", table_name="coach_notes")
    op.drop_index("ix_coach_notes_user_id", table_name="coach_notes")
    op.drop_table("coach_notes")

