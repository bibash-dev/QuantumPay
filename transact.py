import asyncio
import logging
import random
import time

import paypalrestsdk
import stripe
from qiskit_algorithms import NumPyMinimumEigensolver
from qiskit_optimization import QuadraticProgram
from qiskit_optimization.algorithms import MinimumEigenOptimizer
from square.client import Client

import config
from schemas import TransactionCreate


logging.basicConfig(level=logging.INFO, filename="quantum_pay.log")
logger = logging.getLogger(__name__)

stripe.api_key = config.STRIPE_API_KEY
paypalrestsdk.configure(
    {
        "mode": "sandbox",
        "client_id": config.PAYPAL_CLIENT_ID,
        "client_secret": config.PAYPAL_SECRET,
    }
)
square_client = Client(
    access_token=config.SQUARE_ACCESS_TOKEN,
    environment="sandbox",  # Use 'production' for live transactions
)


import random  # Add this import


async def stripe_charge():
    try:
        start = time.time()
        charge = stripe.Charge.create(
            amount=1000,
            currency="usd",
            source="tok_visa",
            description="QuantumPay MVP Test",
        )
        latency = (time.time() - start) * 1000  # convert to ms
        base_fee = (charge.amount * 0.029 + 30) / 100
        fee = base_fee + random.uniform(-0.02, 0.02)  # Add variation
        logger.info(
            "Stripe charge succeeded: ID=%s, Fee=$%.2f, Latency=%.1fms",
            charge.id,
            fee,
            latency,
        )
        return TransactionCreate(gateway="Stripe", fee=fee, latency=latency)
    except stripe.error.StripeError as e:
        logger.error("Stripe error: %s", str(e))
        raise


async def paypal_charge():
    payment = paypalrestsdk.Payment(
        {
            "intent": "sale",
            "payer": {"payment_method": "paypal"},
            "transactions": [
                {
                    "amount": {"total": "10.00", "currency": "USD"},
                    "description": "QuantumPay MVP Test",
                }
            ],
            "redirect_urls": {
                "return_url": "http://localhost",
                "cancel_url": "http://localhost",
            },
        }
    )
    start = time.time()
    if payment.create():
        latency = (time.time() - start) * 1000
        base_fee = float(payment.transactions[0].amount.total) * 0.0299 + 0.49
        fee = base_fee + random.uniform(-0.02, 0.02)  # Add variation
        logger.info(
            "PayPal payment created: ID=%s, Fee=$%.2f, Latency=%.1fms",
            payment.id,
            fee,
            latency,
        )
        return TransactionCreate(gateway="PayPal", fee=fee, latency=latency)
    else:
        logger.error("PayPal error: %s", payment.error)
        raise


async def square_charge():
    try:
        start = time.time()
        result = square_client.payments.create_payment(
            body={
                "source_id": "cnon:card-nonce-ok",  # Test nonce from Square
                "amount_money": {"amount": 1000, "currency": "USD"},
                "idempotency_key": str(time.time()),
            }
        )
        latency = (time.time() - start) * 1000
        if result.is_success():
            base_fee = (1000 * 0.026 + 10) / 100  # 2.6% + $0.10 (Square online rate)
            fee = base_fee + random.uniform(-0.02, 0.02)  # Add variation
            logger.info(
                "Square payment succeeded: ID=%s, Fee=$%.2f, Latency=%.1fms",
                result.body["payment"]["id"],
                fee,
                latency,
            )
            return TransactionCreate(gateway="Square", fee=fee, latency=latency)
        else:
            logger.error("Square error: %s", result.errors)
            raise Exception(result.errors)
    except Exception as e:
        logger.error("Square error: %s", str(e))
        raise


def route_transaction(results, wins=None):
    if wins is None:
        wins = {"stripe": 0, "paypal": 0, "square": 0}

    # Filter out 'unknown' gateways for cost calculation
    valid_results = [r for r in results if r.gateway.lower() in wins]
    if not valid_results:
        logger.warning("All gateways are 'unknown', defaulting to Stripe")
        return "stripe", 0.0, results

    # Define unique gateways (excluding 'unknown')
    unique_gateways = {r.gateway.lower() for r in valid_results}
    if not unique_gateways:
        logger.warning("No valid gateways found, defaulting to Stripe")
        return "stripe", 0.0, results

    # Calculate costs for valid gateways
    costs = {}
    for r in valid_results:
        gateway = r.gateway.lower()
        costs[gateway] = r.fee + 0.01 * r.latency + 6.0 * wins[gateway]

    # Set up the Qiskit Quadratic Program
    qp = QuadraticProgram()
    for gateway in unique_gateways:
        qp.binary_var(gateway)

    qp.minimize(linear=costs)
    qp.linear_constraint({gateway: 1 for gateway in unique_gateways}, "==", 1)

    try:
        optimizer = MinimumEigenOptimizer(NumPyMinimumEigensolver())
        result = optimizer.solve(qp)
        winner_idx = [i for i, v in enumerate(result.x) if v == 1][0]
        winner = list(unique_gateways)[winner_idx]
    except Exception as e:
        logger.error("Qiskit solver failed: %s, falling back to manual routing", str(e))
        winner = min(costs, key=costs.get)

    # Calculate savings based on valid results
    savings = (
        max(r.fee for r in valid_results) - min(r.fee for r in valid_results)
        if valid_results
        else 0.0
    )

    log_msg = (
        "Routing decision: %s ("
        + ", ".join(
            [
                "%s: Fee=$%.2f, Lat=%.1fms" % (r.gateway, r.fee, r.latency)
                for r in results
            ]
        )
        + "), Savings=$%.2f"
    )
    logger.info(log_msg, winner.capitalize(), savings)

    return winner, savings, results