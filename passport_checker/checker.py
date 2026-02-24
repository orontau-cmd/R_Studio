#!/usr/bin/env python3
"""
Passport Appointment Checker – govisit.gov.il (Tel Aviv)

NO LOCAL SETUP NEEDED.  Everything runs on GitHub Actions.

ONE-TIME GITHUB SECRETS SETUP
==============================
Go to: Repository → Settings → Secrets and variables → Actions

  TELEGRAM_BOT_TOKEN  – create a bot via @BotFather on Telegram, copy the token
  TELEGRAM_CHAT_ID    – your personal chat ID (message @userinfobot to get it)
  PASSPORT_PHONE      – your Israeli phone number, e.g. 0501234567
  PASSPORT_ID         – your Israeli ID number (תעודת זהות) — omit if not required
  APPOINTMENT_URL     – the URL of the Tel Aviv passport appointment calendar page
                        (log in manually once in your browser, navigate to the
                        calendar, and copy the URL from the address bar)
  GMAIL_USER          – optional, e.g. youraddress@gmail.com
  PASSPORT_GMAIL_APP_PASSWORD – optional, 16-char Google App Password

HOW IT WORKS
============
1. GitHub Actions runs this script daily at 07:00 Israel time.
2. The script restores a cached login session (from the previous run).
   If the session is still valid → checks for slots immediately (no SMS).
3. If the session has expired:
     a. Navigates to the login page and enters your phone number.
     b. The site sends an SMS to your phone.
     c. The bot messages you on Telegram: "Reply with your SMS code".
     d. You reply → the script logs in, saves the new session, checks slots.
4. If a Tel Aviv slot is found → instant Telegram message + optional email.
"""

import asyncio
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.async_api import async_playwright, BrowserContext, Page

# ---------------------------------------------------------------------------
# Configuration – all values come from environment / GitHub Secrets
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "session.json"
STATE_FILE = SCRIPT_DIR / "known_slots.json"

LOGIN_URL = "https://govisit.gov.il/he/app/auth/login"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PASSPORT_PHONE = os.environ.get("PASSPORT_PHONE", "")
PASSPORT_ID = os.environ.get("PASSPORT_ID", "")
APPOINTMENT_URL = os.environ.get("APPOINTMENT_URL", "")

NOTIFY_EMAIL = "oronroi@gmail.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("PASSPORT_GMAIL_APP_PASSWORD", "")

OTP_WAIT_SECONDS = 300   # 5 minutes to reply in Telegram
RE_ALERT_HOURS = 4       # don't re-alert for the same slot within this window

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Telegram helpers  (synchronous – called from outside the async event loop)
# ---------------------------------------------------------------------------

def _tg(method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=kwargs, timeout=10)
    return resp.json()


def tg_send(text: str) -> None:
    """Send a plain-text message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram] {text}")
        return
    result = _tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")
    if not result.get("ok"):
        print(f"Telegram sendMessage failed: {result}")


def tg_send_photo(path: str, caption: str = "") -> None:
    """Send a screenshot to Telegram for debugging."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(path, "rb") as f:
            requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=15,
            )
    except Exception as exc:
        print(f"Telegram photo upload failed: {exc}")


def tg_get_update_id() -> int:
    """Return the latest update_id so we can ignore old messages."""
    result = _tg("getUpdates", limit=1)
    updates = result.get("result", [])
    return updates[-1]["update_id"] if updates else 0


def tg_wait_for_reply(after_update_id: int, timeout: int = OTP_WAIT_SECONDS) -> str | None:
    """
    Long-poll Telegram for a new message from the user.
    Returns the message text, or None if timeout expires.
    """
    offset = after_update_id + 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = int(deadline - time.monotonic())
        wait = min(30, remaining)
        if wait <= 0:
            break
        try:
            result = _tg(
                "getUpdates",
                offset=offset,
                timeout=wait,
                allowed_updates=["message"],
            )
        except Exception as exc:
            print(f"Telegram poll error: {exc}")
            time.sleep(3)
            continue

        for update in result.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) == str(TELEGRAM_CHAT_ID):
                text = msg.get("text", "").strip()
                if text:
                    return text
    return None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def load_session() -> dict | None:
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            print("Session file found.")
            return data
        except json.JSONDecodeError:
            print("Session file corrupt – will re-login.")
    return None


def save_session(data: dict) -> None:
    SESSION_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("Session saved.")


# ---------------------------------------------------------------------------
# State helpers (avoid duplicate slot alerts)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"alerted_slots": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def already_alerted(state: dict, key: str) -> bool:
    alerted = state.get("alerted_slots", {})
    ts_str = alerted.get(key)
    if not ts_str:
        return False
    try:
        alerted_at = datetime.fromisoformat(ts_str)
        hours_since = (datetime.now(timezone.utc) - alerted_at).total_seconds() / 3600
        return hours_since < RE_ALERT_HOURS
    except ValueError:
        return False


def mark_alerted(state: dict, key: str) -> None:
    state.setdefault("alerted_slots", {})[key] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Login automation
# ---------------------------------------------------------------------------

async def _fill_first_match(page: Page, selectors: list[str], value: str) -> bool:
    """Try each selector in order; fill the first visible one. Returns True on success."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_first_match(page: Page, selectors: list[str]) -> bool:
    """Click the first visible matching element."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                return True
        except Exception:
            continue
    return False


async def do_login(page: Page) -> bool:
    """
    Automate the govisit.gov.il login flow.
    Returns True on success, False if something went wrong.

    Flow:
      1. Navigate to login page
      2. Enter phone (+ optional ID number)
      3. Submit → site sends SMS
      4. Bot asks user for the code via Telegram
      5. User replies → enter code → submit
      6. Verify we are no longer on the login page
    """
    print("  Navigating to login page…")
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

    # ── Step 1: fill phone number ──────────────────────────────────────────
    phone_selectors = [
        "input[type='tel']",
        "input[name*='phone']",
        "input[name*='mobile']",
        "input[placeholder*='טלפון']",
        "input[placeholder*='נייד']",
        "input[id*='phone']",
        "input[id*='mobile']",
    ]
    if not await _fill_first_match(page, phone_selectors, PASSPORT_PHONE):
        print("  ERROR: could not find phone input field.")
        screenshot_path = str(SCRIPT_DIR / "login_error.png")
        await page.screenshot(path=screenshot_path)
        tg_send_photo(screenshot_path, "Could not find phone input – check screenshot")
        return False

    # ── Step 2: fill ID number if present and configured ──────────────────
    if PASSPORT_ID:
        id_selectors = [
            "input[name*='id']",
            "input[placeholder*='תעודת זהות']",
            "input[placeholder*='ת.ז']",
            "input[id*='idNumber']",
            "input[id*='identity']",
        ]
        await _fill_first_match(page, id_selectors, PASSPORT_ID)

    # ── Step 3: click send-OTP / submit button ────────────────────────────
    submit_selectors = [
        "button[type='submit']",
        "button:has-text('שלח')",
        "button:has-text('כניסה')",
        "button:has-text('המשך')",
        "button:has-text('אישור')",
        "button:has-text('Send')",
        "input[type='submit']",
    ]
    if not await _click_first_match(page, submit_selectors):
        print("  ERROR: could not find submit button.")
        return False

    # Give the site a moment to process the submission
    await page.wait_for_timeout(2_000)

    # ── Step 3b: answer pre-appointment questions if they appear ──────────
    await answer_pre_appointment_questions(page)

    # ── Step 4: ask user for OTP via Telegram ─────────────────────────────
    tg_send(
        f"SMS code sent to <b>{PASSPORT_PHONE}</b>.\n\n"
        f"Reply here with the 6-digit code within 5 minutes."
    )

    update_id = tg_get_update_id()
    print("  Waiting for Telegram OTP reply…")
    otp_code = tg_wait_for_reply(update_id, timeout=OTP_WAIT_SECONDS)

    if not otp_code:
        tg_send("No code received within 5 minutes. Skipping today's check.")
        print("  OTP timeout.")
        return False

    # Strip any non-digit characters (in case user types "code: 123456")
    digits_only = "".join(c for c in otp_code if c.isdigit())
    if not digits_only:
        tg_send(f"Received '{otp_code}' but found no digits. Please re-run manually.")
        return False

    print(f"  OTP received: {digits_only}")

    # ── Step 5: enter OTP ─────────────────────────────────────────────────
    otp_selectors = [
        "input[autocomplete='one-time-code']",
        "input[name*='otp']",
        "input[name*='code']",
        "input[placeholder*='קוד']",
        "input[type='number'][maxlength]",
        "input[inputmode='numeric']",
    ]
    if not await _fill_first_match(page, otp_selectors, digits_only):
        print("  ERROR: could not find OTP input field.")
        screenshot_path = str(SCRIPT_DIR / "otp_error.png")
        await page.screenshot(path=screenshot_path)
        tg_send_photo(screenshot_path, "Could not find OTP input – check screenshot")
        return False

    # ── Step 6: submit OTP ────────────────────────────────────────────────
    await _click_first_match(page, submit_selectors)
    await page.wait_for_timeout(3_000)

    # ── Step 7: confirm we are logged in ─────────────────────────────────
    if "login" in page.url or "auth" in page.url:
        print("  Login appears to have failed (still on auth page).")
        screenshot_path = str(SCRIPT_DIR / "login_failed.png")
        await page.screenshot(path=screenshot_path)
        tg_send_photo(screenshot_path, "Login failed – check screenshot")
        return False

    print(f"  Logged in. URL: {page.url}")
    return True


# ---------------------------------------------------------------------------
# Pre-appointment questionnaire (answered automatically after ID submission)
# ---------------------------------------------------------------------------

async def answer_pre_appointment_questions(page: Page) -> None:
    """
    After submitting phone + ID, govisit.gov.il shows up to three questions
    before sending the OTP.  This function detects and answers them:

      Q1. Are any changes required to personal details?  → No changes
      Q2. Which document would you like to renew?        → Passport
      Q3. What is the reason for renewing your passport? → About to Expire

    Runs in a loop until no matching question is found on the page.
    """
    QUESTIONS = [
        (
            ["שינויים בפרטים", "שינויים הנדרשים", "personal details",
             "identity card supplement", "פרטים אישיים"],
            ["אין שינויים", "No changes"],
        ),
        (
            ["מסמך ברצונך לחדש", "איזה מסמך", "Which document",
             "document would you like"],
            ["דרכון", "Passport"],
        ),
        (
            ["סיבה לחידוש", "reason for renewing", "סיבת החידוש",
             "renew your passport", "עומד לפוג תוקף"],
            ["עומד לפוג", "About to Expire", "פג תוקף"],
        ),
    ]
    continue_selectors = [
        "button:has-text('המשך')",
        "button:has-text('הבא')",
        "button:has-text('אישור')",
        "button[type='submit']",
        "button:has-text('Continue')",
        "button:has-text('Next')",
    ]

    for _attempt in range(8):  # up to 8 rounds covers 3 questions with retries
        await page.wait_for_timeout(1_500)
        body_text = (await page.inner_text("body")).lower()

        answered_this_round = False
        for q_keywords, a_options in QUESTIONS:
            if not any(kw.lower() in body_text for kw in q_keywords):
                continue
            for answer in a_options:
                try:
                    el = await page.query_selector(
                        f"button:has-text('{answer}'), "
                        f"label:has-text('{answer}'), "
                        f"[role='radio']:has-text('{answer}'), "
                        f"[class*='option']:has-text('{answer}')"
                    )
                    if el and await el.is_visible():
                        await el.click()
                        print(f"  Pre-question answered → '{answer}'")
                        answered_this_round = True
                        await page.wait_for_timeout(600)
                        break
                except Exception:
                    continue

        if not answered_this_round:
            break  # no more questions on the page

        # Advance to the next step / question
        await _click_first_match(page, continue_selectors)
        await page.wait_for_timeout(1_500)


# ---------------------------------------------------------------------------
# Session validity check
# ---------------------------------------------------------------------------

async def is_session_valid(page: Page) -> bool:
    """Return True if we're logged in (not redirected to login/auth page)."""
    if "auth" in page.url or "login" in page.url:
        return False
    login_el = await page.query_selector(
        "input[type='tel'], input[name*='phone'], [class*='login-form']"
    )
    return login_el is None


# ---------------------------------------------------------------------------
# Appointment slot detection
# ---------------------------------------------------------------------------

async def find_available_slots(page: Page) -> list[dict]:
    """
    Scan the appointment calendar for available slots.
    Returns a list of {"date": str, "time": str, "label": str} dicts.

    Uses multiple selector strategies since we cannot inspect the live DOM
    in advance. The screenshot artifact in GitHub Actions can help you
    identify which selectors need adjustment for this specific site.
    """
    slots: list[dict] = []

    # Strategy 1 – enabled calendar day cells
    available_cells = await page.query_selector_all(
        ".day:not(.disabled):not(.unavailable):not(.empty), "
        "[class*='slot']:not([class*='disabled']):not([class*='unavailable']), "
        "[class*='available']:not([class*='un']), "
        "td.active:not(.disabled), "
        "button[data-date]:not([disabled]):not([class*='disabled'])"
    )
    print(f"  Strategy 1 (calendar cells): {len(available_cells)} candidate(s)")

    for cell in available_cells:
        try:
            date_val = (
                await cell.get_attribute("data-date")
                or await cell.get_attribute("data-value")
                or await cell.get_attribute("aria-label")
                or await cell.inner_text()
            )
            label = (await cell.inner_text()).strip()
            if date_val:
                slots.append({"date": date_val.strip(), "time": "", "label": label})
        except Exception:
            continue

    if slots:
        return slots

    # Strategy 2 – time-slot buttons
    time_buttons = await page.query_selector_all(
        "button[class*='time']:not([disabled]), "
        "[class*='hour']:not([class*='disabled']), "
        "[class*='timeslot']:not([class*='full']):not([class*='taken'])"
    )
    print(f"  Strategy 2 (time buttons): {len(time_buttons)} candidate(s)")
    for btn in time_buttons:
        try:
            label = (await btn.inner_text()).strip()
            if label:
                slots.append({"date": "", "time": label, "label": label})
        except Exception:
            continue

    if slots:
        return slots

    # Strategy 3 – Hebrew availability keywords in page text
    body_text = await page.inner_text("body")
    availability_keywords = ["פגישות פנויות", "תור פנוי", "זמן פנוי", "available"]
    if any(kw.lower() in body_text.lower() for kw in availability_keywords):
        slots.append({
            "date": "–",
            "time": "–",
            "label": "Availability text detected on page – book manually",
        })

    return slots


# ---------------------------------------------------------------------------
# Main check orchestration
# ---------------------------------------------------------------------------

async def run() -> None:
    print("=== Passport Appointment Checker ===")
    print(f"Run time: {datetime.now(timezone.utc).isoformat()}\n")

    if not APPOINTMENT_URL:
        msg = (
            "APPOINTMENT_URL secret is not set.\n"
            "Log into govisit.gov.il in your browser, navigate to the Tel Aviv "
            "passport appointment calendar, and copy the URL. Add it as a GitHub "
            "Secret named APPOINTMENT_URL."
        )
        print(f"ERROR: {msg}")
        tg_send(f"Passport checker error: {msg}")
        sys.exit(1)

    session_data = load_session()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # ── Try cached session ────────────────────────────────────────────
        if session_data:
            print("Trying cached session…")
            context = await browser.new_context(
                user_agent=_BROWSER_UA,
                storage_state=session_data,
            )
            page = await context.new_page()
            await page.goto(APPOINTMENT_URL, wait_until="networkidle", timeout=45_000)
            await answer_pre_appointment_questions(page)

            if not await is_session_valid(page):
                print("Cached session has expired.")
                await context.close()
                session_data = None
            else:
                print("Session valid.")
        else:
            context = None
            page = None

        # ── Login if needed ────────────────────────────────────────────────
        if not session_data:
            print("Logging in via Telegram OTP flow…")
            if context:
                await context.close()
            context = await browser.new_context(user_agent=_BROWSER_UA)
            page = await context.new_page()

            success = await do_login(page)
            if not success:
                await browser.close()
                sys.exit(1)

            # Navigate to appointment page after fresh login
            print(f"  Navigating to appointment page…")
            await page.goto(APPOINTMENT_URL, wait_until="networkidle", timeout=45_000)
            await answer_pre_appointment_questions(page)

            # Save new session
            new_session = await context.storage_state()
            save_session(new_session)

        # ── Take debug screenshot ──────────────────────────────────────────
        screenshot_path = SCRIPT_DIR / "last_check.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"  Screenshot saved: {screenshot_path}")

        # ── Check for available slots ──────────────────────────────────────
        print("Checking for available slots…")
        slots = await find_available_slots(page)
        print(f"  Slots detected: {len(slots)}")

        await browser.close()

    # ── Notify ────────────────────────────────────────────────────────────
    if not slots:
        print("No available slots found. Done.")
        return

    state = load_state()
    new_slots = []
    for slot in slots:
        key = f"{slot.get('date', '')}|{slot.get('time', '')}"
        if not already_alerted(state, key):
            new_slots.append(slot)
            mark_alerted(state, key)
    save_state(state)

    if not new_slots:
        print(f"Found {len(slots)} slot(s), already alerted recently – skipping.")
        return

    print(f"\nFound {len(new_slots)} new slot(s)! Sending alerts…")
    for s in new_slots:
        print(f"  {s.get('date') or '?'} {s.get('time') or ''} – {s.get('label', '')}")

    notify_telegram(new_slots)
    notify_email(new_slots)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_telegram(slots: list[dict]) -> None:
    lines = ["Passport appointment slot available in Tel Aviv!", ""]
    for s in slots:
        date = s.get("date") or "–"
        time_ = s.get("time") or ""
        label = s.get("label") or ""
        lines.append(f"  {date} {time_}  {label}".strip())
    lines += [
        "",
        f'<a href="{LOGIN_URL}">Book now on govisit.gov.il</a>',
    ]
    tg_send("\n".join(lines))


def _build_email_html(slots: list[dict]) -> str:
    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 14px;border-bottom:1px solid #e8e8e8'>{s.get('date') or '–'}</td>"
        f"<td style='padding:8px 14px;border-bottom:1px solid #e8e8e8'>{s.get('time') or '–'}</td>"
        f"<td style='padding:8px 14px;border-bottom:1px solid #e8e8e8'>{s.get('label') or '–'}</td>"
        f"</tr>"
        for s in slots
    )
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;color:#1a1a1a;max-width:640px;margin:0 auto;padding:20px">
  <h2 style="color:#16a34a">Passport appointment available in Tel Aviv!</h2>
  <p style="color:#555">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
  <p><a href="{LOGIN_URL}"
        style="background:#16a34a;color:#fff;padding:10px 20px;border-radius:6px;
               text-decoration:none;display:inline-block;margin-bottom:16px">
    Book now on govisit.gov.il
  </a></p>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <thead>
      <tr style="background:#1e293b;color:#fff;text-align:left">
        <th style="padding:10px 14px">Date</th>
        <th style="padding:10px 14px">Time</th>
        <th style="padding:10px 14px">Details</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""


def notify_email(slots: list[dict]) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Passport appointment available – Tel Aviv ({len(slots)} slot(s))"
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(_build_email_html(slots), "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"Email sent to {NOTIFY_EMAIL}")
    except Exception as exc:
        print(f"Email send failed: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run())
