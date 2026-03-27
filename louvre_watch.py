import os
import re
import sys
import time
import datetime as dt
from dataclasses import dataclass
from telegram import Bot
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


DEBUG = os.getenv("DEBUG", "0") == "1"

def dump_debug(page, name: str):
    """Save screenshot + HTML for later inspection in GitHub Actions artifacts."""
    if not DEBUG:
        return
    page.screenshot(path=f"debug_{name}.png", full_page=True)
    with open(f"debug_{name}.html", "w", encoding="utf-8") as f:
        f.write(page.content())


# ---------------------------
# CONFIG
# ---------------------------
URL = "https://ticket.louvre.fr/billetterie/3396"  # official time-slot selection page [1](https://www.tripadvisor.com/ShowTopic-g187147-i14-k14363929-How_to_book_free_first_Friday_ticket_at_Louvre-Paris_Ile_de_France.html)
DATE_TO_CHECK = dt.date(2026, 4, 3)
TARGET_TIMES = {"16:00", "16:30"}  # for test you can set {"16:30"}

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")

DEBUG = True  # set False once stable


# ---------------------------
# Telegram (sync; python-telegram-bot 13.x)
# ---------------------------
def notify(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("WARN: Telegram not configured (TG_BOT_TOKEN/TG_CHAT_ID missing).")
        return
    Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=msg)


# ---------------------------
# Time normalization
# ---------------------------
def normalize_time(s: str) -> str:
    """
    Normalize:
      '16h30' -> '16:30'
      '16:30' -> '16:30'
      '16 h 30' -> '16:30'
    """
    if not s:
        return ""
    t = s.strip().lower().replace(" ", "").replace("h", ":")
    m = re.search(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return ""
    hh = int(m.group(1))
    mm = m.group(2)
    return f"{hh:02d}:{mm}"


# ---------------------------
# French month parsing (calendar header is like "MARS 2026")
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


def months_between(cur: MonthYear, tgt: MonthYear) -> int:
    return (tgt.year - cur.year) * 12 + (tgt.month - cur.month)


# ---------------------------
# Calendar locators
# ---------------------------
def month_header(page):
    # Match ALLCAPS month + year, e.g. "MARS 2026"
    return page.locator("text=/^[A-ZÉÈÊËÀÂÎÏÔÛÙÜÇ]+\\s+\\d{4}$/").first


def ensure_calendar_visible(page):
    if month_header(page).count() > 0:
        return
    # Try opening date picker
    for txt in ["Sélectionner une date", "Select a date", "Date"]:
        loc = page.locator(f"text={txt}")
        if loc.count() > 0:
            try:
                loc.first.click(timeout=1500)
                time.sleep(0.3)
                break
            except Exception:
                pass


def find_calendar_container(page):
    hdr = month_header(page)
    if hdr.count() == 0:
        return page
    node = hdr
    for _ in range(5):
        parent = node.locator("xpath=..")
        if parent.count() == 0:
            break
        # if this parent has some buttons, it’s likely the calendar header container
        if parent.locator("button").count() >= 2:
            return parent
        node = parent
    return page


def find_prev_next_buttons(container):
    # choose leftmost and rightmost small visible buttons near header
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
            if box["width"] > 140 or box["height"] > 140:
                continue
            candidates.append((box["x"], b))
        except Exception:
            continue

    if len(candidates) < 2:
        return None, None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[-1][1]


def go_to_month(page, target_date: dt.date, max_clicks=36) -> bool:
    ensure_calendar_visible(page)
    hdr = month_header(page)
    if hdr.count() == 0:
        return False

    container = find_calendar_container(page)
    prev_btn, next_btn = find_prev_next_buttons(container)
    if not prev_btn or not next_btn:
        return False

    target = MonthYear(month=target_date.month, year=target_date.year)

    for _ in range(max_clicks):
        txt = hdr.inner_text().strip()
        cur = parse_month_header(txt)
        if not cur:
            time.sleep(0.2)
            continue

        diff = months_between(cur, target)
        if diff == 0:
            return True

        try:
            (next_btn if diff > 0 else prev_btn).click()
        except Exception:
            time.sleep(0.2)

        time.sleep(0.25)

    return False


def click_day(page, target_date: dt.date) -> bool:
    # Try clicking a specific aria-label first (more stable if available)
    # If the DOM does not have it, fallback to the day number.
    day = target_date.day

    # Fallback: click day number (but avoid disabled)
    loc = page.locator("button").filter(has_text=re.compile(rf"^\s*{day}\s*$"))
    if loc.count() == 0:
        loc = page.locator(f"button:has-text('{day}')")

    for i in range(loc.count()):
        b = loc.nth(i)
        try:
            if not b.is_visible():
                continue
            if b.get_attribute("disabled") is not None:
                continue
            b.click()
            time.sleep(0.6)
            return True
        except Exception:
            continue
    return False


# ---------------------------
# Time slots reading (robust)
# ---------------------------
CLICKABLE_SELECTORS = [
    "button",
    "[role='button']",
    "a",
    "[tabindex]"
]

DISABLED_CLASS_RE = re.compile(r"(disabled|is-disabled|unavailable|inactive)", re.IGNORECASE)


def is_disabled(el) -> bool:
    try:
        if el.get_attribute("disabled") is not None:
            return True
    except Exception:
        pass
    try:
        if (el.get_attribute("aria-disabled") or "").lower() == "true":
            return True
    except Exception:
        pass
    try:
        cls = el.get_attribute("class") or ""
        if DISABLED_CLASS_RE.search(cls):
            return True
    except Exception:
        pass
    return False


def wait_for_time_section(page):
    # Your screenshot shows the section title "Sélectionner une heure"
    # Wait for it to be visible before reading slots.
    try:
        page.locator("text=Sélectionner une heure").wait_for(timeout=7000)
    except Exception:
        # fallback: if UI language changes
        try:
            page.locator("text=Select a time").wait_for(timeout=3000)
        except Exception:
            pass


def read_available_times(page) -> set[str]:
    # Wait a bit for slots to render after date selection
    wait_for_time_section(page)
    time.sleep(0.5)

    times = set()
    all_found = []

    for sel in CLICKABLE_SELECTORS:
        els = page.locator(sel)
        for i in range(els.count()):
            el = els.nth(i)
            try:
                txt = el.inner_text()
            except Exception:
                continue

            t = normalize_time(txt)
            if not t:
                continue

            disabled = is_disabled(el)
            all_found.append((t, disabled, sel, (txt or "").strip()))

            if not disabled:
                times.add(t)

    if DEBUG:
        # show what we saw (first 60 rows max)
        preview = all_found[:60]
        print("DEBUG: time candidates (normalized, disabled?, selector, raw):")
        for row in preview:
            print("  ", row)
        print(f"DEBUG: available times (enabled) = {sorted(times)}")

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

        # 1) Navigate to correct month
        if not go_to_month(page, DATE_TO_CHECK):
            print("INFO: Could not reach target month (calendar navigation not available). Exiting cleanly.")
            browser.close()
            return

        # 2) Click the day
        if not click_day(page, DATE_TO_CHECK):
            print("INFO: Day not clickable (not released/available). Exiting cleanly.")
            browser.close()
            return

        dump_debug(page, "after_day_click")
        
        # 3) Read time slots AFTER the "select time" section appears
        page.locator("text=Sélectionner une heure").wait_for(timeout=10000)
        dump_debug(page, "time_grid_visible")
        available = read_available_times(page)

        targets = {normalize_time(t) for t in TARGET_TIMES}
        found = sorted(targets.intersection(available))

        if found:
            notify(
                "🎉 LOUVRE TICKETS AVAILABLE!\n"
                f"📅 {DATE_TO_CHECK.strftime('%d/%m/%Y')}\n"
                f"🕕 Slots: {', '.join(found)}\n"
                f"👉 Book here: {URL}"
            )
            print("INFO: Found target slot(s), notification sent.")
        else:
            print("INFO: Target slot(s) not found (yet).")

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
