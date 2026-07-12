from contextlib import contextmanager
import fcntl
from functools import lru_cache
import logging
from pathlib import Path
from time import perf_counter
from threading import Lock

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from vsniper.core.config import get_settings

_migration_thread_lock = Lock()
logger = logging.getLogger(__name__)


def configure_app_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    logging.getLogger("vsniper").setLevel(logging.INFO)


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=1)
def _get_engine():
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_wal_mode(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=5000")

    return engine


def _get_session_factory():
    return sessionmaker(bind=_get_engine(), autoflush=False, autocommit=False, expire_on_commit=False)


def dispose_engine() -> None:
    """Dispose the cached SQLAlchemy engine and clear the lru_cache so the next
    `_get_engine()` call opens a fresh pool against the (possibly replaced) DB file.

    Used by the full-state import path after overwriting `vsniper.db` on disk."""
    info = _get_engine.cache_info()
    if info.currsize > 0:
        _get_engine().dispose()
    _get_engine.cache_clear()


def _alembic_config() -> Config:
    # backend/alembic.ini sits two directories above this file: src/vsniper/core/database.py
    backend_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def init_database() -> None:
    """Bring the database schema up to head via Alembic."""
    from vsniper.db import models  # noqa: F401  (ensure models register on Base.metadata)

    settings = get_settings()
    lock_path = settings.resolve_path(settings.sqlite_path).parent / ".migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    logger.info("database initialization started lock_path=%s database_url=%s", lock_path, settings.database_url)
    with _migration_thread_lock:
        with lock_path.open("w") as lock_file:
            logger.info("database migration waiting for file lock")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            logger.info("database migration file lock acquired")
            try:
                migration_started = perf_counter()
                command.upgrade(_alembic_config(), "head")
                configure_app_logging()
                logger.info("database migration finished in %.1fs", perf_counter() - migration_started)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                logger.info("database migration file lock released")
    logger.info("database initialization finished in %.1fs", perf_counter() - started)


@contextmanager
def session_scope():
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
