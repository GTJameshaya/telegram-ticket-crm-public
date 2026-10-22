"""messages.meta: метаданные внешнего источника (ingest API)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 19:40:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('messages') as batch:
        batch.add_column(sa.Column('meta', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('messages') as batch:
        batch.drop_column('meta')
