import os
import re
import sys
import datetime as dt
from telegram import Bot
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --- CONFIG ---
URL = "https://ticket.louvre.fr/billetterie/3396"  # pagina con calendario + orari [1](https://ticket.louvre.fr/billetterie/3396)
DATE_TO_CHECK = dt.date(2026, 4, 3)               # 3 aprile 2026
TARGET_TIMES = {"16:00", "16:30"}                 # per test puoi mettere {"17:00"}

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")


def notify(msg: str):
    """Invia Telegram (sync, compatibile con python-telegram-bot 13.x)."""
    if not BOT_TOKEN or not CHAT_ID:
        print("WARN: Telegram non configurato (TG_BOT_TOKEN/TG_CHAT_ID mancanti).")
        return
    Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text=msg)


def normalize_time(s: str) -> str:
    """
    Normalizza vari formati:
    - '17:00' resta '17:00'
    - '17h00' -> '17:00'
    - '17 h 00' -> '17:00'
    """
    s = s.strip().lower()
    s = s.replace(" ", "")
    s = s.replace("h", ":")
    # forza HH:MM
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return s
    hh = int(m.group(1))
    mm = m.group(2)
    return f"{hh:02d}:{mm}"


def goto_month(page, target_year: int, target_month: int):
    """
    Cerca di portare il calendario al mese/anno desiderato cliccando next/prev.
    Funziona con UI che mostra 'April 2026' o 'Avril 2026'.
    """
    month_names = {
        1: ["January", "Janvier"],
        2: ["February", "Février", "Fevrier"],
        3: ["March", "Mars"],
        4: ["April", "Avril"],
        5: ["May", "Mai"],
        6: ["June", "Juin"],
        7: ["July", "Juillet"],
        8: ["August", "Août", "Aout"],
        9: ["September", "Septembre"],
        10: ["October", "Octobre"],
        11: ["November", "Novembre"],
        12: ["December", "Décembre", "Decembre"],
    }

    target_labels = [f"{name} {target_year}" for name in month_names[target_month]]

    def header_matches() -> bool:
        for label in target_labels:
            if page.locator(f"text={label}").count() > 0:
                return True
        return False

    # Se già siamo sul mese giusto, ok
    if header_matches():
        return

    # Tenta fino a 36 click per arrivarci
    for _ in range(36):
        if header_matches():
            return

        # Prova pulsanti "Next" / "Suivant" (diverse UI)
        next_candidates = [
            "button[aria-label*='Next']",
            "button[aria-label*='Suivant']",
            "button:has-text('Next')",
            "button:has-text('Suivant')",
            "button:has-text('>')",
        ]
        clicked = False
        for sel in next_candidates:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                clicked = True
                page.wait_for_timeout(250)
                break

        if not clicked:
            raise RuntimeError("Non trovo il pulsante per cambiare mese nel calendario (Next/Suivant).")

    raise RuntimeError("Impossibile raggiungere il mese/anno desiderato nel calendario.")


def select_date(page, d: dt.date):
    """
    Seleziona la data dal datepicker:
    - apre il calendario se necessario
    - va al mese/anno corretto
    - clicca il giorno
    """
    # A volte il calendario è già visibile; a volte serve cliccare "Sélectionner une date"
    open_candidates = [
        "text=Sélectionner une date",
        "text=Select a date",
        "text=Date",
    ]
    for c in open_candidates:
        loc = page.locator(c)
        if loc.count() > 0:
            try:
                loc.first.click(timeout=1000)
                page.wait_for_timeout(300)
                break
            except Exception:
                pass

    # Vai al mese/anno target
    goto_month(page, d.year, d.month)

    # Clicca il giorno (attenzione: può esserci più di un "3" nella UI; prendiamo il primo visibile nel calendario)
    day_str = str(d.day)
    day_btn = page.get_by_role("button", name=day_str)
    if day_btn.count() == 0:
        # fallback: bottoni con testo
        day_btn = page.locator(f"button:has-text('{day_str}')")

    # Clicca il primo che sembra cliccabile
    day_btn.first.click()
    page.wait_for_timeout(800)


def read_available_times(page) -> set:
    """
    Legge tutti i bottoni che assomigliano a orari e li normalizza.
    """
    times = set()
    buttons = page.locator("button")
    for i in range(buttons.count()):
        txt = buttons.nth(i).inner_text()
        txt_norm = normalize_time(txt)
        if re.fullmatch(r"\d{2}:\d{2}", txt_norm):
            # se il bottone è disabled, ignoralo
            disabled = buttons.nth(i).get_attribute("disabled")
            if not disabled:
                times.add(txt_norm)
    return times


def main():
    print(f"INFO: Controllo su {URL} per data {DATE_TO_CHECK} e orari {sorted(TARGET_TIMES)}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # Seleziona data
        select_date(page, DATE_TO_CHECK)

        # Leggi orari
        available = read_available_times(page)
        print(f"DEBUG: Orari disponibili letti: {sorted(available)[:20]}{'…' if len(available) > 20 else ''}")

        found = sorted(set(map(normalize_time, TARGET_TIMES)).intersection(available))
        if found:
            notify(
                "🎉 BIGLIETTI LOUVRE DISPONIBILI!\n"
                f"📅 {DATE_TO_CHECK.strftime('%d/%m/%Y')}\n"
                f"🕕 Slot: {', '.join(found)}\n"
                f"👉 Prenota qui: {URL}"
            )
            print("INFO: Slot trovati, notifica inviata.")
        else:
            print("INFO: Slot target non trovati (ancora).")

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except PlaywrightTimeoutError as e:
        print(f"ERROR: Timeout Playwright: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
