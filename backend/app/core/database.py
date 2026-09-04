import ssl
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

def database_connect_args(database_url: str, app_env: str) -> dict:
    """Return driver options without weakening TLS identity verification.

    asyncpg accepts an SSLContext.  Python's default SERVER_AUTH context
    validates both the certificate chain and the requested hostname, matching
    PostgreSQL's verify-full security properties.
    """
    if "neon.tech" not in database_url and app_env != "production":
        return {}
    return {"ssl": ssl.create_default_context(ssl.Purpose.SERVER_AUTH)}


connect_args = database_connect_args(settings.DATABASE_URL, settings.APP_ENV)

def _database_logging_options(config) -> dict[str, bool]:
    """Keep SQL parameter values hidden and disable echo unconditionally in production."""
    return {
        "echo": bool(config.DEBUG and config.APP_ENV != "production"),
        "hide_parameters": True,
    }

engine = create_async_engine(
    settings.DATABASE_URL,
    **_database_logging_options(settings),
    pool_pre_ping=True,
    # Recycle connections every 5 min — Neon serverless can drop idle conns silently.
    pool_recycle=300,
    pool_size=10 if settings.APP_ENV == "production" else 10,
    max_overflow=20,
    connect_args=connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass
