"""Telegram-бот: TELEGRAM_TOKEN=... python bot.py"""
import os, asyncio, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import agent
logging.basicConfig(level=logging.INFO)

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Задайте вопрос о сделках биржи на русском или узбекском. / Birja bitimlari haqida rus yoki o‘zbek tilida savol bering.\nОтвечаю только по данным реестра (03.01.2022–30.10.2026).")

async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.chat.send_action("typing")
    try:
        r = await asyncio.wait_for(asyncio.to_thread(agent.ask, u.message.text[:2000]), timeout=88)
        text = r["answer"]
    except Exception:
        text = "Не успел ответить / Javob berishga ulgurmadim. Savolni qisqaroq qilib qayta yuboring."
    for i in range(0, len(text), 4000):
        await u.message.reply_text(text[i:i + 4000])

if __name__ == "__main__":
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()
