"""add general financial metadata to accounts

Revision ID: 066
Revises: 065
Create Date: 2026-08-11

Adds reusable account metadata needed for loans/debt plans while keeping the
existing credit-card cycle fields. payment_due_day is intentionally reused for
both credit cards and loans; service validation decides which fields apply.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "066"
down_revision: Union[str, None] = "065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("interest_rate", sa.Numeric(8, 4), nullable=True))
    op.add_column("accounts", sa.Column("original_principal", sa.Numeric(15, 2), nullable=True))
    op.add_column("accounts", sa.Column("scheduled_payment", sa.Numeric(15, 2), nullable=True))
    op.add_column("accounts", sa.Column("maturity_date", sa.Date(), nullable=True))
    op.add_column("accounts", sa.Column("loan_status", sa.String(50), nullable=True))
    op.add_column("accounts", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "notes")
    op.drop_column("accounts", "loan_status")
    op.drop_column("accounts", "maturity_date")
    op.drop_column("accounts", "scheduled_payment")
    op.drop_column("accounts", "original_principal")
    op.drop_column("accounts", "interest_rate")
