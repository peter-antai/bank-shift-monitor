"""
SESSION EXPIRY DETECTION TEST

Launches a BRAND NEW, EMPTY browser profile in a temporary
folder. An empty profile has no cookies, so Allocate must
show whatever it shows an unauthenticated visitor.

This is exactly what your monitor will see when the real
session finally expires.

SAFETY
------
Your real profile at ~/allocate-monitor/allocate-profile is
never opened, read, or modified. The temporary profile is
deleted at the end. No Telegram message is sent. No database
record is created.
"""

import shutil
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from monitor_booking_once import (
    PROFILE,
    SHIFTS_URL,
    LOGIN_MARKERS,
    SessionExpired,
    looks_like_login_page,
    scan_available_shifts,
)


# ============================================================
# Create a disposable profile.
# ============================================================

temporary_profile = Path(
    tempfile.mkdtemp(
        prefix="allocate-session-test-"
    )
)

print("=" * 70)
print("SESSION EXPIRY DETECTION TEST")
print("=" * 70)

print(
    "\nReal profile (NOT touched):"
)

print(f"  {PROFILE}")

print(
    "\nTemporary empty profile:"
)

print(f"  {temporary_profile}")

if temporary_profile == PROFILE:

    raise SystemExit(
        "Refusing to run: the temporary profile "
        "resolved to your real profile."
    )


try:

    with sync_playwright() as p:

        context = p.chromium.launch_persistent_context(
            user_data_dir=str(temporary_profile),
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
            "\nOpening Available Bank Duties "
            "with no session..."
        )

        page.goto(
            SHIFTS_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(5000)

        # ----------------------------------------------------
        # What did Allocate actually give us?
        # ----------------------------------------------------

        print("\n" + "=" * 70)
        print("WHAT AN UNAUTHENTICATED VISITOR SEES")
        print("=" * 70)

        print(
            "\nFinal URL:"
        )

        print(f"  {page.url}")

        print(
            "\nPage title:"
        )

        print(f"  {page.title()}")

        body = page.locator(
            "body"
        ).inner_text()

        print(
            "\nBody text (first 1500 characters):"
        )

        print("-" * 70)

        print(body[:1500])

        print("-" * 70)

        # ----------------------------------------------------
        # Which of our markers matched?
        # ----------------------------------------------------

        lowered = body.lower()

        print("\n" + "=" * 70)
        print("MARKER ANALYSIS")
        print("=" * 70)

        print(
            "\nLOGIN_MARKERS we search for:"
        )

        matched = []

        for marker in LOGIN_MARKERS:

            hit = marker in lowered

            print(
                f"  {'✅' if hit else '  '} "
                f"{marker!r}"
            )

            if hit:
                matched.append(marker)

        print(
            "\nGuard phrases that would SUPPRESS "
            "detection:"
        )

        for phrase in (
            "available bank duties",
            "personal roster",
        ):

            hit = phrase in lowered

            print(
                f"  {'⚠️' if hit else '  '} "
                f"{phrase!r}"
            )

        # ----------------------------------------------------
        # Does the detector fire?
        # ----------------------------------------------------

        detected = looks_like_login_page(page)

        print("\n" + "=" * 70)
        print("DETECTION RESULT")
        print("=" * 70)

        if detected:

            print(
                "\n✅ looks_like_login_page() "
                "returned True."
            )

            print(
                "Matched marker(s):",
                ", ".join(matched)
            )

        else:

            print(
                "\n❌ looks_like_login_page() "
                "returned False."
            )

            print(
                "The monitor would report a "
                "confusing timeout instead of a "
                "session-expiry message."
            )

            print(
                "\nPick a distinctive phrase from "
                "the body text above and add it to "
                "LOGIN_MARKERS in "
                "monitor_booking_once.py."
            )

        # ----------------------------------------------------
        # Now exercise the real code path.
        #
        # This waits up to 60 seconds for a JSON response that
        # will never arrive, then should raise SessionExpired.
        # ----------------------------------------------------

        print("\n" + "=" * 70)
        print("FULL CODE PATH")
        print("=" * 70)

        print(
            "\nCalling scan_available_shifts()..."
        )

        print(
            "This waits up to 60 seconds for a "
            "response that will never come."
        )

        try:

            scan_available_shifts(page)

            print(
                "\n❓ UNEXPECTED: the scan "
                "succeeded. Allocate may have "
                "served data without a session."
            )

        except SessionExpired as exc:

            print(
                "\n✅ SessionExpired was raised "
                "correctly."
            )

            print(
                "\nThe message your monitor "
                "would show:"
            )

            print("-" * 70)

            print(exc)

            print("-" * 70)

        except Exception as exc:

            print(
                "\n❌ The wrong exception was "
                "raised:"
            )

            print(
                f"  {type(exc).__name__}: {exc}"
            )

            print(
                "\nThis is the bug this test "
                "exists to catch. The monitor "
                "would treat a dead session as a "
                "transient fault and retry "
                "forever."
            )

        input(
            "\nPress ENTER to close..."
        )

        context.close()

finally:

    # --------------------------------------------------------
    # Always clean up the temporary profile.
    # --------------------------------------------------------

    shutil.rmtree(
        temporary_profile,
        ignore_errors=True,
    )

    print(
        "\nTemporary profile deleted."
    )

    print(
        "Your saved session is untouched."
    )
