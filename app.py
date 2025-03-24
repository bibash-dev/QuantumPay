import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, init_db
from models import Transaction
from websocket import broadcast
from forecast import FeeForecaster
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


fee_forecaster = FeeForecaster()


@app.get("/forecast")
async def get_fee_forecast(
    request: Request,
    period: str = "3month",
    db: AsyncSession = Depends(get_db),
):
    if period not in {"1month", "3month", "6month"}:
        raise HTTPException(
            status_code=400, detail="Invalid period. Use 1month, 3month, or 6month."
        )

    # Get Redis client from app state
    redis_client = request.app.state.redis

    # Try to get cached forecast first
    cache_key = f"forecast:{period}"
    cached_data = await redis_client.get(cache_key)
    if cached_data:
        logger.info(f"Returning cached forecast for {period}")
        return json.loads(cached_data)

    # Generate fresh forecast if not cached
    historical_data, earliest_timestamp = await fee_forecaster.fetch_historical_data(db)
    prepared_data = fee_forecaster.prepare_data_for_regression(historical_data)
    fee_forecaster.train_models(prepared_data)

    forecast_data = {
        "labels": fee_forecaster.get_forecast_labels(earliest_timestamp, period),
        "Stripe": fee_forecaster.predict("Stripe", earliest_timestamp, period),
        "PayPal": fee_forecaster.predict("PayPal", earliest_timestamp, period),
        "Square": fee_forecaster.predict("Square", earliest_timestamp, period),
    }

    # Cache the new forecast
    await redis_client.setex(cache_key, 3600, json.dumps(forecast_data))
    logger.info(f"Generated and cached new forecast: {period}")

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

    # Count the existing number of transaction groups (each group has 3 gateway calls)
    query = select(Transaction).order_by(Transaction.timestamp.desc())
    result = await db.execute(query)
    transactions = result.scalars().all()
    existing_transaction_count = (
        len(transactions) // 3
    )  # Each transaction group has 3 entries

    # await db.execute(delete(Transaction))
    # await db.commit()
    # await broadcast({"reset": True})

    logger.info("Starting all 60 gateway calls...")
    tasks = [g() for g in gateways for _ in range(20)]
    all_results = await asyncio.gather(*tasks)
    logger.info("All gateway calls completed.")

    for i in range(20):
        results = [all_results[i], all_results[i + 20], all_results[i + 40]]
        winner, savings, transaction_results = route_transaction(results, wins)
        wins[winner] += 1
        total_savings += savings

        for r in transaction_results:
            await save_to_db(r, db)

        normalized_winner = winner.capitalize()
        if normalized_winner == "Paypal":
            normalized_winner = "PayPal"

        # Increment the transaction number based on existing count
        transaction_number = existing_transaction_count + i + 1

        result_data = {
            "transaction": transaction_number,
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
    query = select(Transaction).order_by(Transaction.timestamp.desc())
    result = await db.execute(query)
    transactions = result.scalars().all()

    wins = {"stripe": 0, "paypal": 0, "square": 0}
    total_savings = 0.0
    results_list = []

    for i in range(0, len(transactions), 3):
        group = transactions[i : i + 3]
        if len(group) != 3:
            continue

        winner, savings, transaction_results = route_transaction(group, wins)
        wins[winner] += 1
        total_savings += savings

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


async def broadcast(message: dict):
    if not connected_clients:
        return

    logger.info(
        "Broadcasting message to %d clients: %s", len(connected_clients), message
    )

    for client in list(
        connected_clients
    ):  # Create a copy to avoid modification during iteration
        try:
            await client.send_json(message)
        except (RuntimeError, ConnectionError) as e:
            logger.warning("Client disconnected during broadcast: %s", str(e))
            connected_clients.discard(client)
        except Exception as e:
            logger.error(
                "Unexpected error broadcasting to client: %s", str(e), exc_info=True
            )
            connected_clients.discard(client)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    client_ip = websocket.client.host if websocket.client else "unknown"
    logger.info("WebSocket client connected from %s", client_ip)

    try:
        while True:
            try:
                # Wait for message with timeout for heartbeat
                data = await asyncio.wait_for(websocket.receive_json(), timeout=20.0)

                if isinstance(data, dict) and data.get("type") == "pong":
                    logger.debug("Received pong from client %s", client_ip)
                    continue

                logger.info("Received message from %s: %s", client_ip, data)

            except asyncio.TimeoutError:
                # Send heartbeat ping
                try:
                    await websocket.send_json({"type": "ping"})
                    logger.debug("Sent ping to client %s", client_ip)
                except Exception as e:
                    raise ConnectionError("Failed to send ping") from e

            except (WebSocketDisconnect, ConnectionError):
                logger.info("Client %s disconnected", client_ip)
                break

            except json.JSONDecodeError:
                logger.warning("Invalid JSON received from client %s", client_ip)
                await websocket.send_json({"error": "Invalid JSON format"})

            except Exception as e:
                logger.error(
                    "Unexpected error with client %s: %s",
                    client_ip,
                    str(e),
                    exc_info=True,
                )
                break

    except Exception as e:
        logger.error(
            "WebSocket connection error with %s: %s", client_ip, str(e), exc_info=True
        )
    finally:
        connected_clients.discard(websocket)
        logger.info("WebSocket client %s disconnected", client_ip)
        try:
            await websocket.close()
        except Exception:
            pass  # Connection already closed
