import stripe
import paypalrestsdk
import config

stripe.api_key = config.STRIPE_API_KEY

# Correct the Stripe version attribute
print(f'Stripe SDK version: {stripe._version}')

# PayPal SDK version should work as expected
print(f'PayPal SDK version: {paypalrestsdk.__version__}')
