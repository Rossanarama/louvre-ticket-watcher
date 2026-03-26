import os
import asyncio
import datetime as dt
from playwright.async_api import async_playwright
from telegram import Bot

# --- CONFIG ---
DATE_TO_CHECK = dt.date(2026, 4, 3)
TARGET_TIMES = {"18:00", "18:30"}
URL = "https://ticket.louvre.fr/en"

# Leggi i secrets dalle variabili d'ambiente passate dal workflow
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")

async def notify(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        # Se mancano le credenziali, non fallire il job: stampa e termina "pulito"
        print("WARN: Telegram BOT_TOKEN/CHAT_ID mancanti: nessuna notifica inviata.")
        return
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=msg)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(timezone_id="Europe/Paris", locale="en-GB")
        page = await ctx.new_page()

        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Versione minimale: controlla se compaiono subito i pulsanti 18:00/18:30
        times_found = set()
        buttons = page.locator("button")
        for i in range(await buttons.count()):
            text = (await buttons.nth(i).inner_text()).strip()
            if text in TARGET_TIMES:
                disabled = await buttons.nth(i).get_attribute("disabled")
                if not disabled:
                    times_found.add(text)

        if times_found:
            await notify(
                "🎉 BIGLIETTI LOUVRE DISPONIBILI!\n"
                "📅 Venerdì 3 aprile 2026\n"
                f"🕕 Slot: {', '.join(sorted(times_found))}\n"
                "👉 https://ticket.louvre.fr"
            )
        else:
            print("Nessuno slot 18:00/18:30 trovato (ancora).")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
