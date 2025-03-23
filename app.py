from fastapi import FastAPI, WebSocket, Depends, HTTPException
from contextlib import asynccontextmanager
import logging
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from quantum_pay.transact import stripe_charge, paypal_charge, square_charge, route_transaction
from quantum_pay.models import Transaction
from quantum_pay.schemas import TransactionCreate, TransactionOut
from quantum_pay.database import get_db, init_db
import asyncio

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.mount('/static', StaticFiles(directory='quantum_pay/static'), name='static')
logger = logging.getLogger(__name__)

async def save_to_db(transaction: TransactionCreate, db: AsyncSession = Depends(get_db)) -> TransactionOut:
    try:
        transaction_data = transaction.model_dump()
        db_transaction = Transaction(**transaction_data)
        db.add(db_transaction)
        await db.commit()
        await db.refresh(db_transaction)
        logger.info("Saved to DB: %s, Fee=$%.2f, Latency=%.1fms", transaction.gateway, transaction.fee, transaction.latency)
        return TransactionOut.from_orm(db_transaction)
    except Exception as e:
        logger.error("Error saving transaction to DB: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Failed to save transaction to database: {str(e)}")

@app.get("/charge")
async def charge(db: AsyncSession = Depends(get_db)):
    gateways = [stripe_charge, paypal_charge, square_charge]
    wins = {"stripe": 0, "paypal": 0, "square": 0}
    total_savings = 0.0
    results_list = []

    # Broadcast reset signal
    await broadcast({"reset": True})

    logger.info("Starting all 60 gateway calls...")
    tasks = [g() for g in gateways for _ in range(20)]
    all_results = await asyncio.gather(*tasks)
    logger.info("All gateway calls completed.")

    for i in range(0, len(all_results), 3):
        results = all_results[i:i+3]
        winner, savings, transaction_results = route_transaction(results)
        wins[winner] += 1
        total_savings += savings

        for r in transaction_results:
            await save_to_db(r, db)

        result_data = {
            "transaction": i // 3 + 1,
            "winner": winner.capitalize(),
            "savings": savings,
            "details": [{"gateway": r.gateway, "fee": r.fee, "latency": r.latency} for r in transaction_results],
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
    from sqlalchemy import select
    # Fetch the last 60 transactions (20 sets of 3)
    query = select(Transaction).order_by(Transaction.timestamp.desc()).limit(60)
    result = await db.execute(query)
    transactions = result.scalars().all()

    wins = {"Stripe": 0, "PayPal": 0, "Square": 0}
    total_savings = 0.0
    results_list = []

    # Group transactions into sets of 3 (Stripe, PayPal, Square per transaction)
    for i in range(0, len(transactions), 3):
        group = transactions[i:i+3]
        if len(group) != 3:  # Skip incomplete groups
            continue

        # Calculate winner and savings
        winner = min(group, key=lambda x: x.fee + 0.001 * x.latency).gateway
        savings = max(t.fee for t in group) - min(t.fee for t in group)
        wins[winner] += 1
        total_savings += savings

        result_data = {
            "transaction": i // 3 + 1,
            "winner": winner,
            "savings": savings,
            "details": [{"gateway": t.gateway, "fee": t.fee, "latency": t.latency} for t in group],
        }
        results_list.append(result_data)

    return {
        "summary": {
            "Stripe Wins": wins["Stripe"],
            "PayPal Wins": wins["PayPal"],
            "Square Wins": wins["Square"],
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