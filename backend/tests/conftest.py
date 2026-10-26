import pytest
import psycopg
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.config import settings


def pytest_configure(config):
    """Disable slowapi rate limiting for all tests."""
    import app.core.rate_limit as rl
    rl.limiter.enabled = False
    rl.limiter._enabled = False


@pytest.fixture(scope="function")
def conn():
    """
    DB connection that bypasses auth.users FK via session_replication_role=replica.
    All changes are rolled back after each test (clean state).
    """
    c = psycopg.connect(settings.DATABASE_URL)
    c.autocommit = False
    with c.cursor() as cur:
        cur.execute("SET session_replication_role = 'replica'")
    yield c
    c.rollback()
    c.close()
