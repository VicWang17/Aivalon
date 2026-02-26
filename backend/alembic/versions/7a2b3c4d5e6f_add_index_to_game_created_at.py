"""add index to game created_at

Revision ID: 7a2b3c4d5e6f
Revises: 43450dfd462d
Create Date: 2026-02-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a2b3c4d5e6f'
down_revision: Union[str, Sequence[str], None] = '43450dfd462d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(op.f('ix_games_created_at'), 'games', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_games_created_at'), table_name='games')
