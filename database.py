import logging

import config
from models import Base
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database URL
DATABASE_URL = config.DB_URL

# Create an async engine
engine = create_async_engine(DATABASE_URL, echo=True)

# Create a sessionmaker for async sessions
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    try:
        async with engine.begin() as conn:
            logger.info("Creating database tables...")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created successfully.")
    except SQLAlchemyError as e:
        logger.error(f"Error initializing database: {e}")
        raise


async def get_db():
    """
    Dependency to get an async database session.
    Yields the session and ensures it’s closed after use.
    """
    session = async_session()
    try:
        yield session
    except SQLAlchemyError as e:
        logger.error(f"Database error: {e}")
        await session.rollback()
        raise
    finally:
        await session.close()
