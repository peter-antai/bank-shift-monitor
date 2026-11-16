# Engineering Notes

Design decisions, the bugs that mattered, and what the failures changed. Written for anyone rebuilding this, or evaluating how it was built.

---

## 1. Getting at the data

### The first approach was wrong

Loading the shifts URL directly gave a blank page. The obvious diagnosis was authentication, and the obvious fix was to scrape the rendered table once logged in.

Watching the network told a better story. Allocate Loop is a Kendo UI application that renders nothing server-side and fetches everything over XHR. Two endpoints carry all the meaning:

```
POST .../Roster/BankShifts/UnfilledShifts        vacant duties
POST .../Roster/PersonalRoster/GetApprovedRoster booked duties
```

Both return clean JSON with typed fields. Intercepting them replaced fragile DOM parsing with something structural:

```python
def is_unfilled_shifts(response):
    return (
        "UnfilledShifts" in response.url
        and response.request.method == "POST"
        and response.status == 200
    )

with page.expect_response(is_unfilled_shifts, timeout=60000) as info:
    page.goto(SHIFTS_URL, wait_until="domcontentloaded")

data = info.value.json()
```

**Lesson:** before parsing what a page renders, look at what it fetched. A single-page application has already done the work of turning HTML into structured data, and that structure is a far more stable contract than its markup.

### Pagination that Playwright would not click

The next-page control existed in the DOM but Playwright's actionability checks refused to click it: Kendo positions those controls so they fail standard visibility tests.

Bypassing Playwright's checks and invoking the page's own handler worked:

```python
next_link.first.evaluate("(element) => element.click()")
```

Paired with `expect_response()`, this triggers Allocate's own JavaScript and waits for the resulting request. Termination is driven by the API's own count rather than by the UI:

```python
if len(all_duties) >= total:      # TotalItemsCount from the response
    break
```

Two guards protect that loop: an empty page breaks out even when the count disagrees, and a disabled next-link stops rather than raising. Any `while True` driven by a remote server's counter needs an escape hatch, because the server can be wrong.

### Week navigation, and a reload that ate the state

An early version reloaded the page between weeks to capture a fresh response. That reset the selected period back to the current week, so it scanned week one five times and reported the same duties as new.

The fix was to stop reloading and let the application manage its own state:

1. Capture the initial response
2. Collect its pagination pages
3. Click `#btnNext`
4. Capture the new response
5. Repeat

**Lesson:** in a stateful SPA, a reload is not a neutral refresh. It is a state reset.

---

## 2. The modelling decision that mattered most

Two data sources describe two different things:

| Source | Describes |
|---|---|
| `UnfilledShifts` | Opportunities that exist |
| `GetApprovedRoster` | Commitments that have been made |

An early implementation blurred them by maintaining a projected count that incremented as alerts were sent. Three available shifts in one week produced "would be booking #5", then #6, then #7 — none of which was true, because sending an alert books nothing.

The corrected rule is absolute: **available shifts never mutate the booking count.** Every alert reads the live number from the approved roster. Three shifts in a four-booked week are all reported as potential booking #5, because until a human books one, that is exactly what each of them is.

```python
booking_count = weekly_counts.get(week_start)   # read, never incremented
message = build_alert(duty, booking_count)
```

**Lesson:** when two datasets describe different kinds of fact, keep them separate all the way through. Blending them produces output that is fluent, specific, and wrong — the most expensive kind of wrong, because it looks authoritative.

---

## 3. The bug that would have broken every real alert

`get_booking_counts` was defined twice in the same module. In Python `def` is an assignment, so the second definition silently replaced the first with no warning.

The surviving version returned `week -> list of duties`. The caller expected `week -> int`:

```python
elif booking_count < WEEKLY_LIMIT:   # list < int → TypeError
```

Every test passed, because the tests only ran when nothing new was found. The first genuinely new shift would have raised `TypeError`, been caught by the loop's generic handler, and retried every ten minutes forever — with `notified` never set, so it would fail identically each time.

The fix was deletion. The lasting fix was a test that checks the *shape* of the return value, not just its content:

```python
bad = {w: v for w, v in booking_counts.items() if not isinstance(v, int)}
if bad:
    print("get_booking_counts() returned lists, not counts.")
```

**Lesson:** duplicate function definitions fail silently in Python, and the survivor is whichever sits lower in the file. "Keep the old one just in case" is dangerous for functions in a way it is not for data. Verifying the shape of a return value catches a class of bug that value-checking misses entirely.

---

## 4. Testing a failure that will not happen on demand

The session-expiry path was written but never exercised. Two attempts to trigger it failed, and both were informative.

### Attempt one: log out

Logging out of Loop and navigating back logged straight back in with no prompt. There are two sessions, not one: the Loop application cookie, and my employer's identity provider session. Logging out kills the first; the IdP silently issues a new one.

That was good news for durability and useless for testing.

### Attempt two: an empty profile

The test that worked removed the *precondition* for success rather than trying to undo it. A throwaway Chromium profile in a temp directory has no cookies, so the server must serve whatever it serves a stranger:

```python
temporary_profile = Path(tempfile.mkdtemp(prefix="allocate-session-test-"))
```

The real profile is never opened, and the temp directory is removed in a `finally` block.

### What it found

Allocate serves an unauthenticated visitor an **empty JavaScript shell**. Same URL, no redirect. No title. Zero characters of body text. No login form at all.

Every marker in the detection code was a guess about login pages, and every one was wrong.

### The rewrite

The original code asked "does this look like a login page?", which requires enumerating every way the site can fail. The rewrite asks "does this look like the application?", which is one known thing:

```python
if any(marker in lowered for marker in APP_MARKERS):
    return False, "the application is still loaded"

if len(body) < MINIMUM_PAGE_TEXT:
    return True, f"Allocate returned an empty page ({len(body)} characters of text)"
```

Order matters. The success check runs first, so a genuine application error with a healthy session is never misread as expiry — otherwise the monitor would stop for thirty minutes over a fault that would have cleared itself.

**Lessons:**

- Code that only runs when things go wrong is code that never gets tested. Manufacture the failure in a disposable environment.
- To test a failure path, ask what the code needs to succeed and withhold it. Here it was cookies.
- Recognising success beats enumerating failure. You know exactly what working looks like; you cannot know every way something breaks.

---

## 5. Failures are not interchangeable

The original loop had one `except Exception`: alert once, wait ten minutes, retry. Correct for a flaky network. Useless for an expired session, which will still be expired in ten minutes, and in ten hours.

The loop now sorts outcomes three ways:

| Outcome | Response |
|---|---|
| Success | Clear error state, wait 10 min + jitter |
| `SessionExpired` | Alert with instructions, retry every 5 min, stop after 6 attempts |
| Anything else | Alert once, retry on schedule, continue indefinitely |

A dedicated exception type carries the distinction across the module boundary:

```python
class SessionExpired(RuntimeError):
    """The saved Allocate session is no longer valid."""
```

Two details that matter more than they look:

**Recovery uses the window that is already open.** With a visible browser, logging in manually restores the session in the shared profile and the next retry succeeds. The monitor then sends a "working again" message. The alert text changes based on `HEADLESS`, because that advice is wrong when there is no window to log into.

**Giving up is a feature.** After six failures the monitor sends a final message saying it has stopped and exits. A silently dead monitor is indistinguishable from a healthy one with nothing to report, and that false quiet is the worst possible failure mode for an alerting tool.

**Lesson:** a timeout is a symptom, not a diagnosis. Ask why before reporting it. The same timeout can mean "network slow" or "you have been logged out", and those need opposite responses.

---

## 6. Filters decide relevance; messages carry nuance

The original filter required `canBookBankPosting is True`. Shifts needing a Staff Bank phone call were discarded.

That conflated two different questions: *is this the kind of shift I want?* and *can I act on it right now?* Folding the second into the filter meant the answer was silence, and silence is indistinguishable from "nothing available".

Now the filter decides relevance only. Bookability is reported in the message, where it combines with the weekly limit into four distinct actions:

| Self-bookable | Within limit | Action |
|---|---|---|
| Yes | Yes | Book it in Loop |
| Yes | No | Loop allows it, but call Staff Bank first |
| No | Yes | Call Staff Bank to request it |
| No | No | Call Staff Bank; over limit and not self-bookable |

**Lesson:** filters answer "is this relevant". Everything else belongs in the output, where a human can see it and judge.

---

## 7. Rules should fit the constraint, not a template

The weekday and weekend rules have different shapes because the underlying constraints differ:

```python
if end_minutes <= start_minutes:              # night: wrapped past midnight
    return date.weekday() in NIGHT_DAYS and start_minutes >= night_floor

if date.weekday() >= 5:                       # weekend: containment
    return start >= window_start and end <= window_end

return weekday_earliest <= start_minutes <= weekday_latest   # weekday: start only
```

On weekdays the constraint is when the shift starts, so the end time is not tested at all. That allows irregular finishes like 07:15–15:45, which the unit posts regularly. At weekends the constraint is the span of the day being committed, so containment is the right test. Nights are judged on the day they start, so a Friday night finishing Saturday morning counts as Friday.

Times are compared as minutes past midnight rather than `(hour, minute)` tuples, which turns four-value comparisons into arithmetic. It also makes the overnight case fall out for free: if `end <= start`, the shift wrapped into the next day.

**Lesson:** resist forcing every rule into one shape. Three constraints of genuinely different kinds deserve three tests. And choosing a representation that makes the awkward case obvious is worth more than clever comparison logic.

---

## 8. Building the tool that tells you the filter is right

A monitor reporting zero matches gives identical output whether the filter is correct or subtly broken. `near_miss_report.py` closes that gap: it shows every duty at the unit with the reason it was accepted or rejected.

```
REJECTED, GROUPED BY REASON

12 × night shift, not Fri/Sat/Sun
32 × weekday, starts after 11:00
```

The report contains a second, hand-written implementation of the filter that explains rather than decides. A hand-written mirror can drift from the thing it mirrors, so the report ends by running the production filter over the same data and comparing counts:

```
This report accepted: 10
is_target_shift() accepted: 10
✅ The report agrees with the monitor's own filter.
```

**Lesson:** a diagnostic that lies is worse than no diagnostic, because you trust it and chase the wrong bug. Any second implementation built for reporting should check itself against the first.

### What it revealed

Across 54 vacant duties over two weeks:

| Pattern | Count |
|---|---|
| 14:00–22:00 and other late starts | 32 |
| 22:00–06:00 nights | 22 |
| 06:00–11:00 starts (the original target) | 0 |

The unit runs an early/late/night rotation, and the early shift is the one nobody gives up. The original filter was working perfectly and would have found nothing for weeks.

It also exposed a rule that had never been chosen deliberately. The `end <= start` check was added as a technical safeguard for the weekend window logic, and quietly became policy: it excluded 22 night shifts, including Friday and Saturday nights carrying both weekend and night enhancements. A safeguard had become a product decision without anyone deciding.

**Lesson:** technical guards can silently become policy. Reviewing what a system rejects is as important as reviewing what it accepts.

---

## 9. Measurement that was correctly abandoned

The plan was to record shift survival times and tune the poll interval to evidence. I dropped it after one observation: shifts are found by someone who tells their colleagues. Survival time is a social process, not a property of the shift.

Measuring it would have produced a number with no predictive value, plus a growing database of dead shifts.

**Lesson:** "instrument, then optimise" is good advice with a precondition — the thing must be a stable property. Measuring a social process yields precision without accuracy. Recognising that saved a schema change, a report, and weeks of collection.

---

## 10. Deployment

### Docker, not Kubernetes

One always-on process with one persistent volume. Kubernetes exists to schedule many replicas across many machines; here it would add an API server, manifests and a control plane to supervise a single Python loop. Compose provides restart-on-crash and start-on-boot in about fifteen lines.

### The session is the hard part

Everything else containerises cleanly. But the session comes from a human completing SSO and MFA in a visible browser, and a headless server has nobody to do that.

The chosen trade: create the profile interactively on a laptop, carry it across as a volume. One manual step, repeated only when the session expires. The alternative — storing NHS credentials so the container can log itself in — trades a rare manual step for a permanent credential store, which is worse.

### Two details that cost time if missed

```yaml
shm_size: "1gb"     # Chromium crashes on Docker's 64MB default
TZ: Europe/London   # pruning uses the local date; UTC disagrees each evening
```

### Git and rsync want opposite things

`data/` and `.env` are excluded from git and included in the rsync. The repository carries code and history; the rsync carries a working deployment. Same two paths, opposite treatment, both correct.

---

## 11. Security

The browser profile contains a live authenticated session to an NHS workforce system. Not a password — something better, because it is already past MFA.

Controls: private repository, `.gitignore` covering `.env`, `data/`, `*.db` and `allocate-profile/`, verification via `git status` before the first commit and via the browser after the first push.

One incident during development: a bot token was exposed by running `cat .env` and pasting the output while asking for help. Response was revocation through @BotFather, which makes every copy inert everywhere.

**Lesson:** secrets leak through display, not only through storage. `.gitignore` protects a file from git and does nothing about `cat`, screenshots, screen sharing, or pasted terminal output. The useful reflex is asking whether a command's output would be safe in a screenshot. And revocation is the only remedy that reaches copies you cannot see.

---

## 12. What generalised

The technical specifics are unlikely to recur. These points are not specific to Allocate:

**Watch what an application fetches before parsing what it renders.**

**Keep different kinds of fact separate.** Availability and commitment blended into confident wrong answers.

**Test failure paths by withholding preconditions.** Waiting for a real failure means being asleep when it arrives.

**Recognise success rather than enumerating failure.** You know exactly what working looks like.

**Classify failures by whether a human is needed.** Retry what heals itself, escalate what does not, and never fail silently.

**Filters decide relevance; nuance belongs in the output.** Silence is the worst possible answer, because it is indistinguishable from "nothing to report".

**Review what a system rejects, not only what it accepts.** A safeguard became policy without anyone deciding.

**Verify the shape of return values, not just their content.** The duplicate-function bug passed every value-based test.

**Not everything worth knowing is worth measuring.** Precision without accuracy is a cost with no benefit.
