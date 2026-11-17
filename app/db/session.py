"""The API's database engine and session factory.

Both are created once, when the app is built, and shared by every request.
Creating them opens no connection: the first query does.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """Create the engine for a Postgres database.

    Args:
        database_url: The database's `postgresql+asyncpg://` URL.

    Returns:
        The engine. Dispose of it when the app shuts down.
    """
    # Statement parameters hold captions and message texts. Hidden, they stay
    # out of SQLAlchemy's error messages, and so out of the logs.
    return create_async_engine(database_url, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the factory that opens one session per unit of work.

    Args:
        engine: The engine the sessions connect through.

    Returns:
        A session factory. Loaded objects stay readable after a commit.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
