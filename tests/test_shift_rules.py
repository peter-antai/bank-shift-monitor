"""
Tests for the pure logic: which shifts match, how the weekly
limit is reported, and how notification state is stored.
"""

import pytest

import monitor_booking_once as monitor


def make_duty(date, start, end, **overrides):
    """Build a duty shaped like Allocate's UnfilledShifts JSON."""

    duty = {
        "onDate": f"{date}T00:00:00",
        "startTime": {"Hour": start[0], "Minute": start[1]},
        "endTime": {"Hour": end[0], "Minute": end[1]},
        "unit": monitor.TARGET_UNIT,
        "grade": monitor.TARGET_GRADE,
        "requestId": "TEST-001",
        "dutyId": 90000001,
        "canBookBankPosting": True,
    }

    duty.update(overrides)

    return duty


# 2026-09-07 is a Monday, so the week runs 07-13 September.
MONDAY = "2026-09-07"
THURSDAY = "2026-09-10"
FRIDAY = "2026-09-11"
SATURDAY = "2026-09-12"
SUNDAY = "2026-09-13"


@pytest.mark.parametrize(
    "date, start, end, expected",
    [
        # Weekday: judged on the start time only.
        (MONDAY, (6, 0), (14, 0), True),
        (MONDAY, (7, 15), (15, 45), True),
        (MONDAY, (11, 0), (19, 0), True),
        (MONDAY, (11, 1), (19, 0), False),
        (MONDAY, (5, 59), (14, 0), False),
        (MONDAY, (14, 0), (22, 0), False),
        # Weekend: the whole shift must sit inside 06:00-22:00.
        (SATURDAY, (6, 0), (14, 0), True),
        (SATURDAY, (14, 0), (22, 0), True),
        (SUNDAY, (14, 0), (22, 30), False),
        (SUNDAY, (5, 30), (13, 30), False),
        # Nights: only when they start Fri/Sat/Sun from 18:00.
        (FRIDAY, (22, 0), (6, 0), True),
        (SATURDAY, (22, 0), (6, 0), True),
        (SUNDAY, (18, 0), (2, 0), True),
        (THURSDAY, (22, 0), (6, 0), False),
        (SATURDAY, (17, 0), (1, 0), False),
    ],
)
def test_time_rules(date, start, end, expected):

    duty = make_duty(date, start, end)

    assert monitor.is_target_shift(duty) is expected


def test_other_unit_or_grade_is_rejected():

    assert not monitor.is_target_shift(
        make_duty(MONDAY, (6, 0), (14, 0), unit="Another Unit")
    )

    assert not monitor.is_target_shift(
        make_duty(MONDAY, (6, 0), (14, 0), grade="Another Grade")
    )


def test_missing_times_are_rejected():

    duty = make_duty(MONDAY, (6, 0), (14, 0))
    duty["startTime"] = {}

    assert not monitor.is_target_shift(duty)


def test_self_booking_flag_does_not_filter():
    """Bookability is reported in the alert, never used to hide a shift."""

    duty = make_duty(
        MONDAY,
        (6, 0),
        (14, 0),
        canBookBankPosting=False,
    )

    assert monitor.is_target_shift(duty)


def test_night_shift_detection():

    assert monitor.is_night_shift(make_duty(FRIDAY, (22, 0), (6, 0)))
    assert not monitor.is_night_shift(make_duty(FRIDAY, (6, 0), (14, 0)))


def test_alert_within_limit():

    text = monitor.build_alert(make_duty(MONDAY, (6, 0), (14, 0)), 2)

    assert "Current bookings: 2/4" in text
    assert "This would be booking #3." in text
    assert "Within the standard weekly limit" in text
    assert "book it yourself" in text
    assert "No automatic booking was performed." in text


def test_alert_at_limit_points_to_staff_bank():

    text = monitor.build_alert(make_duty(SATURDAY, (22, 0), (6, 0)), 4)

    assert "This would be booking #5." in text
    assert "STANDARD WEEKLY LIMIT REACHED" in text
    assert "Contact Test Staff Bank before booking." in text
    assert "overnight, finishes next day" in text


def test_alert_not_self_bookable_over_limit():

    duty = make_duty(
        MONDAY,
        (6, 0),
        (14, 0),
        canBookBankPosting=False,
    )

    text = monitor.build_alert(duty, 4)

    assert "Self-booking: NOT available" in text
    assert "Contact Test Staff Bank." in text


def test_alert_with_unknown_count():

    text = monitor.build_alert(make_duty(MONDAY, (6, 0), (14, 0)), None)

    assert "BOOKING COUNT UNKNOWN" in text


def test_alert_uses_configured_names():

    text = monitor.build_alert(make_duty(MONDAY, (6, 0), (14, 0)), 0)

    assert "Test Site" in text
    assert "Test Unit" in text
    assert "Test Grade" in text


def test_shift_is_stored_once_and_retried_until_notified():

    connection = monitor.initialise_database()

    duty = make_duty(MONDAY, (6, 0), (14, 0), dutyId=91000001)

    try:

        assert monitor.store_shift(connection, duty) == "new"
        assert monitor.store_shift(connection, duty) == "retry"

        monitor.mark_notified(connection, duty["dutyId"])

        assert monitor.store_shift(connection, duty) == "existing"

    finally:

        connection.close()


def test_check_config_reports_missing_settings(monkeypatch):

    monkeypatch.setattr(monitor, "ALLOCATE_TENANT", "")

    with pytest.raises(SystemExit) as error:
        monitor.check_config()

    assert "ALLOCATE_TENANT" in str(error.value)


def test_check_config_passes_when_complete():

    monitor.check_config()
