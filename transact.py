import stripe
import paypalrestsdk
import config
import logging
import random
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
        latency = random.uniform(150, 300)
        charge = stripe.Charge.create(
            amount=1000,
            currency="usd",
            source="tok_visa",
            description="QuantumPay MVP Test",
        )
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
    latency = random.uniform(150, 300)
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
    if payment.create():
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
    logger.info(
        "Routing decision: %s (Stripe: Fee=$%.2f, Lat=%.1fms; PayPal: Fee=$%.2f, Lat=%.1fms)",
        winner,
        stripe_data["fee"],
        stripe_data["latency"],
        paypal_data["fee"],
        paypal_data["latency"],
    )
    return winner


if __name__ == "__main__":
    stripe_result = stripe_charge()
    paypal_result = paypal_charge()
    winner = route_transaction(stripe_result, paypal_result)
    print(
        f'Winner: {winner} (Stripe: Fee=${stripe_result["fee"]:.2f}, Lat={stripe_result["latency"]:.1f}ms; '
        f'PayPal: Fee=${paypal_result["fee"]:.2f}, Lat={paypal_result["latency"]:.1f}ms)'
    )
