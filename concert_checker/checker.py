#!/usr/bin/env python3
"""
Concert alert service – Yuja Wang & Gustavo Dudamel (European concerts).

How it works
============
1. Loads known_concerts.json (previously seen concerts).
2. Scrapes the official artist websites via headless Chromium (Playwright).
3. Filters concerts whose location is in Europe.
4. Sends an email to oronroi@gmail.com for every NEW concert found.
5. Writes the updated state back to known_concerts.json.

First run
---------
On the very first run the state file is empty.  All currently listed European
concerts are saved to state WITHOUT sending an email (to avoid an immediate
flood).  Subsequent runs only alert on brand-new additions.

Required environment variables (store as GitHub Secrets)
---------------------------------------------------------
  GMAIL_USER          – e.g. youraddress@gmail.com
  GMAIL_APP_PASSWORD  – 16-char Google App Password (not your normal password)
"""

import asyncio
import hashlib
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright, Page

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "known_concerts.json"
DEBUG_FILE = SCRIPT_DIR / "debug_last_run.txt"


class Tee:
    """Write to both stdout and a file simultaneously."""
    def __init__(self, path: Path):
        self._file = open(path, "w", encoding="utf-8")
        self._stdout = sys.stdout
    def write(self, data):
        self._stdout.write(data)
        self._file.write(data)
    def flush(self):
        self._stdout.flush()
        self._file.flush()
    def close(self):
        self._file.close()

NOTIFY_EMAIL = "oronroi@gmail.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

ARTISTS: dict[str, dict] = {
    "yuja_wang": {
        "name": "Yuja Wang",
        "url": "https://yujawang.com/calendar/",
        # Filterable isotope grid (gw-gopf WordPress plugin)
        "item_selector": ".gw-gopf-isotope-item",
        "sub_selectors": {
            "date":  ".event-date-wrap",
            "title": ".event-title",
            "venue": ".upcoming-venue",
            "city":  ".upcoming-city",
        },
    },
    "dudamel": {
        "name": "Gustavo Dudamel",
        "url": "https://www.gustavodudamel.com/schedule",
        # Webflow dynamic list (schedule-list8 component)
        "item_selector": ".schedule-list8_item",
        "sub_selectors": {
            "date":     ".schedule-list8_date-wrapper",
            "location": ".schedule-location",
        },
    },
}

# ISO 3166-1 alpha-2 codes for European countries
EUROPE_COUNTRY_CODES: set[str] = {
    "AL", "AD", "AM", "AT", "AZ", "BY", "BE", "BA", "BG", "HR", "CY", "CZ",
    "DK", "EE", "FI", "FR", "GE", "DE", "GR", "HU", "IS", "IE", "IT", "KZ",
    "XK", "LV", "LI", "LT", "LU", "MT", "MD", "MC", "ME", "NL", "MK", "NO",
    "PL", "PT", "RO", "RU", "SM", "RS", "SK", "SI", "ES", "SE", "CH", "TR",
    "UA", "GB", "VA",
}

# Human-readable names as fallback when only plain text is available
EUROPE_COUNTRY_NAMES: set[str] = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czech republic",
    "czechia", "denmark", "estonia", "finland", "france", "germany", "greece",
    "hungary", "iceland", "ireland", "italy", "latvia", "liechtenstein",
    "lithuania", "luxembourg", "malta", "moldova", "monaco", "montenegro",
    "netherlands", "north macedonia", "norway", "poland", "portugal",
    "romania", "russia", "san marino", "serbia", "slovakia", "slovenia",
    "spain", "sweden", "switzerland", "turkey", "ukraine", "united kingdom",
    "england", "scotland", "wales", "northern ireland", "albania", "andorra",
    "armenia", "azerbaijan", "belarus", "bosnia and herzegovina", "georgia",
    "kosovo",
}

# Major European cities for detection when only a city name appears (no country)
EUROPE_CITY_NAMES: set[str] = {
    "amsterdam", "antwerp", "athens", "barcelona", "berlin", "bern",
    "bilbao", "bologna", "bordeaux", "bratislava", "brussels", "bucharest",
    "budapest", "cologne", "copenhagen", "dresden", "dublin", "düsseldorf",
    "edinburgh", "florence", "frankfurt", "geneva", "granada", "hamburg",
    "helsinki", "istanbul", "krakow", "lausanne", "leipzig", "lisbon",
    "ljubljana", "london", "luxembourg", "lyon", "madrid", "marseille",
    "milan", "munich", "nice", "oslo", "paris", "prague", "reykjavik",
    "riga", "rome", "rotterdam", "salzburg", "sarajevo", "seville",
    "sofia", "stockholm", "strasbourg", "tallinn", "toulouse", "venice",
    "vienna", "vilnius", "warsaw", "zagreb", "zurich",
}


# ---------------------------------------------------------------------------
# European location detection
# ---------------------------------------------------------------------------

def is_european(country_code: str, country_name: str, raw_text: str = "") -> bool:
    """Return True if the location is in Europe.

    Checks (in order):
    1. ISO country code
    2. Explicit country name in the structured fields
    3. Full-text scan of raw_text for country and city names
    """
    if country_code and country_code.upper() in EUROPE_COUNTRY_CODES:
        return True
    search = (country_name + " " + raw_text).lower()
    for name in EUROPE_COUNTRY_NAMES:
        if name in search:
            return True
    for city in EUROPE_CITY_NAMES:
        if city in search:
            return True
    return False


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("Warning: corrupt state file – starting fresh.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def concert_id(artist_key: str, date: str, venue: str, city: str) -> str:
    """Stable unique ID for a concert (artist + date + venue + city)."""
    raw = f"{artist_key}|{date}|{venue}|{city}".lower().strip()
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


async def _extract_json_ld(page: Page) -> list[dict]:
    """Extract Schema.org Event/MusicEvent objects from JSON-LD script tags."""
    concerts: list[dict] = []
    scripts: list[str] = await page.eval_on_selector_all(
        'script[type="application/ld+json"]',
        "els => els.map(e => e.textContent)",
    )
    for raw in scripts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") not in ("Event", "MusicEvent"):
                continue
            location = item.get("location") or {}
            address = location.get("address") or {} if isinstance(location, dict) else {}
            if isinstance(address, str):
                address = {}

            country_code = address.get("addressCountry", "")
            city = address.get("addressLocality", "")
            venue = location.get("name", "") if isinstance(location, dict) else ""
            date_raw = item.get("startDate", "")
            date = date_raw[:10] if date_raw else ""
            name = item.get("name", "Concert")
            url = item.get("url", "")
            if not url and isinstance(item.get("offers"), dict):
                url = item["offers"].get("url", "")

            concerts.append(
                dict(
                    date=date,
                    venue=venue,
                    city=city,
                    country_code=country_code.upper() if country_code else "",
                    country_name="",
                    title=name,
                    url=url,
                )
            )
    return concerts


async def _extract_dom_events(page: Page, base_url: str, config: dict) -> list[dict]:
    """
    Extract concert listings using artist-specific selectors from `config`,
    with a generic fallback for unknown sites.
    """
    concerts: list[dict] = []
    sub = config.get("sub_selectors", {})

    # --- resolve item elements ---
    item_selector = config.get("item_selector", "")
    items = []
    if item_selector:
        items = await page.query_selector_all(item_selector)
        print(f"    Selector '{item_selector}': {len(items)} item(s)")

    if not items:
        # Generic fallback selectors
        for sel in (
            ".eventlist-event",
            ".tribe-events-calendar-list__event",
            "article[class*='event']",
            "div[class*='event-item']",
            "li[class*='event']",
            "table tr",
        ):
            found = await page.query_selector_all(sel)
            if len(found) > 1:
                items = found
                print(f"    Fallback selector '{sel}': {len(items)} item(s)")
                break

    if not items:
        print("    No item elements found.")
        return concerts

    for idx, item in enumerate(items):
        try:
            full_text = (await item.inner_text()).strip()
            if not full_text or len(full_text) < 5:
                continue

            # Log the first item's text so we can verify parsing
            if idx == 0:
                print(f"    First item text: {full_text[:200]!r}")

            # --- date ---
            date_str = ""
            if "date" in sub:
                date_el = await item.query_selector(sub["date"])
                if date_el:
                    date_str = (await date_el.inner_text()).strip().split("\n")[0].strip()
            if not date_str:
                time_el = await item.query_selector("time[datetime]")
                if time_el:
                    date_str = ((await time_el.get_attribute("datetime")) or "")[:10]
                if not date_str:
                    time_el = await item.query_selector("time")
                    if time_el:
                        date_str = (await time_el.inner_text()).strip()

            # --- venue / city / country ---
            venue, city, country_name = "", "", ""

            if "venue" in sub:
                venue_el = await item.query_selector(sub["venue"])
                if venue_el:
                    venue = (await venue_el.inner_text()).strip()

            if "city" in sub:
                city_el = await item.query_selector(sub["city"])
                if city_el:
                    city_raw = (await city_el.inner_text()).strip()
                    parts = [p.strip() for p in city_raw.split(",")]
                    city = parts[0]
                    country_name = parts[-1].lower() if len(parts) >= 2 else ""

            if "location" in sub:
                loc_el = await item.query_selector(sub["location"])
                if loc_el:
                    loc_text = (await loc_el.inner_text()).strip()
                    parts = [p.strip() for p in loc_text.split(",")]
                    if not venue:
                        venue = parts[0] if parts else ""
                    if not city:
                        city = parts[1] if len(parts) >= 2 else ""
                    if not country_name:
                        country_name = parts[-1].lower() if len(parts) >= 3 else ""

            # --- link ---
            a_el = await item.query_selector("a")
            href = (await a_el.get_attribute("href") or "") if a_el else ""
            url = (
                href if href.startswith("http")
                else (base_url.rstrip("/") + "/" + href.lstrip("/")) if href
                else base_url
            )

            # --- title ---
            title_str = ""
            if "title" in sub:
                title_el = await item.query_selector(sub["title"])
                if title_el:
                    title_str = (await title_el.inner_text()).strip()
            if not title_str:
                title_el = await item.query_selector("h1,h2,h3,h4,.title,[class*='title']")
                title_str = (await title_el.inner_text()).strip() if title_el else full_text[:120]

            concerts.append(dict(
                date=date_str,
                venue=venue,
                city=city,
                country_code="",
                country_name=country_name,
                title=title_str,
                url=url,
                raw_text=full_text[:400],
            ))
        except Exception as exc:
            print(f"    Warning: could not parse item {idx}: {exc}")

    return concerts


async def scrape_artist(url: str, artist_key: str, config: dict) -> list[dict]:
    """
    Load the official artist calendar page and extract concert listings.
    Tries JSON-LD structured data first, then falls back to DOM parsing
    using selectors from `config`.
    """
    concerts: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_BROWSER_UA,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        try:
            print(f"  Loading {url} …")
            # "load" is less strict than "networkidle" and works for sites
            # with persistent background requests (analytics, polling, etc.)
            await page.goto(url, wait_until="load", timeout=45_000)

            # Extra wait for JS-rendered calendars to populate
            await page.wait_for_timeout(4000)

            # --- light diagnostics ---
            title = await page.title()
            print(f"  Page title: {title}")

            # Strategy 1 – JSON-LD structured data (most reliable when present)
            json_ld = await _extract_json_ld(page)
            print(f"  JSON-LD events found: {len(json_ld)}")
            if json_ld:
                concerts = json_ld
            else:
                # Strategy 2 – artist-specific DOM parsing
                concerts = await _extract_dom_events(page, url, config)

        except Exception as exc:
            print(f"  ERROR scraping {artist_key}: {exc}")
        finally:
            await browser.close()

    return concerts


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

async def check_all() -> tuple[list[dict], bool]:
    """
    Check all artists.  Returns (new_european_concerts, is_first_run).
    """
    state = load_state()
    is_first_run = len(state) == 0
    new_concerts: list[dict] = []

    for artist_key, info in ARTISTS.items():
        print(f"\nChecking {info['name']} …")
        artist_state: dict = state.setdefault(artist_key, {})

        concerts = await scrape_artist(info["url"], artist_key, info)
        print(f"  Total concerts found: {len(concerts)}")

        european = [
            c for c in concerts
            if is_european(
                c.get("country_code", ""),
                c.get("country_name", ""),
                c.get("raw_text", ""),
            )
        ]
        print(f"  European concerts: {len(european)}")

        for concert in european:
            cid = concert_id(
                artist_key,
                concert.get("date", ""),
                concert.get("venue", ""),
                concert.get("city", ""),
            )
            if cid not in artist_state:
                artist_state[cid] = {
                    "date": concert.get("date"),
                    "title": concert.get("title"),
                    "venue": concert.get("venue"),
                    "city": concert.get("city"),
                    "country_code": concert.get("country_code"),
                    "country_name": concert.get("country_name"),
                    "url": concert.get("url"),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
                if not is_first_run:
                    concert["artist_name"] = info["name"]
                    concert["id"] = cid
                    new_concerts.append(concert)
                    print(f"  NEW: {concert.get('date')} | {concert.get('venue')}, {concert.get('city')}")
            else:
                print(f"  Known: {concert.get('date')} | {concert.get('venue')}, {concert.get('city')}")

    save_state(state)
    return new_concerts, is_first_run


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _build_html(concerts: list[dict]) -> str:
    rows = ""
    for c in concerts:
        date_display = c.get("date") or "TBD"
        venue = c.get("venue") or "–"
        city = c.get("city") or "–"
        country = c.get("country_name") or c.get("country_code") or "–"
        title = c.get("title") or "–"
        url = c.get("url") or "#"
        artist = c.get("artist_name", "")
        rows += f"""
        <tr>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{artist}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{date_display}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{venue}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{city}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">{country.title()}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #e8e8e8">
            <a href="{url}" style="color:#2563eb">Tickets / Info</a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;color:#1a1a1a;max-width:760px;margin:0 auto;padding:20px">
  <h2 style="margin-bottom:4px">🎵 New European Concerts</h2>
  <p style="color:#555;margin-top:0">Detected on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}</p>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <thead>
      <tr style="background:#1e293b;color:#fff;text-align:left">
        <th style="padding:10px 14px">Artist</th>
        <th style="padding:10px 14px">Date</th>
        <th style="padding:10px 14px">Venue</th>
        <th style="padding:10px 14px">City</th>
        <th style="padding:10px 14px">Country</th>
        <th style="padding:10px 14px">Link</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="margin-top:24px;font-size:12px;color:#888">
    Sent by your concert-notification-service · source on GitHub
  </p>
</body>
</html>"""


def send_email(concerts: list[dict]) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("Email credentials not set – skipping send.")
        return

    artists_mentioned = ", ".join(sorted({c["artist_name"] for c in concerts}))
    subject = f"New European concerts: {artists_mentioned}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(_build_html(concerts), "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"Email sent → {NOTIFY_EMAIL}")
    except Exception as exc:
        print(f"Failed to send email: {exc}")
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=== Concert Notification Service ===")
    print(f"Run time: {datetime.now(timezone.utc).isoformat()}\n")

    new_concerts, is_first_run = await check_all()

    if is_first_run:
        print("\nFirst run: state populated. No email sent.")
        print("Future runs will alert on new additions.")
    elif new_concerts:
        print(f"\nFound {len(new_concerts)} new European concert(s). Sending email…")
        send_email(new_concerts)
    else:
        print("\nNo new European concerts found. No email sent.")

    print("\nDone.")


if __name__ == "__main__":
    tee = Tee(DEBUG_FILE)
    sys.stdout = tee
    try:
        asyncio.run(main())
    finally:
        sys.stdout = tee._stdout
        tee.close()
