import random
import time
from datetime import datetime, timedelta
import os

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


# ============================================================
# IMPORT OUR TESTED MONITOR LOGIC
# ============================================================

from monitor_booking_once import (
    BASE_DIR,
    PROFILE,
    LOGIN_URL,
    SHIFTS_URL,
    WEEKS_TO_SCAN,
    WEEKLY_LIMIT,
    TARGET_UNIT,
    TARGET_GRADE,
    WEEKDAY_START_EARLIEST,
    WEEKDAY_START_LATEST,
    WEEKEND_WINDOW_START,
    WEEKEND_WINDOW_END,
    NIGHT_START_EARLIEST,
    HEADLESS,
    SessionExpired,
    check_config,
    initialise_database,
    prune_old_shifts,
    scan_available_shifts,
    process_new_shifts,
)


# ============================================================
# LIVE MONITOR SETTINGS
# ============================================================

POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "600")
)

JITTER_SECONDS = int(
    os.getenv("JITTER_SECONDS", "60")
)

# When the Allocate session expires we cannot fix it
# ourselves. If the browser is visible you can log in in the
# window that is already open, and the monitor will pick the
# session back up on its next attempt.

RECOVERY_SECONDS = 300      # 5 minutes between retries
MAX_SESSION_RETRIES = 6     # give up after ~30 minutes

# Set ALLOCATE_HEADLESS=true on a server with no display.
# Imported from monitor_booking_once so both scripts agree.


# ============================================================
# TELEGRAM NOTIFICATIONS
# ============================================================

load_dotenv(
    BASE_DIR / ".env"
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)


def send_notice(heading, message):
    """
    Send an operational notice about the monitor itself.

    This is separate from shift alerts, which are built and
    sent inside monitor_booking_once.py.
    """

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:

        print(
            "Telegram credentials are not available."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    text = (
        f"{heading}\n\n"
        f"{message}"
    )

    try:

        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=15,
        )

        response.raise_for_status()

        result = response.json()

        return bool(
            result.get("ok")
        )

    except Exception as exc:

        print(
            "Failed to send notification:",
            exc
        )

        return False


def send_error_notice(message):

    return send_notice(
        "🚨 SHIFT MONITOR",
        message,
    )


def send_session_notice(message):

    return send_notice(
        "🔐 ALLOCATE SESSION EXPIRED",
        message,
    )


# ============================================================
# SLEEP THAT CAN BE INTERRUPTED CLEANLY
# ============================================================

def wait_for(seconds, reason):
    """
    Sleep, printing when the next attempt is due.

    Returns False if the user pressed Ctrl+C.
    """

    next_time = (
        datetime.now()
        + timedelta(seconds=seconds)
    )

    print(
        "\n" + "-" * 70
    )

    print(
        f"{reason} in "
        f"{seconds // 60}m {seconds % 60}s"
    )

    print(
        "Approximately:",
        next_time.strftime("%H:%M:%S")
    )

    print(
        "-" * 70
    )

    try:

        time.sleep(seconds)

        return True

    except KeyboardInterrupt:

        return False


# ============================================================
# MAIN LIVE MONITOR
# ============================================================

def main():

    print("=" * 70)
    print("BANK SHIFT LIVE MONITOR")
    print("=" * 70)

    print(f"Target: {TARGET_UNIT}")
    print(f"Grade: {TARGET_GRADE}")

    print(
        "Weekday shifts: starting "
        f"{WEEKDAY_START_EARLIEST[0]:02d}:"
        f"{WEEKDAY_START_EARLIEST[1]:02d}–"
        f"{WEEKDAY_START_LATEST[0]:02d}:"
        f"{WEEKDAY_START_LATEST[1]:02d} "
        "(any finish)"
    )

    print(
        "Weekend shifts: anything within "
        f"{WEEKEND_WINDOW_START[0]:02d}:"
        f"{WEEKEND_WINDOW_START[1]:02d}–"
        f"{WEEKEND_WINDOW_END[0]:02d}:"
        f"{WEEKEND_WINDOW_END[1]:02d}"
    )

    print(
        "Night shifts: Fri/Sat/Sun starts from "
        f"{NIGHT_START_EARLIEST[0]:02d}:"
        f"{NIGHT_START_EARLIEST[1]:02d} onward"
    )

    print(
        f"Weekly limit: {WEEKLY_LIMIT} bank "
        "shifts per Monday–Sunday week"
    )

    print(f"Weeks scanned: {WEEKS_TO_SCAN}")

    print(
        f"Polling: every {POLL_SECONDS}s "
        f"+ random 0–{JITTER_SECONDS}s"
    )

    print("Automatic booking: DISABLED")

    print(
        "Browser:",
        "headless"
        if HEADLESS
        else "visible (persistent session)"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # Open the database once and reuse it.
    # --------------------------------------------------------

    check_config()

    connection = initialise_database()

    # --------------------------------------------------------
    # Alert state.
    #
    # error_alert_sent stops us sending the same generic error
    # every ten minutes.
    #
    # session_retries counts consecutive expiry detections so
    # we eventually stop rather than looping forever.
    # --------------------------------------------------------

    error_alert_sent = False
    session_alert_sent = False
    session_retries = 0

    stop_reason = "stopped by user"

    with sync_playwright() as p:

        print(
            "\nOpening persistent Allocate browser..."
        )

        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=HEADLESS,
            viewport={
                "width": 1400,
                "height": 900,
            },
        )

        page = (
            context.pages[0]
            if context.pages
            else context.new_page()
        )

        # ----------------------------------------------------
        # Establish the saved Allocate session.
        # ----------------------------------------------------

        try:

            page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.wait_for_timeout(1500)

            print(
                "Saved Allocate session loaded."
            )

        except Exception as exc:

            print(
                "\nCould not open Allocate:"
            )

            print(repr(exc))

            send_error_notice(
                "Could not open the saved "
                f"Allocate session.\n\n{exc}"
            )

        # ----------------------------------------------------
        # Continuous monitoring loop.
        # ----------------------------------------------------

        while True:

            started = datetime.now()

            print("\n\n" + "=" * 70)
            print("MONITOR PASS START")
            print(
                started.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )
            print("=" * 70)

            try:

                # ------------------------------------------------
                # Drop shifts whose date has passed. They cannot
                # be booked, so the rows serve no purpose.
                # ------------------------------------------------

                prune_old_shifts(connection)

                matches = scan_available_shifts(
                    page
                )

                print(
                    "\nTarget shifts currently "
                    "available:",
                    len(matches)
                )

                alerts_sent = process_new_shifts(
                    connection,
                    page,
                    matches,
                )

                print("\n" + "=" * 70)
                print("PASS COMPLETE")
                print("=" * 70)

                print(
                    "Alerts sent:",
                    alerts_sent
                )

                # ------------------------------------------------
                # A successful pass clears every error state.
                # ------------------------------------------------

                if session_alert_sent:

                    send_session_notice(
                        "The Allocate session is "
                        "working again.\n\n"
                        "Monitoring has resumed."
                    )

                    print(
                        "📱 Session recovery "
                        "notification sent."
                    )

                error_alert_sent = False
                session_alert_sent = False
                session_retries = 0

                delay = (
                    POLL_SECONDS
                    + random.randint(
                        0,
                        JITTER_SECONDS,
                    )
                )

                if not wait_for(
                    delay,
                    "Next check",
                ):

                    print(
                        "\nStopping monitor..."
                    )

                    break

                continue

            # ------------------------------------------------
            # SESSION EXPIRY.
            #
            # This is not a transient fault. Retrying at the
            # normal interval achieves nothing, so we ask for
            # help and back off.
            # ------------------------------------------------

            except SessionExpired as exc:

                session_retries += 1

                print("\n" + "=" * 70)
                print("🔐 ALLOCATE SESSION EXPIRED")
                print("=" * 70)

                print(exc)

                print(
                    "\nAttempt "
                    f"{session_retries} of "
                    f"{MAX_SESSION_RETRIES}"
                )

                if not session_alert_sent:

                    if HEADLESS:

                        instruction = (
                            "Log in on the monitor "
                            "machine using "
                            "login_loop.py, then "
                            "restart the monitor."
                        )

                    else:

                        instruction = (
                            "A browser window is "
                            "already open on the "
                            "monitor machine. Log "
                            "into Allocate in that "
                            "window and monitoring "
                            "will resume "
                            "automatically."
                        )

                    if send_session_notice(
                        "The saved Allocate session "
                        "is no longer valid.\n\n"
                        f"{instruction}\n\n"
                        "Shift alerts are paused "
                        "until this is fixed."
                    ):

                        print(
                            "📱 Session notification "
                            "sent."
                        )

                    session_alert_sent = True

                if session_retries >= (
                    MAX_SESSION_RETRIES
                ):

                    print(
                        "\nGiving up after "
                        f"{MAX_SESSION_RETRIES} "
                        "attempts."
                    )

                    send_session_notice(
                        "The session was not "
                        "restored.\n\n"
                        "The monitor has stopped "
                        "and will send no further "
                        "shift alerts until it is "
                        "restarted."
                    )

                    stop_reason = (
                        "session could not be restored"
                    )

                    break

                if not wait_for(
                    RECOVERY_SECONDS,
                    "Retrying session",
                ):

                    print(
                        "\nStopping monitor..."
                    )

                    break

                continue

            # ------------------------------------------------
            # ANY OTHER FAILURE.
            #
            # Probably transient, so retry on the normal
            # schedule but only alert once.
            # ------------------------------------------------

            except Exception as exc:

                print("\n" + "=" * 70)
                print("❌ MONITOR PASS FAILED")
                print("=" * 70)

                print(repr(exc))

                if not error_alert_sent:

                    if send_error_notice(
                        "The shift monitor "
                        "encountered an error.\n\n"
                        f"{type(exc).__name__}: "
                        f"{exc}\n\n"
                        "The monitor will retry "
                        "automatically."
                    ):

                        print(
                            "📱 Error notification sent."
                        )

                    error_alert_sent = True

                delay = (
                    POLL_SECONDS
                    + random.randint(
                        0,
                        JITTER_SECONDS,
                    )
                )

                if not wait_for(
                    delay,
                    "Retrying",
                ):

                    print(
                        "\nStopping monitor..."
                    )

                    break

                continue

        # --------------------------------------------------------
        # Clean shutdown.
        # --------------------------------------------------------

        print(
            "\nClosing Allocate browser..."
        )

        context.close()

    connection.close()

    print(
        f"Monitor stopped ({stop_reason})."
    )


if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nMonitor interrupted."
        )
