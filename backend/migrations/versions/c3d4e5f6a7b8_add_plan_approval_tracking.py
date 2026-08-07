"""add approval/abort tracking to surgical_plans

Revision ID: c3d4e5f6a7b8
Revises: b2f3d4e5f6a7
Create Date: 2026-08-07 00:00:00.000000

Avant cette migration, un plan chirurgical passait à APPROVED via un simple PUT
générique (routers/plans.py) : aucune colonne ne distinguait qui avait validé le
plan (par opposition à qui l'avait créé), ni quand, ni pourquoi un plan avait été
abandonné. Ces colonnes sont désormais renseignées exclusivement par les endpoints
dédiés POST /plans/{id}/approve (rôle surgeon/admin requis) et /abort (motif
obligatoire) — voir routers/plans.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2f3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('surgical_plans', sa.Column('approved_by_username', sa.String(length=64), nullable=True))
    op.add_column('surgical_plans', sa.Column('approved_at', sa.DateTime(), nullable=True))
    op.add_column('surgical_plans', sa.Column('aborted_by_username', sa.String(length=64), nullable=True))
    op.add_column('surgical_plans', sa.Column('aborted_at', sa.DateTime(), nullable=True))
    op.add_column('surgical_plans', sa.Column('abort_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('surgical_plans', 'abort_reason')
    op.drop_column('surgical_plans', 'aborted_at')
    op.drop_column('surgical_plans', 'aborted_by_username')
    op.drop_column('surgical_plans', 'approved_at')
    op.drop_column('surgical_plans', 'approved_by_username')
