import os
import re
import sys
import time
import datetime as dt
from dataclasses import dataclass
from telegram import Bot
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------
# CONFIG
# ---------------------------
URL = "https://ticket.louvre.fr/billetterie/3396"  # date + time slots page [1](https://www.tripadvisor.com/ShowTopic-g187147-i14-k14363929-How_to_book_free_first_Friday_ticket_at_Louvre-Paris_Ile_de_France.html)

DATE_TO_CHECK = dt.date(2026, 4, 3)               # target date
TARGET_TIMES = {"18:00", "18:30"}                 # target slots (use {"16:00"} for testing)

# Telegram secrets passed via GitHub Actions env
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")

# Optional debug artifacts (handy when selectors change)
DEBUG_SCREENSHOT = False  # set True to capture screenshots in Actions logs/artifacts if you add upload step


# ---------------------------
# Helpers: Telegram
# ---------------------------
def notify(msg: str):
    """Send Telegram message (sync)."""
    if not BOT_TOKEN or not CHAT_ID:
        print("WARN: Telegram not configured (TG_BOT_TOKEN/TG_CHAT_ID missing).")
        return
    Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=msg)


# ---------------------------
# Helpers: Time normalization
# ---------------------------
def normalize_time(s: str) -> str:
    """
    Normalize:
      - '17:00' -> '17:00'
      - '17h00' -> '17:00'
      - '17 h 00' -> '17:00'
    """
    s = (s or "").strip().lower()
    s = s.replace(" ", "").replace("h", ":")
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return ""
    hh = int(m.group(1))
    mm = m.group(2)
    return f"{hh:02d}:{mm}"


# ---------------------------
# Helpers: French month parsing
# ---------------------------
FR_MONTHS = {
    "JANVIER": 1,
    "FÉVRIER": 2, "FEVRIER": 2,
    "MARS": 3,
    "AVRIL": 4,
    "MAI": 5,
    "JUIN": 6,
    "JUILLET": 7,
    "AOÛT": 8, "AOUT": 8,
    "SEPTEMBRE": 9,
    "OCTOBRE": 10,
    "NOVEMBRE": 11,
    "DÉCEMBRE": 12, "DECEMBRE": 12
}

MONTH_HEADER_RE = re.compile(r"^\s*([A-ZÉÈÊËÀÂÎÏÔÛÙÜÇ]+)\s+(\d{4})\s*$")


@dataclass
class MonthYear:
    month: int
    year: int


def parse_month_header(text: str) -> MonthYear | None:
    """
    Parse header like 'MARS 2026' into MonthYear(3, 2026)
    """
    if not text:
        return None
    t = text.strip().upper()
    m = MONTH_HEADER_RE.match(t)
    if not m:
        return None
    month_name = m.group(1)
    year = int(m.group(2))
    month = FR_MONTHS.get(month_name)
    if not month:
        return None
    return MonthYear(month=month, year=year)


def month_diff(current: MonthYear, target: MonthYear) -> int:
    """Target - current in months."""
    return (target.year - current.year) * 12 + (target.month - current.month)


# ---------------------------
# DOM finders: Calendar header + arrows
# ---------------------------
def find_month_header_locator(page):
    """
    Find the calendar month header element.
    On your screenshot it looks like: 'MARS 2026' centered above the grid.
    We'll match any ALLCAPS French month + year.
    """
    # Playwright supports regex text selector: text=/.../
    # This tries to match something like 'MARS 2026', 'AVRIL 2026', etc.
    return page.locator("text=/^[A-ZÉÈÊËÀÂÎÏÔÛÙÜÇ]+\\s+\\d{4}$/").first


def find_calendar_container(page):
    """
    Try to find a container around the header that contains the two arrow buttons + day grid.
    We'll walk up a few parents from the header and pick the first one that has >=2 buttons.
    """
    header = find_month_header_locator(page)
    if header.count() == 0:
        return None

    # Walk up 1..4 levels to find a container with at least two visible buttons (the arrows)
    node = header
    for _ in range(4):
        parent = node.locator("xpath=..")
        if parent.count() == 0:
            break
        btns = parent.locator("button")
        if btns.count() >= 2:
            return parent
        node = parent
    # Fallback to page if nothing found
    return page


def find_prev_next_buttons(container):
    """
    Within the container, find the two month navigation buttons by their x position:
      leftmost = previous
      rightmost = next
    """
    btns = container.locator("button")
    candidates = []
    for i in range(btns.count()):
        b = btns.nth(i)
        try:
            if not b.is_visible():
                continue
            box = b.bounding_box()
            if not box:
                continue
            # Only consider fairly small buttons (the round arrow buttons)
            # (This avoids picking unrelated big buttons further down)
            if box["width"] > 120 or box["height"] > 120:
                continue
            candidates.append((box["x"], b))
        except Exception:
            continue

    if len(candidates) < 2:
        return None, None

    candidates.sort(key=lambda x: x[0])
    prev_btn = candidates[0][1]
    next_btn = candidates[-1][1]
    return prev_btn, next_btn


# ---------------------------
# Calendar actions
# ---------------------------
def ensure_calendar_visible(page):
    """
    On your screenshot, the calendar is open with 'Sélectionner une date'.
    Sometimes the page loads with calendar closed; click the 'Sélectionner une date' area if needed.
    """
    # If header exists, calendar is already visible
    if find_month_header_locator(page).count() > 0:
        return

    # Try clicking the "Sélectionner une date" label or similar
    candidates = [
        "text=Sélectionner une date",
        "text=Select a date",
        "text=Date"
    ]
    for c in candidates:
        loc = page.locator(c)
        if loc.count() > 0:
            try:
                loc.first.click(timeout=1500)
                time.sleep(0.3)
                break
            except Exception:
                pass


def go_to_target_month(page, target_date: dt.date, max_clicks=36) -> bool:
    """
    Navigate calendar to target month/year by clicking arrow buttons.
    Returns True if reached, False if navigation buttons not found.
    """
    ensure_calendar_visible(page)

    header = find_month_header_locator(page)
    if header.count() == 0:
        print("INFO: Calendar header not visible yet.")
        return False

    container = find_calendar_container(page)
    prev_btn, next_btn = find_prev_next_buttons(container)

    if not prev_btn or not next_btn:
        print("INFO: Could not locate month navigation arrows.")
        return False

    target = MonthYear(month=target_date.month, year=target_date.year)

    for _ in range(max_clicks):
        hdr_text = header.inner_text().strip()
        current = parse_month_header(hdr_text)
        if not current:
            # if header text not yet stable, wait a moment
            time.sleep(0.2)
            continue

        diff = month_diff(current, target)
        if diff == 0:
            return True

        # Click next if target is in the future; prev if in the past
        try:
            if diff > 0:
                next_btn.click()
            else:
                prev_btn.click()
        except Exception:
            # Could be blocked or overlay
            time.sleep(0.3)
        time.sleep(0.25)

    print("INFO: Reached max clicks but did not land on target month.")
    return False


def click_day_in_current_month(page, day: int) -> bool:
    """
    Click the day button. Avoid disabled days.
    """
    # Try aria-label first (some calendars provide full accessible label)
    # French label patterns can vary; we'll try partials.
    aria_candidates = [
        f"{day} avril {DATE_TO_CHECK.year}",
        f"{day} April {DATE_TO_CHECK.year}",
        f"{day}/{DATE_TO_CHECK.month}/{DATE_TO_CHECK.year}",
        DATE_TO_CHECK.isoformat()
    ]
    for a in aria_candidates:
        loc = page.locator(f"button[aria-label*='{a}']")
        if loc.count() > 0:
            # Click the first enabled match
            for i in range(loc.count()):
                b = loc.nth(i)
                if b.get_attribute("disabled") is None:
                    b.click()
                    time.sleep(0.4)
                    return True

    # Fallback: click by exact day text within visible buttons, prefer enabled ones
    day_text = str(day)
    loc = page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(day_text)}\s*$"))
    if loc.count() == 0:
        # alternative: locate text then go to closest button
        loc = page.locator(f"button:has-text('{day_text}')")

    for i in range(loc.count()):
        b = loc.nth(i)
        try:
            if not b.is_visible():
                continue
            if b.get_attribute("disabled") is not None:
                continue
            # Some disabled days are shown with strikethrough but not disabled attribute;
            # bounding box and click will fail. We'll try click in try/except.
            b.click()
            time.sleep(0.4)
            return True
        except Exception:
            continue

    return False


def read_available_times(page) -> set[str]:
    """
    Read time-slot buttons on the page and normalize them to 'HH:MM'.
    """
    times = set()
    buttons = page.locator("button")

    for i in range(buttons.count()):
        try:
            txt = buttons.nth(i).inner_text()
        except Exception:
            continue

        t = normalize_time(txt)
        if not t:
            continue

        # ignore disabled slots
        try:
            if buttons.nth(i).get_attribute("disabled") is not None:
                continue
        except Exception:
            pass

        times.add(t)

    return times


# ---------------------------
# Main
# ---------------------------
def main():
    print(f"INFO: Checking {URL} for date {DATE_TO_CHECK} and times {sorted(TARGET_TIMES)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        if DEBUG_SCREENSHOT:
            page.screenshot(path="debug_loaded.png", full_page=True)

        # 1) Navigate to target month/year
        ok_month = go_to_target_month(page, DATE_TO_CHECK)
        if not ok_month:
            print("INFO: Cannot reach target month via arrows (date may not be open yet). Exiting cleanly.")
            browser.close()
            return

        if DEBUG_SCREENSHOT:
            page.screenshot(path="debug_target_month.png", full_page=True)

        # 2) Click day
        ok_day = click_day_in_current_month(page, DATE_TO_CHECK.day)
        if not ok_day:
            print("INFO: Day not clickable (possibly not released/available). Exiting cleanly.")
            browser.close()
            return

        if DEBUG_SCREENSHOT:
            page.screenshot(path="debug_after_day.png", full_page=True)

        # 3) Read available times
        available = read_available_times(page)
        print(f"DEBUG: Times read ({len(available)}): {sorted(available)}")

        targets = {normalize_time(t) for t in TARGET_TIMES}
        found = sorted(targets.intersection(available))

        if found:
            notify(
                "🎉 LOUVRE TICKETS FOUND!\n"
                f"📅 {DATE_TO_CHECK.strftime('%d/%m/%Y')}\n"
                f"🕕 Slots: {', '.join(found)}\n"
                f"👉 Book here: {URL}"
            )
            print("INFO: Found target slot(s), notification sent.")
        else:
            print("INFO: Target slots not found (yet).")

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except PlaywrightTimeoutError as e:
        print(f"ERROR: Playwright timeout: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
