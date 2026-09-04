import asyncio
from logging.config import fileConfig
from pathlib import Path
from sqlalchemy import inspect, pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context
from alembic.script import ScriptDirectory

from app.core.config import settings
from app.core.database import Base, connect_args
import app.models  # noqa: F401 — import all models so Alembic detects them

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
COMPATIBILITY_REVISION = "billrecon0802"
COMPATIBILITY_SCHEMA = Path(__file__).with_name("schema_billrecon0802.sql")


def _bootstrap_legacy_baseline_if_empty(connection) -> None:
    """Install the frozen billrecon0802 schema for an empty ``upgrade head``.

    The root revision is deliberately an empty stamp anchor because the original
    production database was created with ``Base.metadata.create_all``.  Historical
    revisions therefore cannot build a database from zero.  The checked-in SQL
    snapshot is immutable at billrecon0802: later models must not change bootstrap
    state, and every descendant migration must still execute normally.  Any table,
    including Alembic's version table, is existing state and bypasses this path.
    """
    migration_context = context.get_context()
    if migration_context.opts.get("destination_rev") not in {"head", "heads"}:
        return

    tables = set(
        inspect(connection).get_table_names(
            schema=migration_context.version_table_schema
        )
    )
    if tables:
        return

    script = ScriptDirectory.from_config(config)
    script.get_revision(COMPATIBILITY_REVISION)
    snapshot_sql = COMPATIBILITY_SCHEMA.read_text(encoding="utf-8")
    connection.connection.dbapi_connection.run_async(
        lambda driver_connection: driver_connection.execute(snapshot_sql)
    )
    migration_context.stamp(script, COMPATIBILITY_REVISION)


def include_object(object_, name, type_, reflected, compare_to):
    """Keep startup-DDL compatibility objects out of model drift checks."""
    if type_ == "index" and name == "ux_owners_username":
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        _bootstrap_legacy_baseline_if_empty(connection)
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Neon 需要显式 SSL context（同 database.py），否则 asyncpg 连不上。
        # 复用 database.py 的 connect_args，保证在线迁移与应用连库口径一致。
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
