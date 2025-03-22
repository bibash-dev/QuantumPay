import stripe
import paypalrestsdk
import config
import logging
import random

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


def stripe_charge():
    try:
        latency = random.uniform(150, 300)  # Mock latency in ms
        charge = stripe.Charge.create(
            amount=1000,  # $10 in cents
            currency="usd",
            source="tok_visa",
            description="QuantumPay MVP Test",
        )
        fee = (charge.amount * 0.029 + 30) / 100  # 2.9% + $0.30
        logger.info(
            "Stripe charge succeeded: ID=%s, Fee=$%.2f, Latency=%.1fms",
            charge.id,
            fee,
            latency,
        )
        return {"fee": fee, "latency": latency}
    except stripe.error.StripeError as e:
        logger.error("Stripe error: %s", str(e))
        raise


def paypal_charge():
    latency = random.uniform(150, 300)  # Mock latency in ms
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
        fee = (
            float(payment.transactions[0].amount.total) * 0.0299 + 0.49
        )  # 2.99% + $0.49
        logger.info(
            "PayPal payment created: ID=%s, Fee=$%.2f, Latency=%.1fms",
            payment.id,
            fee,
            latency,
        )
        return {"fee": fee, "latency": latency}
    else:
        logger.error("PayPal error: %s", payment.error)
        raise


if __name__ == "__main__":
    stripe_result = stripe_charge()
    paypal_result = paypal_charge()
    print(
        f'Stripe: Fee=${stripe_result["fee"]:.2f}, '
        f'Latency={stripe_result["latency"]:.1f}ms'
    )
    print(
        f'PayPal: Fee=${paypal_result["fee"]:.2f}, '
        f'Latency={paypal_result["latency"]:.1f}ms'
    )
