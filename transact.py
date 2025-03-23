import stripe
import paypalrestsdk
from square.client import Client
import config
import logging
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from qiskit_optimization import QuadraticProgram
from qiskit_algorithms import NumPyMinimumEigensolver
from qiskit_optimization.algorithms import MinimumEigenOptimizer

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
    environment='sandbox'  # Use 'production' for live transactions
)


def save_to_db(gateway, fee, latency):
    conn = sqlite3.connect("quantum_pay.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO transactions (gateway, fee, latency) VALUES (?, ?, ?)",
        (gateway, fee, latency),
    )
    conn.commit()
    logger.info("Saved to DB: %s, Fee=$%.2f, Latency=%.1fms", gateway, fee, latency)
    conn.close()


def stripe_charge():
    try:
        start = time.time()
        charge = stripe.Charge.create(
            amount=1000,
            currency="usd",
            source="tok_visa",
            description="QuantumPay MVP Test",
        )
        latency = (time.time() - start) * 1000  # convert to ms
        fee = (charge.amount * 0.029 + 30) / 100
        logger.info(
            "Stripe charge succeeded: ID=%s, Fee=$%.2f, Latency=%.1fms",
            charge.id,
            fee,
            latency,
        )
        save_to_db("Stripe", fee, latency)
        return {"gateway": "Stripe", "fee": fee, "latency": latency}
    except stripe.error.StripeError as e:
        logger.error("Stripe error: %s", str(e))
        raise


def paypal_charge():
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
        fee = float(payment.transactions[0].amount.total) * 0.0299 + 0.49
        logger.info(
            "PayPal payment created: ID=%s, Fee=$%.2f, Latency=%.1fms",
            payment.id,
            fee,
            latency,
        )
        save_to_db("PayPal", fee, latency)
        return {"gateway": "PayPal", "fee": fee, "latency": latency}
    else:
        logger.error("PayPal error: %s", payment.error)
        raise


def square_charge():
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
            fee = (1000 * 0.026 + 10) / 100  # 2.6% + $0.10 (Square online rate)
            logger.info(
                "Square payment succeeded: ID=%s, Fee=$%.2f, Latency=%.1fms",
                result.body["payment"]["id"],
                fee,
                latency,
            )
            save_to_db("Square", fee, latency)
            return {"gateway": "Square", "fee": fee, "latency": latency}
        else:
            logger.error("Square error: %s", result.errors)
            raise Exception(result.errors)
    except Exception as e:
        logger.error("Square error: %s", str(e))
        raise


def route_transaction(results):
    qp = QuadraticProgram()

    # Create binary variables with unique names
    for r in results:
        qp.binary_var(r["gateway"].lower())  # Use full gateway name as variable name

    # Define costs for each gateway
    costs = {r["gateway"].lower(): r["fee"] + 0.001 * r["latency"] for r in results}
    qp.minimize(linear=costs)

    # Add constraint: only one gateway can be selected
    qp.linear_constraint({r["gateway"].lower(): 1 for r in results}, "==", 1)

    # Solve the optimization problem
    optimizer = MinimumEigenOptimizer(NumPyMinimumEigensolver())
    result = optimizer.solve(qp)

    # Determine the winning gateway
    winner_idx = [i for i, v in enumerate(result.x) if v == 1][0]
    winner = results[winner_idx]["gateway"]

    # Calculate savings
    savings = max(r["fee"] for r in results) - min(r["fee"] for r in results)

    # Log the routing decision
    log_msg = (
            "Routing decision: %s ("
            + ", ".join(
        [
            "%s: Fee=$%.2f, Lat=%.1fms" % (r["gateway"], r["fee"], r["latency"])
            for r in results
        ]
    )
            + "), Savings=$%.2f"
    )
    logger.info(log_msg, winner, savings)

    return winner, savings


if __name__ == '__main__':
    gateways = [stripe_charge, paypal_charge, square_charge]
    wins = {'Stripe': 0, 'PayPal': 0, 'Square': 0}
    total_savings = 0.0
    for i in range(5):
        print(f'\nTransaction #{i+1}:')
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_gateway = {executor.submit(g): g.__name__ for g in gateways}
            results = [future.result() for future in future_to_gateway]
        winner, savings = route_transaction(results)
        wins[winner] += 1
        total_savings += savings
        print(f'Winner: {winner} (' + ', '.join([f'{r["gateway"]}: Fee=${r["fee"]:.2f}, Lat={r["latency"]:.1f}ms' for r in results]) + ')')
    print(f'\nSummary: Stripe Wins={wins["Stripe"]}, PayPal Wins={wins["PayPal"]}, Square Wins={wins["Square"]}, Total Savings=${total_savings:.2f}')
