"""
NEAR-MISS REPORT

Scans the same weeks as the monitor, but instead of showing
only matches, it shows every duty at your unit and explains
why each one was accepted or rejected.

Use this when the monitor reports no matching shifts, to tell
the difference between:

    "there genuinely are none"

and

    "my filter is quietly excluding shifts I would want."

No Telegram message is sent. No database record is written.
"""

from collections import Counter, defaultdict

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from monitor_booking_once import (
    LOGIN_URL,
    SHIFTS_URL,
    PROFILE,
    WEEKS_TO_SCAN,
    TARGET_UNIT,
    TARGET_GRADE,
    WEEKDAY_START_EARLIEST,
    WEEKDAY_START_LATEST,
    WEEKEND_WINDOW_START,
    WEEKEND_WINDOW_END,
    NIGHT_DAYS,
    NIGHT_START_EARLIEST,
    SessionExpired,
    as_minutes,
    duty_times,
    is_target_shift,
    is_unfilled_shifts,
    parse_datetime,
    collect_week,
    raise_if_logged_out,
)


def time_text(minutes_value):

    return (
        f"{minutes_value // 60:02d}:"
        f"{minutes_value % 60:02d}"
    )


def explain(duty):
    """
    Return (accepted, reason) for a single duty.

    This mirrors is_target_shift, but reports WHY rather than
    just yes or no.
    """

    unit = (
        duty.get("unit") or ""
    ).strip()

    grade = (
        duty.get("grade") or ""
    ).strip()

    if unit != TARGET_UNIT:
        return False, f"different unit ({unit})"

    if grade != TARGET_GRADE:
        return False, f"different grade ({grade})"

    times = duty_times(duty)

    if times is None:
        return False, "no usable times"

    start_minutes, end_minutes = times

    if end_minutes <= start_minutes:

        night_date = parse_datetime(
            duty.get("onDate")
        )

        if night_date is None:
            return False, "no usable date"

        if night_date.weekday() not in NIGHT_DAYS:

            return (
                False,
                "night shift, not Fri/Sat/Sun",
            )

        floor = as_minutes(
            *NIGHT_START_EARLIEST
        )

        if start_minutes < floor:

            return (
                False,
                "night shift, starts before "
                f"{time_text(floor)}",
            )

        return True, "weekend night"

    date = parse_datetime(
        duty.get("onDate")
    )

    if date is None:
        return False, "no usable date"

    if date.weekday() >= 5:

        window_start = as_minutes(
            *WEEKEND_WINDOW_START
        )

        window_end = as_minutes(
            *WEEKEND_WINDOW_END
        )

        if start_minutes < window_start:

            return (
                False,
                "weekend, starts before "
                f"{time_text(window_start)}",
            )

        if end_minutes > window_end:

            return (
                False,
                "weekend, ends after "
                f"{time_text(window_end)}",
            )

        return True, "weekend, inside window"

    earliest = as_minutes(
        *WEEKDAY_START_EARLIEST
    )

    latest = as_minutes(
        *WEEKDAY_START_LATEST
    )

    if start_minutes < earliest:

        return (
            False,
            "weekday, starts before "
            f"{time_text(earliest)}",
        )

    if start_minutes > latest:

        return (
            False,
            "weekday, starts after "
            f"{time_text(latest)}",
        )

    return True, "weekday, start in range"


def scan_all_duties(page):
    """
    Walk the same weeks the monitor walks, but keep every
    duty rather than only the matches.
    """

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

    everything = {}

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

        print(
            f"    Duties this week: "
            f"{len(duties)}"
        )

        for duty in duties:

            duty_id = duty.get("dutyId")

            if duty_id is not None:
                everything[duty_id] = duty

        if week_number >= (
            WEEKS_TO_SCAN - 1
        ):
            break

        next_week = page.locator("#btnNext")

        if next_week.count() == 0:

            print(
                "    Next Week button not found."
            )

            break

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
                "Timed out moving to the next week."
            ) from exc

        page.wait_for_timeout(500)

    return list(
        everything.values()
    )


# ============================================================
# MAIN
# ============================================================

print("=" * 70)
print("NEAR-MISS REPORT")
print("=" * 70)

print(f"Unit: {TARGET_UNIT}")
print(f"Grade: {TARGET_GRADE}")

print(
    "Weekday rule: starts "
    f"{time_text(as_minutes(*WEEKDAY_START_EARLIEST))}–"
    f"{time_text(as_minutes(*WEEKDAY_START_LATEST))}"
    " (any finish)"
)

print(
    "Weekend rule: inside "
    f"{time_text(as_minutes(*WEEKEND_WINDOW_START))}–"
    f"{time_text(as_minutes(*WEEKEND_WINDOW_END))}"
)

print(
    "Night rule: Fri/Sat/Sun starts from "
    f"{time_text(as_minutes(*NIGHT_START_EARLIEST))}"
)

print("=" * 70)


with sync_playwright() as p:

    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False,
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

    try:

        duties = scan_all_duties(page)

    except SessionExpired as exc:

        print("\n🔐 SESSION EXPIRED")
        print(exc)

        input("\nPress ENTER to close...")
        context.close()

        raise SystemExit

    # --------------------------------------------------------
    # Overall picture.
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("WHAT ALLOCATE IS OFFERING")
    print("=" * 70)

    print(
        "Unique duties across "
        f"{WEEKS_TO_SCAN} weeks: "
        f"{len(duties)}"
    )

    if not duties:

        print(
            "\n⚠️ Allocate returned NO duties at "
            "all. That is unusual, and suggests a "
            "scanning problem rather than an empty "
            "bank."
        )

    # --------------------------------------------------------
    # Which units exist at all?
    # --------------------------------------------------------

    units = Counter(
        (duty.get("unit") or "").strip()
        for duty in duties
    )

    print(
        "\nUnits represented "
        "(top 10 by duty count):"
    )

    for unit, count in units.most_common(10):

        mark = (
            "  ← your unit"
            if unit == TARGET_UNIT
            else ""
        )

        print(
            f"  {count:4d}  {unit}{mark}"
        )

    if TARGET_UNIT not in units:

        print(
            f"\n⚠️ {TARGET_UNIT} does not appear "
            "at all in the current listings."
        )

    # --------------------------------------------------------
    # Everything at your unit, accepted or not.
    # --------------------------------------------------------

    at_unit = [
        duty
        for duty in duties
        if (duty.get("unit") or "").strip()
        == TARGET_UNIT
    ]

    print("\n" + "=" * 70)
    print(f"EVERY DUTY AT {TARGET_UNIT}")
    print("=" * 70)

    print(
        f"Duties at your unit: {len(at_unit)}"
    )

    accepted = []
    rejected = defaultdict(list)

    for duty in at_unit:

        ok, reason = explain(duty)

        if ok:
            accepted.append((duty, reason))
        else:
            rejected[reason].append(duty)

    for duty, reason in sorted(
        accepted,
        key=lambda item: item[0].get("onDate") or "",
    ):

        date = parse_datetime(
            duty.get("onDate")
        )

        times = duty_times(duty) or (0, 0)

        print(
            "\n✅ MATCH  "
            f"{date.strftime('%a %d %b %Y')}  "
            f"{time_text(times[0])}–"
            f"{time_text(times[1])}"
        )

        print(
            f"          {reason} | "
            f"can self-book: "
            f"{duty.get('canBookBankPosting')} | "
            f"duty {duty.get('dutyId')}"
        )

    if rejected:

        print(
            "\n" + "-" * 70
        )

        print(
            "REJECTED, GROUPED BY REASON"
        )

        print("-" * 70)

    for reason in sorted(rejected):

        group = rejected[reason]

        print(
            f"\n{len(group)} × {reason}"
        )

        for duty in sorted(
            group,
            key=lambda item: item.get("onDate") or "",
        )[:10]:

            date = parse_datetime(
                duty.get("onDate")
            )

            times = duty_times(duty)

            when = (
                date.strftime("%a %d %b %Y")
                if date
                else "unknown date"
            )

            span = (
                f"{time_text(times[0])}–"
                f"{time_text(times[1])}"
                if times
                else "unknown times"
            )

            print(
                f"    {when}  {span}"
            )

        if len(group) > 10:

            print(
                f"    ... and {len(group) - 10} more"
            )

    # --------------------------------------------------------
    # Cross-check against the real filter.
    # --------------------------------------------------------

    real = [
        duty
        for duty in duties
        if is_target_shift(duty)
    ]

    print("\n" + "=" * 70)
    print("CROSS-CHECK")
    print("=" * 70)

    print(
        "This report accepted:",
        len(accepted)
    )

    print(
        "is_target_shift() accepted:",
        len(real)
    )

    if len(accepted) == len(real):

        print(
            "\n✅ The report agrees with the "
            "monitor's own filter."
        )

    else:

        print(
            "\n❌ MISMATCH. explain() and "
            "is_target_shift() disagree, so one "
            "of them has a bug."
        )

    print(
        "\nNo Telegram message was sent."
    )

    print(
        "No database record was written."
    )

    input(
        "\nPress ENTER to close..."
    )

    context.close()
