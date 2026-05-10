import os
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY = os.getenv("CLAUDE_API_KEY")
if not API_KEY:
    raise RuntimeError("Set CLAUDE_API_KEY in .env or in your environment.")

client = Anthropic(api_key=API_KEY)


def ask_claude(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


if __name__ == "__main__":
    question = "Hello Claude, please summarize trading bot risk considerations."
    print(ask_claude(question))
