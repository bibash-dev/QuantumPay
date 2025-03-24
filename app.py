import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, init_db
from models import Transaction
from schemas import TransactionCreate, TransactionOut
from transact import stripe_charge, paypal_charge, square_charge, route_transaction


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    app.state.redis = redis.from_url(redis_url)
    await init_db()
    yield
    await app.state.redis.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
logger = logging.getLogger(__name__)


# Mock fee trends for 3 months (March-May 2025)
FEE_TRENDS = {
    "1month": {"Stripe": [2.9], "PayPal": [2.99], "Square": [2.8], "labels": ["March"]},
    "3month": {
        "Stripe": [2.9, 2.8, 2.85],
        "PayPal": [2.99, 2.95, 2.7],
        "Square": [2.8, 2.75, 2.9],
        "labels": ["March", "April", "May"],
    },
    "6month": {
        "Stripe": [2.9, 2.9, 2.9, 2.8, 2.8, 2.85],
        "PayPal": [2.99, 2.99, 2.99, 2.95, 2.95, 2.7],
        "Square": [2.8, 2.8, 2.8, 2.75, 2.75, 2.9],
        "labels": ["Dec", "Jan", "Feb", "March", "April", "May"],
    },
}


@app.get("/forecast")
async def get_fee_forecast(period: str = "3month"):
    if period not in ["1month", "3month", "6month"]:
        raise HTTPException(
            status_code=400, detail="Invalid period. Use 1month, 3month, or 6month."
        )

    # Check Redis cache
    redis_client = app.state.redis
    cache_key = f"forecast:{period}"
    cached_data = await redis_client.get(cache_key)

    if cached_data:
        logger.info("Serving forecast from Redis cache: %s", period)
        return json.loads(cached_data)

    # Mock data (replace with Linear Regression in production)
    forecast_data = FEE_TRENDS[period]

    # Cache the result in Redis (expire after 1 hour)
    await redis_client.setex(cache_key, 3600, json.dumps(forecast_data))
    logger.info("Generated and cached forecast: %s", period)

    return forecast_data


async def save_to_db(
    transaction: TransactionCreate, db: AsyncSession = Depends(get_db)
) -> TransactionOut:
    try:
        transaction_data = transaction.model_dump()
        db_transaction = Transaction(**transaction_data)
        db.add(db_transaction)
        await db.commit()
        await db.refresh(db_transaction)
        logger.info(
            "Saved to DB: %s, Fee=$%.2f, Latency=%.1fms",
            transaction.gateway,
            transaction.fee,
            transaction.latency,
        )
        return TransactionOut.from_orm(db_transaction)
    except Exception as e:
        logger.error("Error saving transaction to DB: %s", str(e))
        raise HTTPException(
            status_code=500, detail=f"Failed to save transaction to database: {str(e)}"
        )


@app.get("/charge")
async def charge(db: AsyncSession = Depends(get_db)):
    gateways = [stripe_charge, paypal_charge, square_charge]
    wins = {"stripe": 0, "paypal": 0, "square": 0}
    total_savings = 0.0
    results_list = []

    # Delete old transactions before saving new ones
    await db.execute(delete(Transaction))
    await db.commit()

    # Broadcast reset signal
    await broadcast({"reset": True})

    logger.info("Starting all 60 gateway calls...")
    tasks = [
        g() for g in gateways for _ in range(20)
    ]  # 20 Stripe, 20 PayPal, 20 Square
    all_results = await asyncio.gather(*tasks)
    logger.info("All gateway calls completed.")

    # Reorganize into groups of [Stripe, PayPal, Square]
    for i in range(20):  # 20 transactions
        # Index: Stripe (0-19), PayPal (20-39), Square (40-59)
        results = [all_results[i], all_results[i + 20], all_results[i + 40]]
        winner, savings, transaction_results = route_transaction(results, wins)
        wins[winner] += 1
        total_savings += savings

        for r in transaction_results:
            await save_to_db(r, db)

        # Normalize winner
        normalized_winner = winner.capitalize()
        if normalized_winner == "Paypal":
            normalized_winner = "PayPal"

        result_data = {
            "transaction": i + 1,
            "winner": normalized_winner,
            "savings": savings,
            "details": [
                {"gateway": r.gateway, "fee": r.fee, "latency": r.latency}
                for r in transaction_results
            ],
        }
        results_list.append(result_data)
        await broadcast(result_data)

    logger.info("Charge route completed successfully.")
    return {
        "summary": {
            "Stripe Wins": wins["stripe"],
            "PayPal Wins": wins["paypal"],
            "Square Wins": wins["square"],
            "Total Savings": total_savings,
        },
        "transactions": results_list,
    }


@app.get("/dashboard-data")
async def get_dashboard_data(db: AsyncSession = Depends(get_db)):
    # Fetch the last 60 transactions (20 sets of 3)
    query = select(Transaction).order_by(Transaction.timestamp.desc()).limit(60)
    result = await db.execute(query)
    transactions = result.scalars().all()

    wins = {"stripe": 0, "paypal": 0, "square": 0}
    total_savings = 0.0
    results_list = []

    # Group transactions into sets of 3 (Stripe, PayPal, Square per transaction)
    for i in range(0, len(transactions), 3):
        group = transactions[i : i + 3]
        if len(group) != 3:  # Skip incomplete groups
            continue

        # Use route_transaction for consistency with /charge
        winner, savings, transaction_results = route_transaction(group, wins)
        wins[winner] += 1
        total_savings += savings

        # Normalize winner case to match frontend
        normalized_winner = winner.capitalize()
        if normalized_winner == "Paypal":
            normalized_winner = "PayPal"

        result_data = {
            "transaction": i // 3 + 1,
            "winner": normalized_winner,
            "savings": savings,
            "details": [
                {"gateway": t.gateway, "fee": t.fee, "latency": t.latency}
                for t in group
            ],
        }
        results_list.append(result_data)

    return {
        "summary": {
            "Stripe Wins": wins["stripe"],
            "PayPal Wins": wins["paypal"],
            "Square Wins": wins["square"],
            "Total Savings": total_savings,
        },
        "transactions": results_list,
    }


# Websocket handling
connected_clients = set()


async def broadcast(message):
    logger.info("Broadcasting: %s", message)
    for client in connected_clients:
        await client.send_json(message)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    logger.info("WebSocket client connected")
    try:
        while True:
            await websocket.receive_text()  # Keep connection alive
    except Exception as e:
        logger.error("WebSocket error: %s", str(e))
    finally:
        connected_clients.remove(websocket)
        logger.info("WebSocket client disconnected")
