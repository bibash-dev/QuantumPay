from dotenv import load_dotenv
import os

load_dotenv()

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET")
SQUARE_ACCESS_TOKEN= os.getenv("SQUARE_ACCESS_TOKEN")