#!/usr/bin/env python3
"""
Passport Appointment Checker – govisit.gov.il (Tel Aviv)

HOW IT WORKS
============
1. You log in ONCE locally via --setup (a real browser window opens; enter the
   SMS code directly in the browser — no need to read it programmatically).
2. The session cookies are saved and encoded for GitHub Secrets.
3. GitHub Actions polls every 30 minutes using the saved session (no SMS needed).
4. When a Tel Aviv slot appears, an email is sent to oronroi@gmail.com.
5. When the session expires you get an alert email; re-run --setup locally.

FIRST-TIME SETUP (run locally once)
=====================================
  pip install -r requirements.txt
  playwright install chromium

  python checker.py --setup

  A browser window opens. You will:
    1. See the login page — enter your phone number and submit
    2. Receive an SMS — enter the code directly in the browser
    3. After login succeeds, navigate to the passport appointment page
       (Services → Passport → Select location → Tel Aviv → calendar)
    4. Once you can see the calendar/availability page, press Enter in
       the terminal to capture the session and current URL.

ENCODE SESSION FOR GITHUB ACTIONS
=====================================
  python checker.py --export

  Copy the printed value and add it as GitHub Secret: PASSPORT_SESSION
  In repository: Settings → Secrets and variables → Actions → New secret

REQUIRED GITHUB SECRETS
=====================================
  PASSPORT_SESSION   – output of: python checker.py --export
  GMAIL_USER         – e.g. youraddress@gmail.com
  GMAIL_APP_PASSWORD – 16-char Google App Password (not your main password)
"""

import argparse
import asyncio
import base64
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
SESSION_FILE = SCRIPT_DIR / "session.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"      # stores the appointment URL
STATE_FILE = SCRIPT_DIR / "known_slots.json"  # tracks already-alerted slots

LOGIN_URL = "https://govisit.gov.il/he/app/auth/login"

NOTIFY_EMAIL = "oronroi@gmail.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
PASSPORT_SESSION_B64 = os.environ.get("PASSPORT_SESSION", "")

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Hours before we re-alert for the same available slot
RE_ALERT_HOURS = 4


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def load_session_data() -> dict | None:
    """Load session from env var (GitHub Actions) or local file (dev)."""
    if PASSPORT_SESSION_B64:
        try:
            raw = base64.b64decode(PASSPORT_SESSION_B64).decode("utf-8")
            return json.loads(raw)
        except Exception as exc:
            print(f"ERROR: Could not decode PASSPORT_SESSION: {exc}")
            return None
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: session.json is corrupt.")
    return None


def save_session_file(data: dict) -> None:
    SESSION_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Session saved to {SESSION_FILE}")


async def capture_session(context: BrowserContext) -> dict:
    """Dump cookies + localStorage from the browser context."""
    return await context.storage_state()


# ---------------------------------------------------------------------------
# Config helpers (stores the appointment page URL)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_config(data: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# State helpers (tracks alerted slots to avoid duplicate emails)
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


def slot_key(date_str: str, time_str: str) -> str:
    return f"{date_str}|{time_str}"


def already_alerted(state: dict, key: str) -> bool:
    alerted = state.get("alerted_slots", {})
    if key not in alerted:
        return False
    alerted_at_str = alerted[key]
    try:
        alerted_at = datetime.fromisoformat(alerted_at_str)
        now = datetime.now(timezone.utc)
        hours_since = (now - alerted_at).total_seconds() / 3600
        return hours_since < RE_ALERT_HOURS
    except ValueError:
        return False


def mark_alerted(state: dict, key: str) -> None:
    state.setdefault("alerted_slots", {})[key] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# --setup mode: interactive login in a real browser window
# ---------------------------------------------------------------------------

async def run_setup() -> None:
    """
    Open a headed browser so the user can log in interactively (including
    entering the SMS code), navigate to the appointment calendar, then
    press Enter to save the session and the current URL.
    """
    print("=== SETUP MODE ===")
    print("A browser window will open.")
    print("1. Log in with your phone number and the SMS code.")
    print("2. Navigate to the Tel Aviv passport appointment calendar.")
    print("3. Once you can see the calendar/date picker, come back here and")
    print("   press Enter.")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(user_agent=_BROWSER_UA)
        page = await context.new_page()

        await page.goto(LOGIN_URL, timeout=30_000)
        print(f"Opened: {LOGIN_URL}")
        print()
        input(">>> Complete login + navigate to the appointment page, then press Enter: ")

        current_url = page.url
        print(f"\nCaptured URL: {current_url}")

        # Save session
        session_data = await capture_session(context)
        save_session_file(session_data)

        # Save appointment URL
        config = load_config()
        config["appointment_url"] = current_url
        save_config(config)
        print(f"Config saved to {CONFIG_FILE}")

        await browser.close()

    print()
    print("Setup complete! Next steps:")
    print("  1. Run: python checker.py --export")
    print("     Copy the output and add it as GitHub Secret PASSPORT_SESSION")
    print("  2. Add GMAIL_USER and GMAIL_APP_PASSWORD as GitHub Secrets")
    print("  3. The GitHub Actions workflow will handle the rest.")


# ---------------------------------------------------------------------------
# --export mode: encode session for GitHub Secrets
# ---------------------------------------------------------------------------

def run_export() -> None:
    if not SESSION_FILE.exists():
        print("ERROR: session.json not found. Run --setup first.")
        sys.exit(1)
    raw = SESSION_FILE.read_text(encoding="utf-8")
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    print()
    print("=== Copy the value below and add it as GitHub Secret PASSPORT_SESSION ===")
    print()
    print(encoded)
    print()
    print("Repository → Settings → Secrets and variables → Actions → New repository secret")
    print("  Name:  PASSPORT_SESSION")
    print("  Value: <the string above>")


# ---------------------------------------------------------------------------
# Appointment slot detection
# ---------------------------------------------------------------------------

async def is_session_valid(page: Page) -> bool:
    """
    Check whether we are still logged in by looking at the current URL.
    If we've been redirected to the login page, the session has expired.
    """
    current_url = page.url
    if "auth/login" in current_url or "login" in current_url:
        return False
    # Also check for a login-related element on the page
    login_el = await page.query_selector(
        "input[type='tel'], input[name='phone'], [class*='login'], [class*='auth']"
    )
    return login_el is None


async def find_available_slots(page: Page) -> list[dict]:
    """
    Scan the appointment page for available date/time slots.

    govisit.gov.il uses a calendar widget. Available dates are typically
    rendered as clickable (non-disabled) cells. This function tries several
    common patterns used by Israeli government scheduling systems.

    Returns a list of dicts: [{"date": "...", "time": "...", "label": "..."}]
    """
    slots: list[dict] = []

    # ── Strategy 1: look for enabled calendar day cells ──────────────────────
    # Many scheduling UIs mark available days with a class like "available",
    # "enabled", or "selectable", and disabled ones with "disabled"/"unavailable".
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
            # Try to get a date from common attributes
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

    # ── Strategy 2: look for time-slot buttons ────────────────────────────────
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

    # ── Strategy 3: look for any text mentioning availability ─────────────────
    # Some pages show "פגישות פנויות" (available appointments) as text
    body_text = await page.inner_text("body")
    availability_keywords = ["פגישות פנויות", "תור פנוי", "זמן פנוי", "available", "open slot"]
    if any(kw.lower() in body_text.lower() for kw in availability_keywords):
        slots.append({
            "date": "–",
            "time": "–",
            "label": "Appointment availability detected – please check manually",
        })

    return slots


async def run_check(appointment_url: str) -> list[dict]:
    """
    Load the saved session, navigate to the appointment page, and return
    any available slots found.
    """
    session_data = load_session_data()
    if not session_data:
        print("ERROR: No session found. Run --setup locally and store PASSPORT_SESSION.")
        raise RuntimeError("missing_session")

    found_slots: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_BROWSER_UA,
            storage_state=session_data,
        )
        page = await context.new_page()

        try:
            print(f"  Navigating to appointment page…")
            await page.goto(appointment_url, wait_until="networkidle", timeout=45_000)

            if not await is_session_valid(page):
                print("  Session has expired – authentication required.")
                raise RuntimeError("session_expired")

            print(f"  Logged in. Current URL: {page.url}")

            # Take a debug screenshot (useful for GitHub Actions logs)
            screenshot_path = SCRIPT_DIR / "last_check.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"  Screenshot saved: {screenshot_path}")

            found_slots = await find_available_slots(page)
            print(f"  Slots detected: {len(found_slots)}")

        except RuntimeError:
            raise
        except Exception as exc:
            print(f"  ERROR during check: {exc}")
            raise
        finally:
            await browser.close()

    return found_slots


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------

def _build_slot_html(slots: list[dict]) -> str:
    rows = ""
    for s in slots:
        rows += f"""
        <tr>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{s.get('date') or '–'}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{s.get('time') or '–'}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{s.get('label') or '–'}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;color:#1a1a1a;max-width:640px;margin:0 auto;padding:20px">
  <h2 style="margin-bottom:4px;color:#16a34a">Passport appointment slots available in Tel Aviv!</h2>
  <p style="color:#555;margin-top:0">Detected on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
  <p><strong>Act fast</strong> – slots disappear quickly.</p>
  <p><a href="https://govisit.gov.il/he/app/auth/login"
        style="background:#16a34a;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block;margin-bottom:16px">
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
  <p style="margin-top:24px;font-size:12px;color:#888">
    Sent by your passport-appointment-checker · auto-monitoring via GitHub Actions
  </p>
</body>
</html>"""


def _build_expired_html() -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;color:#1a1a1a;max-width:640px;margin:0 auto;padding:20px">
  <h2 style="color:#dc2626">Passport checker: session expired</h2>
  <p>The saved login session has expired. The automatic checker has paused.</p>
  <p>To resume monitoring:</p>
  <ol>
    <li>On your local machine, run:<br>
        <code style="background:#f1f5f9;padding:3px 8px;border-radius:4px">python passport_checker/checker.py --setup</code></li>
    <li>Then:<br>
        <code style="background:#f1f5f9;padding:3px 8px;border-radius:4px">python passport_checker/checker.py --export</code></li>
    <li>Update the <strong>PASSPORT_SESSION</strong> GitHub Secret with the new value.</li>
  </ol>
  <p style="font-size:12px;color:#888">
    Sent by your passport-appointment-checker · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
  </p>
</body>
</html>"""


def send_email(subject: str, html_body: str) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("Email credentials not set – skipping send.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"Email sent to {NOTIFY_EMAIL}: {subject}")
    except Exception as exc:
        print(f"Failed to send email: {exc}")
        raise


# ---------------------------------------------------------------------------
# Main polling logic
# ---------------------------------------------------------------------------

async def run_polling() -> None:
    """Standard polling run (used by GitHub Actions)."""
    print("=== Passport Appointment Checker ===")
    print(f"Run time: {datetime.now(timezone.utc).isoformat()}\n")

    config = load_config()
    appointment_url = config.get("appointment_url")

    # Env var override (useful for testing)
    appointment_url = os.environ.get("APPOINTMENT_URL", appointment_url)

    if not appointment_url:
        print("ERROR: appointment_url not set.")
        print("Run 'python checker.py --setup' locally to configure.")
        sys.exit(1)

    print(f"Checking: {appointment_url}\n")

    try:
        slots = await run_check(appointment_url)
    except RuntimeError as exc:
        if "session_expired" in str(exc):
            print("\nSession expired. Sending alert email…")
            send_email(
                "ACTION REQUIRED: Passport checker session expired",
                _build_expired_html(),
            )
            sys.exit(1)
        elif "missing_session" in str(exc):
            sys.exit(1)
        raise

    if not slots:
        print("No available slots found.")
        return

    # Filter out already-alerted slots
    state = load_state()
    new_slots = []
    for slot in slots:
        key = slot_key(slot.get("date", ""), slot.get("time", ""))
        if not already_alerted(state, key):
            new_slots.append(slot)
            mark_alerted(state, key)

    save_state(state)

    if not new_slots:
        print(f"Found {len(slots)} slot(s), but already alerted recently – skipping email.")
        return

    print(f"\nFound {len(new_slots)} new slot(s)! Sending alert email…")
    for s in new_slots:
        print(f"  {s.get('date') or '?'} {s.get('time') or ''} – {s.get('label', '')}")

    send_email(
        f"Passport appointment available in Tel Aviv ({len(new_slots)} slot(s))",
        _build_slot_html(new_slots),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Passport appointment checker for govisit.gov.il")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Interactive setup: log in via browser and save session",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Print base64-encoded session for use as GitHub Secret",
    )
    args = parser.parse_args()

    if args.setup:
        asyncio.run(run_setup())
    elif args.export:
        run_export()
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
