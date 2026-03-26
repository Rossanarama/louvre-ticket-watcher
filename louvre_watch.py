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

