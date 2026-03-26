import asyncio
import datetime as dt
from playwright.async_api import async_playwright
from telegram import Bot

DATE_TO_CHECK = dt.date(2026, 4, 3)
TARGET_TIMES = {"18:00", "18:30"}
URL = "https://ticket.louvre.fr/en"

BOT_TOKEN = None
CHAT_ID = None

async def notify(msg: str):
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=msg)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(timezone_id="Europe/Paris")
        page = await ctx.new_page()

        await page.goto(URL)
        await page.wait_for_timeout(2000)

        times = set()
        buttons = page.locator("button")
        for i in range(await buttons.count()):
            text = (await buttons.nth(i).inner_text()).strip()
            if text in TARGET_TIMES:
                disabled = await buttons.nth(i).get_attribute("disabled")
                if not disabled:
                    times.add(text)

        if times:
            await notify(
                "🎉 BIGLIETTI LOUVRE DISPONIBILI!\n"
                "📅 Venerdì 3 aprile\n"
                f"🕕 Slot: {', '.join(sorted(times))}\n"
                "👉 https://ticket.louvre.fr"
            )

        await browser.close()

if name == "main":
    asyncio.run(main())
