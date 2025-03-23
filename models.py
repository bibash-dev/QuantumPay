from sqlalchemy import Column, String, Float, DateTime
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import uuid

# Define the base class for declarative models
Base = declarative_base()


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        unique=True,
        nullable=False,
    )
    gateway = Column(String, nullable=False)
    fee = Column(Float, nullable=False)
    latency = Column(Float, nullable=False)
    timestamp = Column(DateTime(timezone=False), server_default=func.now())
