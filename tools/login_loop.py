import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE_DIR = Path(
    os.getenv(
        "ALLOCATE_HOME",
        str(Path.home() / "allocate-monitor"),
    )
)

BASE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")
load_dotenv()

ALLOCATE_TENANT = os.getenv("ALLOCATE_TENANT", "").strip()

if not ALLOCATE_TENANT:
    raise SystemExit(
        "ALLOCATE_TENANT is missing. Copy .env.example to .env "
        "and set it before running this script."
    )

LOGIN_URL = "https://web.loop.allocate-cloud.co.uk/"

SHIFTS_URL = (
    "https://web.loop.allocate-cloud.co.uk/"
    f"EmployeeOnlineHealth/{ALLOCATE_TENANT}/Roster/BankShifts"
)

PROFILE = BASE_DIR / "allocate-profile"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False,
        viewport={"width": 1400, "height": 900},
    )

    page = context.pages[0] if context.pages else context.new_page()

    print("Opening the Loop login page...")
    page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    print("\nLogin page opened.")
    print("Log in through your organisation sign-in as normal.")
    print("Complete MFA if required.")

    input(
        "\nPress ENTER after you have successfully logged into Loop... "
    )

    print("\nNow navigating to Available Bank Duties...")

    page.goto(
        SHIFTS_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.wait_for_timeout(5000)

    print("\nCurrent URL:")
    print(page.url)

    print("\nPage title:")
    print(page.title())

    print("\nCookies stored:", len(context.cookies()))

    print(
        "\nIf you can see Available Bank Duties in the browser, "
        "the login/session setup worked."
    )

    input("\nPress ENTER to close the browser...")

    context.close()
