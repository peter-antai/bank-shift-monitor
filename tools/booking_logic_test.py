from datetime import datetime
from playwright.sync_api import sync_playwright

from monitor_booking_once import (
    LOGIN_URL,
    TARGET_UNIT,
    TARGET_GRADE,
    WEEKLY_LIMIT,
    PROFILE,
    get_booking_counts,
    build_alert,
    parse_datetime,
    monday_of_week,
)


# ============================================================
# SIMULATED NEW SHIFTS
#
# These are NOT sent to Telegram.
# They are only used to test the booking-count logic.
# ============================================================

fake_shifts = [

    {
        "onDate": "2026-09-12T00:00:00",
        "startTime": {
            "Hour": 6,
            "Minute": 0,
        },
        "endTime": {
            "Hour": 14,
            "Minute": 0,
        },
        "unit": TARGET_UNIT,
        "grade": TARGET_GRADE,
        "requestId": "TEST-SAT-001",
        "dutyId": 90000001,
        "canBookBankPosting": True,
    },

    {
        "onDate": "2026-09-12T00:00:00",
        "startTime": {
            "Hour": 6,
            "Minute": 0,
        },
        "endTime": {
            "Hour": 14,
            "Minute": 0,
        },
        "unit": TARGET_UNIT,
        "grade": TARGET_GRADE,
        "requestId": "TEST-SAT-002",
        "dutyId": 90000002,
        "canBookBankPosting": True,
    },

    {
        "onDate": "2026-09-13T00:00:00",
        "startTime": {
            "Hour": 6,
            "Minute": 0,
        },
        "endTime": {
            "Hour": 14,
            "Minute": 0,
        },
        "unit": TARGET_UNIT,
        "grade": TARGET_GRADE,
        "requestId": "TEST-SUN-001",
        "dutyId": 90000003,
        "canBookBankPosting": True,
    },
]


# ============================================================
# MAIN TEST
# ============================================================

with sync_playwright() as p:

    print(
        "Opening saved Allocate session..."
    )

    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False,
    )

    page = (
        context.pages[0]
        if context.pages
        else context.new_page()
    )

    page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.wait_for_timeout(1500)

    print(
        "Loading actual Personal Roster..."
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # get_booking_counts() now returns a dictionary:
    #
    #     week_start -> integer count
    #
    # so DO NOT use [0].
    # --------------------------------------------------------

    booking_counts = get_booking_counts(
        page
    )

    print("\n")
    print("=" * 70)
    print("SIMULATED NEW-SHIFT BOOKING TEST")
    print("=" * 70)

    # --------------------------------------------------------
    # Test each fake shift independently.
    #
    # The booking count is NEVER incremented.
    # --------------------------------------------------------

    for number, duty in enumerate(
        fake_shifts,
        start=1,
    ):

        date = parse_datetime(
            duty["onDate"]
        )

        week_start = monday_of_week(
            date.date()
        )

        actual_count = booking_counts.get(
            week_start
        )

        print("\n" + "-" * 70)

        print(
            f"SIMULATED SHIFT {number}"
        )

        print(
            "Date:",
            date.strftime(
                "%a %d %b %Y"
            )
        )

        print(
            "Week:",
            week_start.strftime(
                "%d %b %Y"
            )
        )

        print(
            "Actual Allocate booking count:",
            (
                f"{actual_count}/{WEEKLY_LIMIT}"
                if actual_count is not None
                else "UNKNOWN"
            ),
        )

        if actual_count is None:

            projected = None

        else:

            projected = (
                actual_count + 1
            )

        print(
            "This shift would be booking:",
            (
                f"#{projected}"
                if projected is not None
                else "UNKNOWN"
            ),
        )

        # ----------------------------------------------------
        # Generate the real alert text for inspection only.
        #
        # It is NOT sent to Telegram.
        # ----------------------------------------------------

        message = build_alert(
            duty,
            actual_count,
        )

        print("\nALERT PREVIEW")
        print("-" * 70)
        print(message)

    # --------------------------------------------------------
    # Final verification.
    # --------------------------------------------------------

    print("\n")
    print("=" * 70)
    print("EXPECTED BEHAVIOUR")
    print("=" * 70)

    test_date = datetime(
        2026,
        9,
        12,
    ).date()

    test_week = monday_of_week(
        test_date
    )

    actual_count = booking_counts.get(
        test_week
    )

    print(
        f"Actual Allocate count for "
        f"{test_week}: "
        f"{actual_count}/{WEEKLY_LIMIT}"
    )

    if actual_count is not None and actual_count >= WEEKLY_LIMIT:

        print(
            "\n✅ BOOKING COUNT TEST PASSED"
        )

        print(
            "Allocate reports the weekly limit "
            "is already reached for the test week."
        )

        print(
            "Each simulated available shift "
            "should therefore be presented as "
            "a booking over the weekly limit."
        )

    elif actual_count is None:

        print(
            "\n⚠️ BOOKING COUNT UNKNOWN"
        )

        print(
            "Allocate did not return data "
            "for this week."
        )

    else:

        print(
            "\n⚠️ CURRENT COUNT:",
            actual_count,
        f"/{WEEKLY_LIMIT}"
        )

        print(
            "The test week is under the weekly limit, so "
            "each alert should show the remaining slots."
        )

    print("\n" + "=" * 70)

    print(
        "NO TELEGRAM MESSAGE WAS SENT"
    )

    print(
        "NO DATABASE RECORD WAS CREATED"
    )

    print(
        "NO BOOKING WAS ATTEMPTED"
    )

    print("=" * 70)

    input(
        "\nPress ENTER to close..."
    )

    context.close()
