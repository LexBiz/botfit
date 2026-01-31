"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-01-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("profile_complete", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("age", sa.Integer(), nullable=True),
        sa.Column("sex", sa.String(length=16), nullable=True),
        sa.Column("height_cm", sa.Float(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("activity_level", sa.String(length=16), nullable=True),
        sa.Column("goal", sa.String(length=16), nullable=True),
        sa.Column("allergies", sa.Text(), nullable=True),
        sa.Column("restrictions", sa.Text(), nullable=True),
        sa.Column("favorite_products", sa.Text(), nullable=True),
        sa.Column("disliked_products", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=8), nullable=False, server_default="CZ"),
        sa.Column("stores_csv", sa.String(length=256), nullable=False, server_default="Lidl,Kaufland,Albert"),
        sa.Column("calories_target", sa.Integer(), nullable=True),
        sa.Column("protein_g_target", sa.Integer(), nullable=True),
        sa.Column("fat_g_target", sa.Integer(), nullable=True),
        sa.Column("carbs_g_target", sa.Integer(), nullable=True),
        sa.Column("dialog_state", sa.String(length=64), nullable=True),
        sa.Column("dialog_step", sa.Integer(), nullable=True),
        sa.Column("dialog_data_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_preferences_user_id", "preferences", ["user_id"], unique=True)

    op.create_table(
        "meals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("eaten_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="text"),
        sa.Column("description_raw", sa.Text(), nullable=True),
        sa.Column("meal_json", sa.Text(), nullable=True),
        sa.Column("photo_file_id", sa.String(length=256), nullable=True),
        sa.Column("calories", sa.Integer(), nullable=True),
        sa.Column("protein_g", sa.Integer(), nullable=True),
        sa.Column("fat_g", sa.Integer(), nullable=True),
        sa.Column("carbs_g", sa.Integer(), nullable=True),
        sa.Column("total_weight_g", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_meals_user_id", "meals", ["user_id"], unique=False)
    op.create_index("ix_meals_user_created", "meals", ["user_id", "created_at"], unique=False)

    op.create_table(
        "stats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("weight_start_kg", sa.Float(), nullable=True),
        sa.Column("weight_end_kg", sa.Float(), nullable=True),
        sa.Column("weight_change_kg", sa.Float(), nullable=True),
        sa.Column("avg_calories", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_stats_user_id", "stats", ["user_id"], unique=False)
    op.create_index("ix_stats_week_start", "stats", ["week_start"], unique=False)
    op.create_index("ix_stats_week_end", "stats", ["week_end"], unique=False)

    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("calories_target", sa.Integer(), nullable=True),
        sa.Column("plan_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_plans_user_id", "plans", ["user_id"], unique=False)
    op.create_index("ix_plans_date", "plans", ["date"], unique=False)

    op.create_table(
        "foods",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("barcode", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("brand", sa.String(length=256), nullable=True),
        sa.Column("nutriments_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_foods_barcode", "foods", ["barcode"], unique=False)
    op.create_index("ix_foods_source_barcode", "foods", ["source", "barcode"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_foods_source_barcode", table_name="foods")
    op.drop_index("ix_foods_barcode", table_name="foods")
    op.drop_table("foods")

    op.drop_index("ix_plans_date", table_name="plans")
    op.drop_index("ix_plans_user_id", table_name="plans")
    op.drop_table("plans")

    op.drop_index("ix_stats_week_end", table_name="stats")
    op.drop_index("ix_stats_week_start", table_name="stats")
    op.drop_index("ix_stats_user_id", table_name="stats")
    op.drop_table("stats")

    op.drop_index("ix_meals_user_created", table_name="meals")
    op.drop_index("ix_meals_user_id", table_name="meals")
    op.drop_table("meals")

    op.drop_index("ix_preferences_user_id", table_name="preferences")
    op.drop_table("preferences")

    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")

