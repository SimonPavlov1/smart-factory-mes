"""Prepare the production database before the API process starts.

An existing versioned database is upgraded with Alembic. A database created by
an older application release without ``alembic_version`` is first reconciled by
the application's additive compatibility bootstrap and then stamped at the
current revision. No tables or application data are dropped here.
"""

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.database import engine


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("smart-factory-prestart")


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    # env.py reads DATABASE_URL and safely overrides the development SQLite URL.
    return config


def prepare_database() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    config = _alembic_config()

    if "alembic_version" in table_names:
        logger.info("Applying pending database migrations")
        command.upgrade(config, "head")
        logger.info("Database migrations are up to date")
        return

    if table_names:
        logger.warning(
            "Legacy database without alembic_version detected; "
            "running additive schema compatibility bootstrap"
        )
    else:
        logger.info("Empty database detected; creating the current schema")

    # Importing app.main runs the existing additive compatibility bootstrap:
    # create_all plus guarded ADD COLUMN statements. It never drops data.
    import app.main  # noqa: F401

    logger.info("Recording the current schema revision")
    command.stamp(config, "head")
    logger.info("Database preparation completed")


if __name__ == "__main__":
    prepare_database()
