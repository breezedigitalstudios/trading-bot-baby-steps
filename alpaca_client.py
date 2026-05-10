import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)


def get_account():
    return client.get_account()


if __name__ == "__main__":
    account = get_account()
    print(f"Account status : {account.status}")
    print(f"Buying power   : ${float(account.buying_power):,.2f}")
    print(f"Portfolio value: ${float(account.portfolio_value):,.2f}")
    print(f"Cash           : ${float(account.cash):,.2f}")
