from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sila.core.config import settings

# asyncpg driver; pool size tuned for a single-server POC
engine = create_async_engine(
    str(settings.database_url),
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    # begin() wraps the whole request in one transaction: auto-commit on success,
    # auto-rollback on exception. Endpoints never need to call db.commit().
    async with AsyncSessionLocal.begin() as session:
        yield session
