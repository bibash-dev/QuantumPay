from fastapi import FastAPI, WebSocket
import logging
from fastapi.responses import JSONResponse
from concurrent.futures import ThreadPoolExecutor
from quantum_pay.db_setup import setup_db
from quantum_pay.transact import (
    stripe_charge,
    paypal_charge,
    square_charge,
    route_transaction,
)

app = FastAPI()
logger = logging.getLogger(__name__)

# Call setup_db to create the table
setup_db()


@app.get("/status")
async def status():
    return {"message": "QuantumPay server is running"}


@app.get("/charge")
async def charge():
    gateways = [stripe_charge, paypal_charge, square_charge]
    wins = {"Stripe": 0, "PayPal": 0, "Square": 0}
    total_savings = 0.0
    results_list = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        for i in range(20):
            future_to_gateway = {executor.submit(g): g.__name__ for g in gateways}
            results = [future.result() for future in future_to_gateway]
            winner, savings, transaction_results = route_transaction(results)
            wins[winner] += 1
            total_savings += savings
            results_list.append(
                {
                    "transaction": i + 1,
                    "winner": winner,
                    "savings": savings,
                    "details": transaction_results,
                }
            )
            # Broadcast to WebSocket clients
            await broadcast(
                {
                    "transaction": i + 1,
                    "winner": winner,
                    "savings": savings,
                    "details": [
                        {r["gateway"]: {"fee": r["fee"], "latency": r["latency"]}}
                        for r in transaction_results
                    ],
                }
            )
    return {
        "summary": {
            "Stripe Wins": wins["Stripe"],
            "PayPal Wins": wins["PayPal"],
            "Square Wins": wins["Square"],
            "Total Savings": total_savings,
        },
        "transactions": results_list,
    }


# WebSocket handling
connected_clients = set()


async def broadcast(message):
    for client in connected_clients:
        await client.send_json(message)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep connection alive
    except Exception as e:
        logger.error("WebSocket error: %s", str(e))
    finally:
        connected_clients.remove(websocket)
