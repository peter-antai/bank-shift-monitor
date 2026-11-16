import os

import requests
from dotenv import load_dotenv


load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")

if not token:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is missing from .env"
    )


url = (
    f"https://api.telegram.org/"
    f"bot{token}/getUpdates"
)

response = requests.get(
    url,
    timeout=15,
)

response.raise_for_status()

data = response.json()

print("Telegram response received.")

updates = data.get("result", [])

if not updates:
    print(
        "\nNo messages found."
    )

    print(
        "Open your bot in Telegram, press Start, "
        "and send it a message."
    )

else:

    print(
        "\nChats found:"
    )

    seen = set()

    for update in updates:

        message = update.get(
            "message"
        )

        if not message:
            continue

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        name = chat.get(
            "first_name",
            ""
        )

        username = chat.get(
            "username",
            ""
        )

        if chat_id not in seen:

            seen.add(chat_id)

            print(
                f"\nChat ID: {chat_id}"
            )

            print(
                f"Name: {name}"
            )

            print(
                f"Username: @{username}"
            )
