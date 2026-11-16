# Bank Shift Monitor for Allocate Loop

[![tests](https://github.com/peter-antai/bank-shift-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/peter-antai/bank-shift-monitor/actions/workflows/tests.yml)

Monitors an NHS staff bank's Allocate Loop system for newly available shifts at one unit, and sends a Telegram alert within ten minutes of one appearing.

It never books anything. Detection is automated; the booking decision stays with the person.

```
Ubuntu homelab (always on)
        │
   Docker container
        │
   Python monitor ──── SQLite (notification state)
        │
 Playwright / headless Chromium
        │
   Allocate Loop
        │
   ┌────┴────┐
   ▼         ▼
Available   Approved
 duties      roster
   └────┬────┘
        ▼
   New shift?
        ▼
    Telegram ──► phone
```

---

## The problem

Bank shifts at my unit are released without notice. There is no alerting in Allocate, no email, no feed. Desirable shifts spread by word of mouth and are gone before anyone checking manually finds them.

Checking a web interface every ten minutes is not a realistic thing for a person to do. It is a trivial thing for a computer to do.

A second problem sits underneath the first. The staff bank applies a limit of four bank shifts per Monday–Sunday week, and going beyond it requires Staff Bank approval. Knowing a shift exists is therefore only half an answer. The useful question is whether to take it, which needs the shift and the current week's booking count together in one message.

---

## What it does

Every ten minutes, with random jitter, the monitor:

1. Loads Available Bank Duties in a headless authenticated browser
2. Walks three weeks forward, following pagination to the end of each
3. Filters for shifts matching the target unit, grade and time rules
4. Checks SQLite so a shift is only ever alerted once
5. If anything is new, reads the approved roster for the live weekly booking count
6. Sends a Telegram alert combining the shift, the week's count, and how to book it

### Shift rules

| Type | Rule |
|---|---|
| Weekday | Starts 06:00–11:00, any finish |
| Weekend | Whole shift inside 06:00–22:00 |
| Night | Crosses midnight, starts Fri/Sat/Sun from 18:00 |

The two rule shapes are deliberate. On weekdays the constraint is when the shift *starts*, because late shifts are unwanted and the finish is irrelevant once the start is acceptable. At weekends the constraint is the *span*, because that is the portion of the day worth committing. Forcing both into one shape would have compromised one of them.

### Example alert

```
🌙 NEW SATURDAY NIGHT SHIFT

<SITE_NAME>
<TARGET_UNIT>
<TARGET_GRADE>

Saturday 12 September 2026
22:00–06:00 (overnight, finishes next day)

Week: 07 Sep–13 Sep 2026

⚡ WEEKEND NIGHT — ENHANCED RATE

Current bookings: 4/4

This would be booking #5.

🚨 STANDARD WEEKLY LIMIT REACHED

HOW TO BOOK
Loop will let you book this, but it exceeds your standard weekly limit.

📞 Contact <STAFF_BANK_NAME> before booking.

Self-booking: available
Request ID: 0826xxxxxx
Duty ID: 46xxxxxx

No automatic booking was performed.
```

Two independent facts drive that message: whether Loop permits self-booking, and whether the week is within limit. They combine into four distinct actions, and the alert states which one applies rather than leaving it to be worked out on a phone screen.

---

## Architecture

### JSON interception, not HTML scraping

Allocate Loop is a Kendo UI application that fetches its data over XHR. Rather than parsing rendered tables, the monitor intercepts the responses:

| Endpoint | Provides |
|---|---|
| `POST .../Roster/BankShifts/UnfilledShifts` | Currently vacant duties |
| `POST .../Roster/PersonalRoster/GetApprovedRoster` | Duties already booked |

Playwright's `expect_response()` waits for the request the page makes on its own, and the JSON is read directly. This survives styling changes, DOM restructuring and Kendo version bumps, none of which a table scraper would survive.

### Availability and booking are separate systems

The most important modelling decision in the project. `UnfilledShifts` describes opportunities; `GetApprovedRoster` describes commitments. They are never mixed.

Concretely: if three shifts appear in a week where four are already booked, all three are reported as "would be booking #5". The count is not incremented per alert, because sending an alert does not book anything. Only Allocate changing its approved roster changes the number.

An early version violated this by projecting counts forward, and produced confidently wrong advice.

### Failure classification

Not every failure deserves the same response. The live monitor separates two kinds:

| Failure | Response |
|---|---|
| Session expired | Alert with instructions, retry every 5 min, stop after 6 attempts |
| Anything else | Alert once, retry on the normal schedule, continue indefinitely |

The distinction is whether a human is required. Transient faults heal themselves, so retry quietly. An expired SSO session cannot heal itself, so ask for help and set a deadline. On giving up, the monitor sends a final message saying it has stopped.

That last part matters more than it appears. For a monitoring tool the dangerous state is false quiet, because a dead process looks exactly like a healthy one with nothing to report.

### Detecting a dead session

Allocate does not serve a login page to an unauthenticated visitor. It serves an empty JavaScript shell, at the same URL, with no title and no body text.

So detection does not try to recognise failure, which would mean enumerating every way the site can break. It recognises success, which is one known thing:

```python
if any(marker in body for marker in APP_MARKERS):
    return False, "the application is still loaded"

if len(body) < MINIMUM_PAGE_TEXT:
    return True, f"Allocate returned an empty page ({len(body)} characters)"
```

The success check comes first, so a genuine application error with a working session is never misdiagnosed as expiry and does not stop the monitor.

### State

SQLite, keyed on Allocate's `dutyId`, holding a `notified` flag. A shift is marked notified only after Telegram confirms delivery, so a failed send retries on the next pass rather than being lost.

Shifts whose date has passed are deleted each pass. They cannot be booked, so the rows have no further use, and the database stays roughly the size of what is currently actionable.

---

## Running it

### Requirements

Docker and Docker Compose on the host. A machine with a screen for the one-time login.

### 1. Create the session

Allocate authenticates through SSO with MFA. A headless server has nobody to complete that, so the browser profile is created interactively and then carried across.

```bash
git clone https://github.com/peter-antai/bank-shift-monitor.git
cd bank-shift-monitor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env    # then set ALLOCATE_TENANT in .env

mkdir -p data
ALLOCATE_HOME=./data python tools/login_loop.py
```

Log in, complete MFA, press ENTER once Available Bank Duties is visible. This writes `data/allocate-profile/`.

### 2. Configure

In `.env`, set `TARGET_UNIT` and `TARGET_GRADE` exactly as Loop displays them, then `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_ID`. To find the chat id, message your bot then run `ALLOCATE_HOME=./data python tools/get_chat_id.py`.

### 3. Verify before deploying

```bash
ALLOCATE_HOME=./data python monitor/monitor_booking_once.py
```

One complete pass, then exit. Confirms login, scanning, filtering and Telegram all work before any containers are involved.

### 4. Deploy

```bash
rsync -av --exclude .venv --exclude .git ./ user@server:~/allocate-monitor/
ssh user@server
cd ~/allocate-monitor
docker compose up -d --build
docker compose logs -f
```

`data/` and `.env` are excluded from git but included in the rsync. Git carries the code; rsync carries the deployment.

### Operating

```bash
docker compose logs -f          # watch
docker compose restart          # restart
docker compose down             # stop
docker compose up -d --build    # apply code changes
```

`restart: unless-stopped` handles crashes and host reboots.

### When the session expires

A Telegram message headed `🔐 ALLOCATE SESSION EXPIRED` arrives. Repeat step 1 on a machine with a screen, then:

```bash
rsync -av data/allocate-profile/ user@server:~/allocate-monitor/data/allocate-profile/
ssh user@server 'cd ~/allocate-monitor && docker compose restart'
```

---

## Testing

```bash
pip install -r requirements.txt pytest
pytest
```

The tests in `tests/` cover the logic that decides what I get told: the weekday, weekend and night rules, the wording of each alert against the weekly limit, the notification state in SQLite, and the start-up configuration check. They use made-up duties and a temporary database, so they need no login, no browser and no network. GitHub Actions runs them on every push.

The scripts in `tools/` are different: they are interactive checks that drive a real browser against a real session, so they are run by hand.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHAT_ID` | — | Required |
| `ALLOCATE_TENANT` | — | Required. Tenant code from your organisation's Loop URL |
| `TARGET_UNIT` | — | Required. Unit name exactly as Loop displays it |
| `TARGET_GRADE` | — | Required. Grade name exactly as Loop displays it |
| `SITE_NAME` | empty | Optional site line shown in alerts |
| `STAFF_BANK_NAME` | `Staff Bank` | Who the alert says to contact |
| `WEEKLY_LIMIT` | `4` | Standard bank shifts per Monday–Sunday week |
| `ALLOCATE_HOME` | `~/allocate-monitor` | Profile and database location |
| `ALLOCATE_HEADLESS` | `false` | `true` on a server |
| `POLL_SECONDS` | `600` | Seconds between checks |
| `JITTER_SECONDS` | `60` | Extra random delay per check |
| `WEEKS_TO_SCAN` | `3` | Weeks ahead to scan |

Shift time rules are constants at the top of `monitor/monitor_booking_once.py`, not environment variables. Environment variables are for what changes between machines. Shift rules are logic that gets reasoned about and tested, so they belong in version control where changes show up in diffs.

---

## Repository layout

```
monitor/
  monitor_booking_once.py    all logic, one pass, runs standalone
  allocate_monitor_live.py   polling loop, error handling, recovery
tools/
  login_loop.py              create or refresh the session
  near_miss_report.py        every duty at the unit, and why each was accepted or rejected
  session_expiry_test.py     prove expiry detection works, using a throwaway profile
  booking_logic_test.py      preview alerts and booking counts without sending
  get_chat_id.py             find your Telegram chat id
tests/
  conftest.py                test settings, no real account needed
  test_shift_rules.py        shift rules, alert wording, state, config check
docker/
  Dockerfile
data/                        gitignored: browser profile, SQLite database
```

The split between the two monitor files is deliberate. All logic lives in `monitor_booking_once.py`, which runs standalone as a single pass. `allocate_monitor_live.py` imports it and adds only polling and failure handling. Every piece of logic can therefore be tested in seconds instead of waiting out a ten-minute cycle.

### The diagnostic tool

`near_miss_report.py` earned its place fastest. A monitor reporting zero matches looks identical whether the filter is correct or silently broken. The report shows every duty at the unit with the reason it was accepted or rejected:

```
✅ MATCH  Fri 04 Sep 2026  22:00–06:00
          weekend night | can self-book: True | duty 46xxxxxx

REJECTED, GROUPED BY REASON
12 × night shift, not Fri/Sat/Sun
32 × weekday, starts after 11:00
```

It ends by running the production filter over the same data and comparing counts. A hand-written diagnostic can drift from the code it describes, and a diagnostic that lies is worse than none at all.

That output also produced the project's most useful finding. Across 54 vacant duties over two weeks, not one was a 06:00–14:00 early shift. The early shift is the one nobody gives up. The filter was working perfectly and finding nothing, and those two states are easy to confuse.

---

## Security

**Keep any fork with data in it private.** `data/allocate-profile/` holds a live authenticated staff-bank session. Anyone with it can act as you inside a hospital rostering system.

`.gitignore` covers `.env`, `data/`, `*.db` and `allocate-profile/`. Verify with `git status` before the first commit, and check the pushed repository in a browser afterwards. Intent and reality are different things.

Secrets leak through display as much as through storage. `.gitignore` protects a file from git; it does nothing about `cat`, screenshots, or pasting terminal output when asking for help. If a token is ever exposed, revoke it immediately, because revocation makes every copy inert and nothing else does.

### Responsible use

- Reads one person's own authorised account
- Books nothing; every booking is a manual human decision
- Polls every 10 minutes with jitter, well below the rate of a person refreshing a page
- Does not bypass authentication, MFA or any access control, and should not be extended to

Check your employer's acceptable-use policy. That judgement belongs to the operator.

---

## Known limitations

**Session expiry needs a human.** SSO with MFA cannot be automated without storing credentials, which would be worse. The manual re-login is a deliberate trade.

**Duty identity is Allocate's `dutyId`.** If a posting is withdrawn and recreated with a new id, it alerts again. A composite fingerprint of date, times, unit and grade would fix that, at the cost of missing genuinely distinct postings that share those fields.

**Detection latency is bounded by the poll interval.** Zero to eleven minutes, averaging five. Tightening it was considered and rejected: availability spreads by word of mouth, so survival time is a social process rather than a measurable property, and a shorter interval would multiply request volume for unquantifiable benefit.

**Three weeks only.** Allocate publishes about two weeks ahead. Weeks four and five returned zero on every run and were removed.

---

## Engineering notes

Longer write-up of the design decisions, the bugs, and what the failures taught: [ENGINEERING.md](ENGINEERING.md).

---

## Licence

MIT. See [LICENSE](LICENSE).

## Disclaimer

Independent personal project. Not affiliated with, endorsed by, or connected to Allocate Software, RLDatix or any NHS organisation. All trademarks belong to their respective owners.

Users are responsible for ensuring their use complies with applicable NHS policies, employer requirements and account terms.
