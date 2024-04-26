from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from palace.manager.core.config import Configuration

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# Because this line overrides the logging configuration, we use a flag
# to disable it when running alembic from within the application.
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
from palace.manager.sqlalchemy.model.base import Base
from palace.manager.sqlalchemy.util import LOCK_ID_DB_INIT, pg_advisory_lock

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = Configuration.database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # This deviates from the autogenerated code from alembic a bit in order
    # to be able to take connection from the context which is needed by
    # pytest-alembic to run migrations in a test database.
    # See: https://pytest-alembic.readthedocs.io/en/latest/setup.html#setup
    connectable = context.config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
            **{"url": Configuration.database_url()}
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            # Acquire an application lock to ensure multiple migrations are queued and not concurrent.
            # When alembic is run in the context of the application initialization script, the lock
            # is acquired by the application itself, so we don't need to do it here. That is why we
            # have the need_lock attribute, and why it defaults to True.
            lock_id = (
                LOCK_ID_DB_INIT
                if context.config.attributes.get("need_lock", True)
                else None
            )
            with pg_advisory_lock(connection, lock_id):
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
