"""Миграция Alembic применяется к чистой БД и создаёт все таблицы модели."""
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

from app.models import Base

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_head(tmp_path):
    db = tmp_path / "m.db"
    env = {"DATABASE_URL": f"sqlite+aiosqlite:///{db}", "PATH": ""}
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    tables = set(sa.inspect(sa.create_engine(f"sqlite:///{db}")).get_table_names())
    assert set(Base.metadata.tables) <= tables
