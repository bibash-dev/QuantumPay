from uuid import UUID
from pydantic import BaseModel
from datetime import datetime


class TransactionCreate(BaseModel):
    gateway: str
    fee: float
    latency: float


class TransactionOut(BaseModel):
    id: UUID
    gateway: str
    fee: float
    latency: float
    timestamp: datetime

    class Config:
        from_attributes = True
