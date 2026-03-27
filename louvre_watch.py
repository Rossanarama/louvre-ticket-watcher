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
URL = "https://ticket.louvre.fr/billetterie/3396"  # pagina ufficiale data/ora [1](https://www.tripadvisor.com/ShowTopic-g187147-i14-k14363929-How_to_book_free_first_Friday_ticket_at_Louvre-Paris_Ile_de_France.html)
DATE_TO_CHECK = dt.date(2026, 4, 3)
TARGET_TIMES = {"16:00", "16:30"}  # per test: {"16:30"}

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")

DEBUG = os.getenv("DEBUG", "0") == "1"

# ---------------------------
# Debug helpers
# ---------------------------
def dump_debug(page, name: str):
    # crea SEMPRE un file di traccia (così artifacts non sono vuoti)
    with open("debug_times.txt", "a", encoding="utf-8") as f:
        f.write(f"[HIT] {name}\n")

    if not DEBUG:
        return

    page.screenshot(path=f"debug_{name}.png", full_page=True)
    with open(f"debug_{name}.html", "w", encoding="utf-8") as f:
        f.write(page.content())

# ---------------------------
# Telegram
# ---------------------------
def notify(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("WARN: Telegram non configurato (TG_BOT_TOKEN/TG_CHAT_ID mancanti).")
        return
    Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=msg)

# ---------------------------
# Time normalization
# ---------------------------
TIME_RE = re.compile(r"\b(\d{1,2})\s*(?:h|:)\s*(\d{2})\b", re.IGNORECASE)

def normalize_time(s: str) -> str:
    if not s:
        return ""
    m = TIME_RE.search(s)
    if not m:
        return ""
    hh = int(m.group(1))
    mm = m.group(2)
    return f"{hh:02d}:{mm}"

# ---------------------------
# Month header parsing (FR)
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
    name = m.group(1)
    year = int(m.group(2))
    month = FR_MONTHS.get(name)
    if not month:
        return None
    return MonthYear(month=month, year=year)

def months_diff(cur: MonthYear, tgt: MonthYear) -> int:
    return (tgt.year - cur.year) * 12 + (tgt.month - cur.month)

# ---------------------------
# Calendar locators
# ---------------------------
def month_header(page):
    # Header tipo "MARS 2026"
    return page.locator("text=/^[A-ZÉÈÊËÀÂÎÏÔÛÙÜÇ]+\\s+\\d{4}$/").first

def ensure_calendar_visible(page):
    # Se header presente, calendario già visibile
    if month_header(page).count() > 0:
        return True

    # Prova a cliccare "Sélectionner une date" se serve
    for txt in ["Sélectionner une date", "Select a date", "Date"]:
        loc = page.locator(f"text={txt}")
        if loc.count() > 0:
            try:
                loc.first.click(timeout=1500)
                time.sleep(0.4)
                return True
            except Exception:
                pass

    return month_header(page).count() > 0

def calendar_header_container(page):
    """
    Trova un container “vicino” all’header che contenga i due bottoni freccia.
    Evita bounding boxes: prendiamo i primi due button in DOM order.
    """
    hdr = month_header(page)
    if hdr.count() == 0:
        return None

    node = hdr
    for _ in range(6):
        parent = node.locator("xpath=..")
        if parent.count() == 0:
            break
        # Se questo parent contiene almeno 2 bottoni, probabilmente include le frecce
        if parent.locator("button").count() >= 2:
            return parent
        node = parent

    return page  # fallback

def get_prev_next_buttons(page):
    container = calendar_header_container(page)
    if container is None:
        return None, None

    btns = container.locator("button")
    if btns.count() < 2:
        return None, None

    # Nella UI mostrata (cerchi < e >), i due bottoni sono i primi due attorno all’header.
    # In DOM order: spesso [0]=prev, [1]=next.
    prev_btn = btns.nth(0)
    next_btn = btns.nth(1)

    # Se per caso sono invertiti (raro), li scambiamo controllando la posizione “x”
    try:
        b0 = prev_btn.bounding_box()
        b1 = next_btn.bounding_box()
        if b0 and b1 and b0["x"] > b1["x"]:
            prev_btn, next_btn = next_btn, prev_btn
    except Exception:
        pass

    return prev_btn, next_btn

def go_to_target_month(page, target_date: dt.date, max_steps=36) -> bool:
    if not ensure_calendar_visible(page):
        return False

    hdr = month_header(page)
    if hdr.count() == 0:
        return False

    prev_btn, next_btn = get_prev_next_buttons(page)
    if not prev_btn or not next_btn:
        return False

    target = MonthYear(month=target_date.month, year=target_date.year)

    for _ in range(max_steps):
        cur = parse_month_header(hdr.inner_text())
        if not cur:
            time.sleep(0.2)
            continue

        diff = months_diff(cur, target)
        if diff == 0:
            return True

        try:
            (next_btn if diff > 0 else prev_btn).click()
        except Exception:
            time.sleep(0.2)

        time.sleep(0.35)

    return False

def click_day(page, target_date: dt.date) -> bool:
    day = str(target_date.day)

    # Preferisci bottoni con testo esatto del giorno
    loc = page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(day)}\s*$"))
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
# Time slots extraction (text + aria-label)
# ---------------------------
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
        if re.search(r"(disabled|is-disabled|unavailable|inactive)", cls, re.IGNORECASE):
            return True
    except Exception:
        pass
    return False

def read_times_debug(page) -> set[str]:
    # aspetta che compaia la sezione orari (come nel tuo screenshot)
    page.locator("text=Sélectionner une heure").wait_for(timeout=10000)
    time.sleep(0.4)

    candidates = ["button", "[role='button']", "a", "[tabindex]"]
    enabled = set()
    lines = []

    for sel in candidates:
        els = page.locator(sel)
        for i in range(els.count()):
            el = els.nth(i)

            try:
                txt = (el.inner_text() or "").strip()
            except Exception:
                txt = ""

            try:
                aria = (el.get_attribute("aria-label") or "").strip()
            except Exception:
                aria = ""

            t = normalize_time(txt) or normalize_time(aria)
            if not t:
                continue

            dis = is_disabled(el)
            lines.append(f"{sel}\t{t}\tdisabled={dis}\ttext='{txt}'\taria='{aria}'")
            if not dis:
                enabled.add(t)

    with open("debug_times.txt", "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("DEBUG: enabled times:", sorted(enabled))
    return enabled

# ---------------------------
# Main
# ---------------------------
def main():
    print(f"INFO: Checking {URL} for date {DATE_TO_CHECK} and times {sorted(TARGET_TIMES)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris", viewport={"width": 1280, "height": 720})
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        dump_debug(page, "start")

        # 1) go to target month
        ok_month = go_to_target_month(page, DATE_TO_CHECK)
        if not ok_month:
            dump_debug(page, "no_nav_or_month")
            print("INFO: Could not reach target month (calendar navigation not available). Exiting cleanly.")
            browser.close()
            return

        dump_debug(page, "on_target_month")

        # 2) click day
        if not click_day(page, DATE_TO_CHECK):
            dump_debug(page, "day_not_clickable")
            print("INFO: Day not clickable (not released/available). Exiting cleanly.")
            browser.close()
            return

        dump_debug(page, "after_day_click")

        # 3) read times
        dump_debug(page, "before_times")
        available = read_times_debug(page)
        dump_debug(page, "time_grid_visible")

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
