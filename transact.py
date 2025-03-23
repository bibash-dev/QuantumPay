import stripe
import paypalrestsdk
import config
import logging
import random
import time
import sqlite3
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
        return {"fee": fee, "latency": latency}
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
        return {"fee": fee, "latency": latency}
    else:
        logger.error("PayPal error: %s", payment.error)
        raise


def route_transaction(stripe_data, paypal_data):
    qp = QuadraticProgram()
    qp.binary_var("s")  # 1 if Stripe, 0 if PayPal
    qp.binary_var("p")  # 1 if PayPal, 0 if Stripe
    qp.minimize(
        linear={
            "s": stripe_data["fee"] + 0.001 * stripe_data["latency"],
            "p": paypal_data["fee"] + 0.001 * paypal_data["latency"],
        }
    )
    qp.linear_constraint({"s": 1, "p": 1}, "==", 1)  # Only one gateway
    optimizer = MinimumEigenOptimizer(NumPyMinimumEigensolver())
    result = optimizer.solve(qp)
    winner = "Stripe" if result.x[0] == 1 else "PayPal"
    savings = max(stripe_data["fee"], paypal_data["fee"]) - min(
        stripe_data["fee"], paypal_data["fee"]
    )
    logger.info(
        "Routing decision: %s (Stripe: Fee=$%.2f, Lat=%.1fms; PayPal: Fee=$%.2f, Lat=%.1fms), Savings=$%.2f",
        winner,
        stripe_data["fee"],
        stripe_data["latency"],
        paypal_data["fee"],
        paypal_data["latency"],
        savings,
    )
    return winner, savings


if __name__ == "__main__":
    stripe_wins = 0
    paypal_wins = 0
    total_savings = 0.0
    for i in range(5):
        print(f"\nTransaction #{i+1}:")
        stripe_result = stripe_charge()
        paypal_result = paypal_charge()
        winner, savings = route_transaction(stripe_result, paypal_result)
        if winner == "Stripe":
            stripe_wins += 1
        else:
            paypal_wins += 1
        total_savings += savings
        print(
            f'Winner: {winner} (Stripe: Fee=${stripe_result["fee"]:.2f}, Lat={stripe_result["latency"]:.1f}ms; '
            f'PayPal: Fee=${paypal_result["fee"]:.2f}, Lat={paypal_result["latency"]:.1f}ms)'
        )
    print(
        f"\nSummary: Stripe Wins={stripe_wins}, PayPal Wins={paypal_wins}, Total Savings=${total_savings:.2f}"
    )
