"""goals + weight_logs + daily_checkins

Revision ID: 0003_goals_weight_checkins
Revises: 0002_coach_notes
Create Date: 2026-02-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_goals_weight_checkins"
down_revision = "0002_coach_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("phase", sa.String(length=16), nullable=False, server_default="cut"),
        sa.Column("target_weight_kg", sa.Float(), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_goals_user_id", "goals", ["user_id"], unique=False)
    op.create_index("ix_goals_user_created", "goals", ["user_id", "created_at"], unique=False)

    op.create_table(
        "weight_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("weight_kg", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_weight_logs_user_id", "weight_logs", ["user_id"], unique=False)
    op.create_index("ix_weight_logs_date", "weight_logs", ["date"], unique=False)
    op.create_index("ix_weight_logs_user_date", "weight_logs", ["user_id", "date"], unique=True)

    op.create_table(
        "daily_checkins",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("calories_ok", sa.Boolean(), nullable=True),
        sa.Column("protein_ok", sa.Boolean(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("sleep_hours", sa.Float(), nullable=True),
        sa.Column("training_done", sa.Boolean(), nullable=True),
        sa.Column("alcohol", sa.Boolean(), nullable=True),
        sa.Column("note_text", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_daily_checkins_user_id", "daily_checkins", ["user_id"], unique=False)
    op.create_index("ix_daily_checkins_date", "daily_checkins", ["date"], unique=False)
    op.create_index("ix_daily_checkins_user_date", "daily_checkins", ["user_id", "date"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_daily_checkins_user_date", table_name="daily_checkins")
    op.drop_index("ix_daily_checkins_date", table_name="daily_checkins")
    op.drop_index("ix_daily_checkins_user_id", table_name="daily_checkins")
    op.drop_table("daily_checkins")

    op.drop_index("ix_weight_logs_user_date", table_name="weight_logs")
    op.drop_index("ix_weight_logs_date", table_name="weight_logs")
    op.drop_index("ix_weight_logs_user_id", table_name="weight_logs")
    op.drop_table("weight_logs")

    op.drop_index("ix_goals_user_created", table_name="goals")
    op.drop_index("ix_goals_user_id", table_name="goals")
    op.drop_table("goals")

