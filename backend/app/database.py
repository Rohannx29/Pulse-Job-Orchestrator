from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


try:
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
except Exception:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

Base = declarative_base()

_tables_created = False


def _init_sqlite_tables():
    global _tables_created
    if not _tables_created and engine.url.drivername.startswith("sqlite"):
        import app.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        _tables_created = True


class _SessionFactory(sessionmaker):
    def __call__(self, *args, **kwargs):
        _init_sqlite_tables()
        return super().__call__(*args, **kwargs)


SessionLocal = _SessionFactory(autocommit=False, autoflush=False, bind=engine, future=True)


def get_db():
    _init_sqlite_tables()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

