import logging
import numpy as np
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from sklearn.linear_model import LinearRegression
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import Transaction

logger = logging.getLogger(__name__)


class FeeForecaster:
    def __init__(self):
        self.transaction_amount = 10.0
        self.models = {
            "Stripe": LinearRegression(),
            "PayPal": LinearRegression(),
            "Square": LinearRegression(),
        }
        self.is_trained = {
            "Stripe": False,
            "PayPal": False,
            "Square": False,
        }
        self.historical_data = None

    async def fetch_historical_data(self, db: AsyncSession):
        logger.info("Fetching historical transaction data for fee forecasting...")

        try:
            result = await db.execute(
                select(Transaction).order_by(Transaction.timestamp)
            )
            transactions = result.scalars().all()
        except Exception as e:
            logger.error("Error fetching transactions from database: %s", str(e))
            raise

        if not transactions:
            logger.warning("No transactions found in the database")
            return {"Stripe": [], "PayPal": [], "Square": []}, None

        earliest_timestamp = min(tx.timestamp for tx in transactions)
        historical_data = {"Stripe": [], "PayPal": [], "Square": []}

        for tx in transactions:
            try:
                effective_fee_percent = (tx.fee / self.transaction_amount) * 100
                time_delta = (tx.timestamp - earliest_timestamp).total_seconds() / 86400
                historical_data[tx.gateway].append((time_delta, effective_fee_percent))
            except Exception as e:
                logger.error(
                    "Error processing transaction for gateway %s: %s",
                    tx.gateway,
                    str(e),
                )
                continue

        for gateway, data in historical_data.items():
            logger.info(f"Retrieved {len(data)} transactions for {gateway}")

        self.historical_data = historical_data
        return historical_data, earliest_timestamp

    def prepare_data_for_regression(self, historical_data):
        prepared_data = {}

        for gateway, data in historical_data.items():
            if len(data) < 2:
                logger.warning(
                    f"Not enough data for {gateway} to train linear regression model"
                )
                prepared_data[gateway] = {"X": [], "y": []}
                continue

            time_deltas, fee_percents = zip(*data)
            num_transactions = len(time_deltas)

            if time_deltas:
                max_time = max(time_deltas)
                scaled_times = (
                    [(t / max_time) * (num_transactions - 1) for t in time_deltas]
                    if max_time > 0
                    else [0.0] * len(time_deltas)
                )
            else:
                scaled_times = []

            prepared_data[gateway] = {
                "X": np.array(scaled_times).reshape(-1, 1),
                "y": np.array(fee_percents),
            }

        return prepared_data

    def train_models(self, prepared_data):
        for gateway, data in prepared_data.items():
            X, y = data["X"], data["y"]

            if len(X) < 2:
                self.is_trained[gateway] = False
                continue

            self.models[gateway].fit(X, y)
            self.is_trained[gateway] = True
            logger.info(
                f"Trained model for {gateway} with R² score: {self.models[gateway].score(X, y):.4f}"
            )

    def predict(self, gateway: str, earliest_timestamp: datetime, period: str):
        if not self.is_trained[gateway]:
            logger.warning(
                f"Model for {gateway} is not trained, returning default fees"
            )
            default_fee_percent = {"Stripe": 5.9, "PayPal": 7.9, "Square": 3.6}[gateway]
            steps = {"1month": 1, "3month": 3, "6month": 6}[period]
            return [default_fee_percent] * steps

        steps = {"1month": 1, "3month": 3, "6month": 6}[period]
        future_times = np.array([i * 30 for i in range(steps)]).reshape(-1, 1)
        predicted_fees = [
            max(0, fee) for fee in self.models[gateway].predict(future_times)
        ]

        return predicted_fees

    def get_forecast_labels(self, earliest_timestamp: datetime, period: str):
        steps = {"1month": 1, "3month": 3, "6month": 6}[period]
        forecast_start = earliest_timestamp.replace(day=1)

        return [
            (forecast_start + relativedelta(months=i)).strftime("%B")
            for i in range(steps)
        ]
