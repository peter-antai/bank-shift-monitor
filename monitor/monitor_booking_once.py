from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import json
import os
import sqlite3

import requests
from dotenv import load_dotenv
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURATION
# ============================================================

# ------------------------------------------------------------
# Where writable state lives.
#
# Defaults to ~/allocate-monitor so the scripts still run
# unchanged on a laptop. In the container ALLOCATE_HOME is
# set to /data, which is a mounted volume.
# ------------------------------------------------------------

BASE_DIR = Path(
    os.getenv(
        "ALLOCATE_HOME",
        str(Path.home() / "allocate-monitor"),
    )
)

BASE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

PROFILE = BASE_DIR / "allocate-profile"
DATABASE = BASE_DIR / "shifts.db"

# Load .env before any setting below is read. The data folder
# is checked first, then the current directory and its parents,
# so a local run and the container both find it.
load_dotenv(BASE_DIR / ".env")
load_dotenv()

# ------------------------------------------------------------
# Organisation-specific settings.
#
# These identify one employer and one unit, so they live in
# .env and never in the repository. See .env.example.
# ------------------------------------------------------------

# The tenant code is the path segment after
# EmployeeOnlineHealth/ in your organisation's Loop URL.
ALLOCATE_TENANT = os.getenv("ALLOCATE_TENANT", "").strip()

LOGIN_URL = "https://web.loop.allocate-cloud.co.uk/"

SHIFTS_URL = (
    "https://web.loop.allocate-cloud.co.uk/"
    f"EmployeeOnlineHealth/{ALLOCATE_TENANT}/Roster/BankShifts"
)

ROSTER_URL = (
    "https://web.loop.allocate-cloud.co.uk/"
    f"EmployeeOnlineHealth/{ALLOCATE_TENANT}/Roster/PersonalRoster"
)

WEEKS_TO_SCAN = int(
    os.getenv("WEEKS_TO_SCAN", "3")
)

# Set ALLOCATE_HEADLESS=true on a server with no display.

HEADLESS = os.getenv(
    "ALLOCATE_HEADLESS",
    "false",
).strip().lower() in ("1", "true", "yes")

# Unit and grade exactly as Loop displays them.
TARGET_UNIT = os.getenv("TARGET_UNIT", "").strip()
TARGET_GRADE = os.getenv("TARGET_GRADE", "").strip()

# Optional wording used in alerts.
SITE_NAME = os.getenv("SITE_NAME", "").strip()

STAFF_BANK_NAME = os.getenv(
    "STAFF_BANK_NAME",
    "Staff Bank",
).strip()

# Every active bank shift counts toward this limit,
# not only the 06:00-14:00 ones.
WEEKLY_LIMIT = int(
    os.getenv("WEEKLY_LIMIT", "4")
)

# Weekday: any shift STARTING in this range, whatever time
# it ends. Units post irregular starts such as 07:15.
WEEKDAY_START_EARLIEST = (6, 0)
WEEKDAY_START_LATEST = (11, 0)

# Weekend: any shift falling entirely inside this window.
WEEKEND_WINDOW_START = (6, 0)
WEEKEND_WINDOW_END = (22, 0)

# Nights: shifts that run past midnight, but only when they
# START on a Friday, Saturday or Sunday.
#
# The start floor stops an odd daytime shift that happens to
# cross midnight from being treated as a night shift.
NIGHT_DAYS = (4, 5, 6)          # Mon=0, so Fri/Sat/Sun
NIGHT_START_EARLIEST = (18, 0)


# ============================================================
# CONFIGURATION CHECK
#
# Without these the monitor would run happily and match
# nothing, which looks exactly like "no shifts available".
# So it stops early and says what is missing.
# ============================================================


def check_config():
    """Exit with a clear message if a required setting is missing."""

    missing = [
        name
        for name, value in (
            ("ALLOCATE_TENANT", ALLOCATE_TENANT),
            ("TARGET_UNIT", TARGET_UNIT),
            ("TARGET_GRADE", TARGET_GRADE),
        )
        if not value
    ]

    if missing:

        raise SystemExit(
            "Missing required setting(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )


# ============================================================
# EXCEPTIONS
#
# A dedicated exception type lets the live monitor tell the
# difference between "Allocate logged me out" (needs a human)
# and "something else broke" (will probably retry fine).
# ============================================================

class SessionExpired(RuntimeError):
    """The saved Allocate session is no longer valid."""


# ============================================================
# TELEGRAM
# ============================================================

load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)


def send_telegram(message):
    """Send a Telegram message with an Open Allocate button."""

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from .env"
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing from .env"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "Open Allocate Loop",
                    "url": SHIFTS_URL,
                }
            ]
        ]
    }

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "reply_markup": json.dumps(
                keyboard
            ),
        },
        timeout=15,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram error: {result}"
        )


# ============================================================
# DATE AND TIME HELPERS
# ============================================================

def parse_datetime(value):

    if not value:
        return None

    if not isinstance(value, str):
        return None

    try:

        return datetime.fromisoformat(
            value.replace("Z", "")
        )

    except ValueError:

        return None


def monday_of_week(value):

    return value - timedelta(
        days=value.weekday()
    )


def sunday_of_week(value):

    return (
        monday_of_week(value)
        + timedelta(days=6)
    )


def format_date(value):

    parsed = parse_datetime(value)

    if parsed is None:
        return str(value)

    return parsed.strftime(
        "%a %d %b %Y"
    )


def priority_for_date(value):

    parsed = parse_datetime(value)

    if parsed is None:
        return "🟢 WEEKDAY"

    if parsed.weekday() == 5:
        return "🔥 SATURDAY"

    if parsed.weekday() == 6:
        return "🔥 SUNDAY"

    return "🟢 WEEKDAY"


def as_minutes(hour, minute):
    """Times compare far more easily as minutes past midnight."""

    return (hour * 60) + minute


def duty_times(duty):
    """
    Return (start_minutes, end_minutes), or None if Allocate
    did not supply usable times.
    """

    start = duty.get("startTime") or {}
    end = duty.get("endTime") or {}

    start_hour = start.get("Hour")
    start_minute = start.get("Minute")

    end_hour = end.get("Hour")
    end_minute = end.get("Minute")

    if None in (
        start_hour,
        start_minute,
        end_hour,
        end_minute,
    ):

        return None

    return (
        as_minutes(
            start_hour,
            start_minute,
        ),
        as_minutes(
            end_hour,
            end_minute,
        ),
    )


def is_night_shift(duty):
    """
    A shift that runs past midnight.

    The end time being at or before the start time is what
    tells us the shift wrapped into the next day.
    """

    times = duty_times(duty)

    if times is None:
        return False

    start_minutes, end_minutes = times

    return end_minutes <= start_minutes


def priority_for_duty(duty):
    """
    Priority label stored in the database.

    Unlike priority_for_date this also knows about nights.
    """

    date = parse_datetime(
        duty.get("onDate")
    )

    if date is None:
        return "🟢 WEEKDAY"

    if is_night_shift(duty):

        return (
            "🌙 "
            + date.strftime("%a").upper()
            + " NIGHT"
        )

    return priority_for_date(
        duty.get("onDate")
    )


# ============================================================
# ALLOCATE RESPONSE IDENTIFICATION
# ============================================================

def is_unfilled_shifts(response):

    return (
        "UnfilledShifts" in response.url
        and response.request.method == "POST"
        and response.status == 200
    )


def is_approved_roster(response):

    return (
        "GetApprovedRoster" in response.url
        and response.request.method == "POST"
        and response.status == 200
    )


# ============================================================
# SESSION CHECK
#
# Observed behaviour: with no valid session, Allocate does not
# show a login form. It serves an empty JavaScript shell with
# no title and no body text, at the same URL. So we do not try
# to recognise failure. We recognise SUCCESS, and treat its
# absence as a dead session.
# ============================================================

ALLOCATE_DOMAIN = "allocate-cloud.co.uk"

# What a WORKING page looks like.

APP_MARKERS = (
    "available bank duties",
    "personal roster",
)

# What a broken page sometimes looks like. Useful extra
# detail, but never relied upon on its own.

LOGIN_MARKERS = (
    "sign in",
    "log in",
    "login",
    "username",
    "password",
    "authenticator",
    "verification code",
)

# A working Allocate page contains a great deal of text.
# Anything shorter than this is not a real page.

MINIMUM_PAGE_TEXT = 40


def read_body_text(page):

    try:

        return page.locator(
            "body"
        ).inner_text().strip()

    except Exception:

        return ""


def session_looks_dead(page):
    """
    Decide whether the Allocate session has stopped working.

    Returns (dead, reason).

    This is only ever called AFTER a request has already
    failed, so we are explaining a known failure rather than
    policing a healthy page.
    """

    body = read_body_text(page)

    # Give a slow render one further chance.

    if len(body) < MINIMUM_PAGE_TEXT:

        page.wait_for_timeout(3000)

        body = read_body_text(page)

    lowered = body.lower()

    # --------------------------------------------------------
    # The application is on screen, so the session is fine
    # and something else went wrong.
    # --------------------------------------------------------

    if any(
        marker in lowered
        for marker in APP_MARKERS
    ):

        return (
            False,
            "the application is still loaded",
        )

    if len(body) < MINIMUM_PAGE_TEXT:

        return (
            True,
            "Allocate returned an empty page "
            f"({len(body)} characters of text)",
        )

    if any(
        marker in lowered
        for marker in LOGIN_MARKERS
    ):

        return (
            True,
            "Allocate returned a login page",
        )

    if ALLOCATE_DOMAIN not in page.url:

        return (
            True,
            "the browser was redirected to "
            f"{page.url}",
        )

    return (
        False,
        "the page does not look like a login page",
    )


def looks_like_login_page(page):
    """Kept so session_expiry_test.py still runs."""

    dead, _ = session_looks_dead(page)

    return dead


def raise_if_logged_out(page, context_note):
    """
    Raise SessionExpired if the session has stopped working.
    Otherwise do nothing and let the original error surface.
    """

    dead, reason = session_looks_dead(page)

    if dead:

        raise SessionExpired(
            "The saved Allocate session is no "
            "longer valid.\n\n"
            f"Detected {context_note}: {reason}.\n\n"
            "Run login_loop.py, log in manually, "
            "then restart the monitor."
        )


# ============================================================
# TARGET SHIFT FILTER
# ============================================================

def is_target_shift(duty):
    """
    Weekday: any shift starting between 06:00 and 11:00,
             whatever time it ends.
    Weekend: any shift contained within 06:00-22:00.
    Nights:  shifts running past midnight, but only when they
             start on a Friday, Saturday or Sunday evening.

    canBookBankPosting is deliberately NOT tested. A shift
    needing a call to Staff Bank is still worth knowing about;
    that fact is reported in the alert instead of used to
    silence it.
    """

    unit = (
        duty.get("unit") or ""
    ).strip()

    grade = (
        duty.get("grade") or ""
    ).strip()

    if unit != TARGET_UNIT:
        return False

    if grade != TARGET_GRADE:
        return False

    times = duty_times(duty)

    if times is None:
        return False

    start_minutes, end_minutes = times

    date = parse_datetime(
        duty.get("onDate")
    )

    if date is None:
        return False

    # --------------------------------------------------------
    # Nights: the shift wrapped past midnight.
    #
    # Judged on the day it STARTS, so a Friday night finishing
    # Saturday morning counts as a Friday night.
    # --------------------------------------------------------

    if end_minutes <= start_minutes:

        if date.weekday() not in NIGHT_DAYS:
            return False

        return start_minutes >= as_minutes(
            *NIGHT_START_EARLIEST
        )

    # --------------------------------------------------------
    # Weekend: anything inside the window.
    # --------------------------------------------------------

    if date.weekday() >= 5:

        return (
            start_minutes >= as_minutes(
                *WEEKEND_WINDOW_START
            )
            and end_minutes <= as_minutes(
                *WEEKEND_WINDOW_END
            )
        )

    # --------------------------------------------------------
    # Weekday: judged on the START time only.
    #
    # The end time is deliberately not tested, so an
    # irregular finish such as 07:15-15:45 still matches.
    # --------------------------------------------------------

    return (
        start_minutes >= as_minutes(
            *WEEKDAY_START_EARLIEST
        )
        and start_minutes <= as_minutes(
            *WEEKDAY_START_LATEST
        )
    )


# ============================================================
# DATABASE
# ============================================================

def initialise_database():

    connection = sqlite3.connect(
        DATABASE
    )

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            duty_id INTEGER PRIMARY KEY,
            request_id TEXT,
            bank_posting_id INTEGER,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            unit TEXT NOT NULL,
            grade TEXT NOT NULL,
            priority TEXT NOT NULL,
            can_book INTEGER NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0
        )
    """)

    connection.commit()

    return connection


def prune_old_shifts(connection):
    """
    Delete shifts whose date has passed.

    A shift in the past cannot be booked, so the row has no
    further use. This keeps the database small and, as a side
    effect, clears out the old rows that used the earlier
    'HIGH-SATURDAY' priority format.
    """

    today = (
        datetime.now()
        .date()
        .isoformat()
    )

    cursor = connection.cursor()

    cursor.execute(
        """
        DELETE FROM shifts
        WHERE substr(date, 1, 10) < ?
        """,
        (today,),
    )

    removed = cursor.rowcount

    connection.commit()

    if removed:

        print(
            f"\nRemoved {removed} past "
            "shift(s) from the database."
        )

    return removed


def get_shift_notification_state(
    connection,
    duty_id,
):

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT notified
        FROM shifts
        WHERE duty_id = ?
        """,
        (duty_id,),
    )

    row = cursor.fetchone()

    if row is None:
        return None

    return row[0]


def store_shift(
    connection,
    duty,
):
    """
    Store a shift.

    Returns:
        "new"      - never seen before
        "retry"    - seen before but not successfully notified
        "existing" - already notified

    IMPORTANT:
    This function has nothing to do with booking counts.
    It only tracks availability and notification state.
    """

    duty_id = duty["dutyId"]

    existing = get_shift_notification_state(
        connection,
        duty_id,
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    start = duty["startTime"]
    end = duty["endTime"]

    start_text = (
        f'{start["Hour"]:02d}:'
        f'{start["Minute"]:02d}'
    )

    end_text = (
        f'{end["Hour"]:02d}:'
        f'{end["Minute"]:02d}'
    )

    priority = priority_for_duty(
        duty
    )

    can_book = (
        1
        if duty.get("canBookBankPosting")
        else 0
    )

    cursor = connection.cursor()

    if existing is not None:

        cursor.execute(
            """
            UPDATE shifts
            SET last_seen = ?,
                can_book = ?,
                priority = ?
            WHERE duty_id = ?
            """,
            (
                now,
                can_book,
                priority,
                duty_id,
            ),
        )

        connection.commit()

        if existing == 0:
            return "retry"

        return "existing"

    cursor.execute(
        """
        INSERT INTO shifts (
            duty_id,
            request_id,
            bank_posting_id,
            date,
            start_time,
            end_time,
            unit,
            grade,
            priority,
            can_book,
            first_seen,
            last_seen,
            notified
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            duty_id,
            duty.get("requestId"),
            duty.get("bankPostingId"),
            duty["onDate"],
            start_text,
            end_text,
            (duty.get("unit") or "").strip(),
            (duty.get("grade") or "").strip(),
            priority,
            can_book,
            now,
            now,
        ),
    )

    connection.commit()

    return "new"


def mark_notified(
    connection,
    duty_id,
):

    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE shifts
        SET notified = 1
        WHERE duty_id = ?
        """,
        (duty_id,),
    )

    connection.commit()


# ============================================================
# COLLECT ALL PAGINATION PAGES FOR THE CURRENT WEEK
# ============================================================

def collect_week(
    page,
    first_data,
):

    all_duties = []

    data = first_data

    while True:

        duties = data.get(
            "vacantDuties",
            []
        )

        total = data.get(
            "TotalItemsCount",
            0
        )

        all_duties.extend(
            duties
        )

        print(
            f"      Page: +{len(duties)} "
            f"({len(all_duties)}/{total})"
        )

        if len(all_duties) >= total:
            break

        # Guard against an empty page causing an endless loop.
        if not duties:

            print(
                "      Empty page returned; "
                "stopping pagination."
            )

            break

        next_link = page.locator(
            'a[title="Go to the next page"]'
        )

        if next_link.count() == 0:

            raise RuntimeError(
                "Next pagination link "
                "was not found."
            )

        next_class = (
            next_link.first.get_attribute("class")
            or ""
        )

        if "disabled" in next_class:

            print(
                "      Next page is disabled "
                "before all duties were collected."
            )

            break

        try:

            with page.expect_response(
                is_unfilled_shifts,
                timeout=60000,
            ) as response_info:

                next_link.first.evaluate(
                    "(element) => element.click()"
                )

            data = (
                response_info.value.json()
            )

        except PlaywrightTimeoutError as exc:

            raise_if_logged_out(
                page,
                "while paginating shifts"
            )

            raise RuntimeError(
                "Timed out retrieving "
                "pagination page."
            ) from exc

        page.wait_for_timeout(
            300
        )

    # Deduplicate by duty ID.

    unique = {}

    for duty in all_duties:

        duty_id = duty.get(
            "dutyId"
        )

        if duty_id is not None:

            unique[duty_id] = duty

    return list(
        unique.values()
    )


# ============================================================
# SCAN AVAILABLE SHIFTS
# ============================================================

def scan_available_shifts(page):

    try:

        with page.expect_response(
            is_unfilled_shifts,
            timeout=60000,
        ) as response_info:

            page.goto(
                SHIFTS_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        current_data = (
            response_info.value.json()
        )

    except PlaywrightTimeoutError as exc:

        raise_if_logged_out(
            page,
            "while opening Available Bank Duties"
        )

        raise RuntimeError(
            "The initial UnfilledShifts response "
            "was not received."
        ) from exc

    all_matches = []

    for week_number in range(
        WEEKS_TO_SCAN
    ):

        print(
            f"\nWEEK {week_number + 1} "
            f"OF {WEEKS_TO_SCAN}"
        )

        duties = collect_week(
            page,
            current_data,
        )

        matches = [
            duty
            for duty in duties
            if is_target_shift(duty)
        ]

        print(
            f"    Total duties: "
            f"{len(duties)}"
        )

        print(
            f"    Matching target shifts: "
            f"{len(matches)}"
        )

        all_matches.extend(
            matches
        )

        if week_number >= (
            WEEKS_TO_SCAN - 1
        ):
            break

        next_week = page.locator(
            "#btnNext"
        )

        if next_week.count() == 0:

            raise RuntimeError(
                "The Next Week button "
                "was not found."
            )

        if next_week.is_disabled():

            print(
                "    Next Week is disabled."
            )

            break

        print(
            "    Moving to next week..."
        )

        try:

            with page.expect_response(
                is_unfilled_shifts,
                timeout=60000,
            ) as response_info:

                next_week.evaluate(
                    "(element) => element.click()"
                )

            current_data = (
                response_info.value.json()
            )

        except PlaywrightTimeoutError as exc:

            raise_if_logged_out(
                page,
                "while changing week"
            )

            raise RuntimeError(
                "Timed out moving to "
                "the next week."
            ) from exc

        page.wait_for_timeout(
            500
        )

    # Remove duplicates across weeks.

    unique = {}

    for duty in all_matches:

        unique[
            duty["dutyId"]
        ] = duty

    return list(
        unique.values()
    )


# ============================================================
# GET ACTUAL APPROVED BOOKINGS
#
# Returns:
#
#     { week_start (date) : count (int) }
#
# EVERY active bank duty counts toward the weekly limit,
# regardless of its times.
#
# These numbers come ONLY from Allocate's approved roster.
# We never increment them for an available shift.
# ============================================================

def get_booking_counts(page):

    print(
        "\nOpening Personal Roster..."
    )

    try:

        with page.expect_response(
            is_approved_roster,
            timeout=60000,
        ) as response_info:

            page.goto(
                ROSTER_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        data = (
            response_info.value.json()
        )

    except PlaywrightTimeoutError as exc:

        raise_if_logged_out(
            page,
            "while opening the Personal Roster"
        )

        raise RuntimeError(
            "The GetApprovedRoster response "
            "was not received."
        ) from exc

    duties = data.get(
        "duties",
        []
    )

    print(
        "    Roster duties returned:",
        len(duties)
    )

    # --------------------------------------------------------
    # Group active bank duties by Monday-Sunday week.
    # --------------------------------------------------------

    weekly = defaultdict(list)

    for duty in duties:

        # Only bank duties count.
        if duty.get("isBank") is not True:
            continue

        # Ignore cancelled duties.
        if duty.get("isCancelled") is True:
            continue

        start_value = (
            duty.get("start")
            or duty.get("dutyStart")
            or duty.get("plannedStartTime")
        )

        start = parse_datetime(
            start_value
        )

        if not start:
            continue

        week_start = monday_of_week(
            start.date()
        )

        weekly[
            week_start
        ].append(duty)

    # --------------------------------------------------------
    # Convert week -> list of duties
    # into    week -> integer count.
    #
    # build_alert() compares this against WEEKLY_LIMIT, so it
    # must be a number, not a list.
    # --------------------------------------------------------

    booking_counts = {
        week_start: len(duties_for_week)
        for week_start, duties_for_week
        in weekly.items()
    }

    print(
        "\nCURRENT ACTUAL BOOKING COUNTS"
    )

    print(
        "-" * 70
    )

    if not booking_counts:

        print(
            "No active bank duties were found."
        )

    for week_start in sorted(
        booking_counts
    ):

        week_end = sunday_of_week(
            week_start
        )

        count = booking_counts[
            week_start
        ]

        if count < WEEKLY_LIMIT:

            status = (
                f"{WEEKLY_LIMIT - count} standard "
                f"slot(s) remaining"
            )

        else:

            status = (
                "🚨 standard limit reached"
            )

        print(
            f"{week_start} – {week_end}: "
            f"{count}/{WEEKLY_LIMIT} | {status}"
        )

    return booking_counts


# ============================================================
# BUILD TELEGRAM ALERT
# ============================================================

def build_alert(
    duty,
    booking_count,
):

    date = parse_datetime(
        duty["onDate"]
    )

    day = date.strftime("%A")

    date_text = date.strftime(
        "%d %B %Y"
    )

    start = duty["startTime"]
    end = duty["endTime"]

    start_text = (
        f'{start["Hour"]:02d}:'
        f'{start["Minute"]:02d}'
    )

    end_text = (
        f'{end["Hour"]:02d}:'
        f'{end["Minute"]:02d}'
    )

    week_start = monday_of_week(
        date.date()
    )

    week_end = sunday_of_week(
        date.date()
    )

    is_weekend = (
        date.weekday() >= 5
    )

    is_night = is_night_shift(duty)

    can_self_book = (
        duty.get("canBookBankPosting")
        is True
    )

    # --------------------------------------------------------
    # Weekend and night shifts carry enhanced pay and go
    # quickly, so they are marked distinctly.
    # --------------------------------------------------------

    if is_night:

        heading = (
            "🌙 NEW "
            + date.strftime("%A").upper()
            + " NIGHT SHIFT"
        )

    elif date.weekday() == 5:
        heading = "🔥 NEW SATURDAY SHIFT"

    elif date.weekday() == 6:
        heading = "🔥 NEW SUNDAY SHIFT"

    else:
        heading = "🟢 NEW SHIFT"

    # Overnight shifts finish on the following day, so say so
    # rather than showing a span that reads backwards.

    times_text = (
        f"{start_text}–{end_text}"
        + (
            " (overnight, finishes next day)"
            if is_night
            else ""
        )
    )

    lines = [
        heading,
        "",
        *([SITE_NAME] if SITE_NAME else []),
        TARGET_UNIT,
        TARGET_GRADE,
        "",
        f"{day} {date_text}",
        times_text,
        "",
        (
            "Week: "
            f"{week_start.strftime('%d %b')}–"
            f"{week_end.strftime('%d %b %Y')}"
        ),
        "",
    ]

    if is_night:

        lines.extend([
            "⚡ WEEKEND NIGHT — ENHANCED RATE",
            "",
        ])

    elif is_weekend:

        lines.extend([
            "⚡ WEEKEND — ENHANCED RATE",
            "",
        ])

    # --------------------------------------------------------
    # FACT ONE: the real booking count from Allocate.
    #
    # Never incremented between shifts.
    # --------------------------------------------------------

    if booking_count is None:

        within_limit = None

        lines.extend([
            "⚠️ BOOKING COUNT UNKNOWN",
            "",
            "Allocate did not return your "
            "booking data for this week.",
        ])

    elif booking_count < WEEKLY_LIMIT:

        within_limit = True

        lines.extend([
            "Current bookings: "
            f"{booking_count}/{WEEKLY_LIMIT}",
            "",
            "This would be booking "
            f"#{booking_count + 1}.",
            "",
            "✅ Within the standard weekly limit.",
        ])

    else:

        within_limit = False

        lines.extend([
            "Current bookings: "
            f"{booking_count}/{WEEKLY_LIMIT}",
            "",
            "This would be booking "
            f"#{booking_count + 1}.",
            "",
            "🚨 STANDARD WEEKLY LIMIT REACHED",
        ])

    # --------------------------------------------------------
    # FACT TWO: whether Loop will let you book it yourself.
    #
    # Independent of the weekly limit, so the two combine
    # into four possible actions.
    # --------------------------------------------------------

    lines.extend([
        "",
        "HOW TO BOOK",
    ])

    if can_self_book and within_limit is True:

        lines.append(
            "Open Allocate Loop and book it "
            "yourself."
        )

    elif can_self_book and within_limit is False:

        lines.extend([
            "Loop will let you book this, but it "
            "exceeds your standard weekly limit.",
            "",
            f"📞 Contact {STAFF_BANK_NAME} "
            "before booking.",
        ])

    elif can_self_book:

        lines.append(
            "Check your weekly count, then book "
            "in Allocate Loop."
        )

    elif within_limit is False:

        lines.extend([
            "📞 This shift cannot be booked in "
            "Loop, AND it exceeds your standard "
            "weekly limit.",
            "",
            f"Contact {STAFF_BANK_NAME}.",
        ])

    else:

        lines.extend([
            "📞 This shift cannot be booked in "
            "Loop directly.",
            "",
            f"Contact {STAFF_BANK_NAME} to "
            "request it.",
        ])

    lines.extend([
        "",
        "Self-booking: "
        + (
            "available"
            if can_self_book
            else "NOT available"
        ),
        f"Request ID: {duty.get('requestId')}",
        f"Duty ID: {duty.get('dutyId')}",
        "",
        "No automatic booking was performed.",
    ])

    return "\n".join(lines)


# ============================================================
# PROCESS NEW SHIFTS
# ============================================================

def process_new_shifts(
    connection,
    page,
    matches,
):

    candidates = []

    # --------------------------------------------------------
    # First identify new/unnotified duties.
    # --------------------------------------------------------

    for duty in matches:

        state = store_shift(
            connection,
            duty,
        )

        if state == "new":

            print(
                "    🆕 NEW:",
                duty["dutyId"]
            )

            candidates.append(
                duty
            )

        elif state == "retry":

            print(
                "    🔁 RETRY:",
                duty["dutyId"]
            )

            candidates.append(
                duty
            )

        else:

            print(
                "    ✓ Already known:",
                duty["dutyId"]
            )

    if not candidates:

        print(
            "\nNo new or unnotified target shifts."
        )

        return 0

    # --------------------------------------------------------
    # Only now do we load the roster. There is no point
    # opening it when there is nothing to alert about.
    # --------------------------------------------------------

    weekly_counts = get_booking_counts(
        page
    )

    # --------------------------------------------------------
    # Enhanced-rate shifts first: weekends and weekend nights.
    #
    # This affects alert ORDER only.
    # It does not affect booking counts.
    # --------------------------------------------------------

    candidates.sort(
        key=lambda duty: (
            0
            if (
                parse_datetime(
                    duty["onDate"]
                ).weekday() >= 5
                or is_night_shift(duty)
            )
            else 1,
            duty["onDate"],
            duty["dutyId"],
        )
    )

    sent = 0

    for duty in candidates:

        date = parse_datetime(
            duty["onDate"]
        )

        week_start = monday_of_week(
            date.date()
        )

        # ----------------------------------------------------
        # Always read the actual count from Allocate.
        #
        # We do NOT increment this after an alert. Two shifts
        # in the same week are both "booking #N+1", because
        # booking one is a decision you make manually.
        # ----------------------------------------------------

        booking_count = weekly_counts.get(
            week_start
        )

        message = build_alert(
            duty,
            booking_count,
        )

        try:

            send_telegram(
                message
            )

            mark_notified(
                connection,
                duty["dutyId"],
            )

            print(
                "    📱 Telegram sent:",
                duty["dutyId"]
            )

            sent += 1

        except Exception as exc:

            print(
                "    ❌ Telegram failed:",
                exc
            )

            # notified stays 0, so the next pass retries.

    return sent


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)

    print(
        "BOOKING-AWARE "
        "SINGLE-PASS MONITOR"
    )

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

    print("Automatic booking: DISABLED")

    print("=" * 70)

    check_config()

    connection = initialise_database()

    prune_old_shifts(connection)

    try:

        with sync_playwright() as p:

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

            print(
                "\nOpening saved Allocate session..."
            )

            page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.wait_for_timeout(1500)

            print(
                "Checking available shifts..."
            )

            matches = scan_available_shifts(
                page
            )

            print("\n" + "=" * 70)
            print("TARGET SHIFT SUMMARY")
            print("=" * 70)

            print(
                "Matching target shifts:",
                len(matches)
            )

            sent = process_new_shifts(
                connection,
                page,
                matches,
            )

            print("\n" + "=" * 70)
            print("MONITOR PASS COMPLETE")
            print("=" * 70)

            print(
                "Alerts sent:",
                sent
            )

            context.close()

    except SessionExpired as exc:

        print("\n" + "=" * 70)
        print("🔐 SESSION EXPIRED")
        print("=" * 70)

        print(exc)

    finally:

        connection.close()


if __name__ == "__main__":
    main()
