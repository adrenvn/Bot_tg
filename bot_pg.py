import asyncio
import logging
import asyncpg
import csv
import os
import aiofiles
import uvloop
from io import StringIO
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)
from config import TOKEN, DATABASE_URL

# Администраторы
ADMIN_IDS = {5060645464}  # Используем set для быстрого поиска

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
WAITING_VIDEO_LINKS, CONFIRM_MORE_LINKS = range(30, 32)
WAITING_SCORE, WAITING_COMMENT = range(2)

# Подключение к БД
async def connect_with_retry(retries=5, delay=3):
    for i in range(retries):
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, max_size=20)
            logger.info("✅ Подключение к PostgreSQL успешно установлено.")
            return pool
        except Exception as e:
            logger.warning(f"⚠️ Не удалось подключиться к PostgreSQL: {e}. Попытка {i+1}/{retries}")
            await asyncio.sleep(delay)
    raise Exception("❌ Не удалось подключиться к PostgreSQL после нескольких попыток.")

# Функции БД
async def add_video_link(pool, video_link: str):
    async with pool.acquire() as conn:
        try:
            await conn.execute('''
                INSERT INTO videos (video_link) VALUES ($1)
                ON CONFLICT (video_link) DO NOTHING;
            ''', video_link)
            logger.info("✅ Добавлено новое видео: %s", video_link)
        except Exception as e:
            logger.error(f"❌ Ошибка добавления видео {video_link}: {e}")

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎥 Отправить видео", callback_data='send_video')],
        [InlineKeyboardButton("⭐ Начать оценку", callback_data='start_rating')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Привет! Выберите действие:", reply_markup=markup)

# Обработчик нажатий кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data == "send_video":
            await query.message.reply_text("Отправьте ссылку на видео.")
        elif query.data == "start_rating":
            await query.message.reply_text("Оцените видео.")
        elif query.data == "help":
            await query.message.reply_text("Справка по боту.")
        else:
            logger.warning(f"⚠️ Неизвестный callback_data: {query.data}")
    except Exception as e:
        logger.error(f"❌ Ошибка при обработке callback: {e}")

# Команда /download – скачивание CSV
async def download_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для скачивания таблицы.")
        return

    pool = context.bot_data["db_pool"]
    filename = "videos_ratings.csv"
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM videos")

    output = StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["ID", "Ссылка", "Дата создания"])
    for row in rows:
        writer.writerow([row['video_id'], row['video_link'], row['created_at']])

    async with aiofiles.open(filename, "w", encoding="utf-8-sig") as f:
        await f.write(output.getvalue())

    try:
        async with aiofiles.open(filename, "rb") as f:
            await update.message.reply_document(document=f, filename=filename)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при отправке файла: {e}")
    os.remove(filename)

# Обновление хендлеров
async def main():
    try:
        pool = await connect_with_retry()
        app = ApplicationBuilder().token(TOKEN).build()
        app.bot_data["db_pool"] = pool

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(CommandHandler("download", download_table))
        app.add_handler(CommandHandler("clear_table", clear_table))

        logger.info("✅ Бот успешно запущен!")
        await app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")

if __name__ == "__main__":
    uvloop.install()
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    loop.create_task(main())
    loop.run_forever()

