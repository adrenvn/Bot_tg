import os
import logging
import pandas as pd
import asyncpg
import nest_asyncio
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# Применяем nest_asyncio для предотвращения проблем с event loop
nest_asyncio.apply()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Проверка токена
if not TOKEN:
    logging.error("Токен бота отсутствует. Проверьте .env файл.")
    raise ValueError("Токен не найден")

# Определение состояний
WAITING_VIDEO_LINKS, WAITING_RATING, WAITING_COMMENT = range(3)

# Подключение к БД
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)

# Пересоздание таблицы
async def recreate_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.effective_message.reply_text("❌ База данных недоступна")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS videos CASCADE")
        await conn.execute(
            """CREATE TABLE videos (
                id SERIAL PRIMARY KEY,
                link TEXT NOT NULL,
                total_score INT DEFAULT 0,
                avg_score FLOAT DEFAULT 0,
                comments TEXT DEFAULT '[]'
            )"""
        )
    await update.effective_message.reply_text("🔄 Таблица пересоздана")

# Скачивание данных
async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.effective_message.reply_text("❌ База данных недоступна")
        return

    async with db_pool.acquire() as conn:
        records = await conn.fetch("SELECT * FROM videos")
        df = pd.DataFrame(records, columns=["id", "link", "total_score", "avg_score", "comments"])
        df.to_csv("videos.csv", index=False)
        
        with open("videos.csv", "rb") as file:
            await update.effective_message.reply_document(document=InputFile(file, "videos.csv"))

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎥 Отправить видео", callback_data="send_video")],
        [InlineKeyboardButton("⭐ Начать оценку", callback_data="start_rating")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]
    await update.effective_message.reply_text(
        "Привет! Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    🆘 Команды:
    /start - Главное меню
    /download - Экспорт данных
    /recreate_table - Сброс базы данных
    """
    await update.effective_message.reply_text(help_text)

# Отправка видео
async def send_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.effective_message.reply_text("📤 Отправьте ссылки через пробел:")
    return WAITING_VIDEO_LINKS

async def receive_video_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("❌ Некорректный ввод")
        return ConversationHandler.END

    links = update.message.text.split()
    db_pool = context.bot_data.get("db_pool")
    
    if not db_pool:
        await update.effective_message.reply_text("❌ Ошибка базы данных")
        return ConversationHandler.END

    try:
        async with db_pool.acquire() as conn:
            for link in links:
                await conn.execute(
                    "INSERT INTO videos (link) VALUES ($1)", link.strip()
                )
        
        keyboard = [
            [InlineKeyboardButton("➕ Ещё видео", callback_data="send_video")],
            [InlineKeyboardButton("🏠 В меню", callback_data="start")],
        ]
        await update.effective_message.reply_text(
            "✅ Ссылки сохранены!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await update.effective_message.reply_text("🚫 Произошла ошибка")

    return ConversationHandler.END

# Настройка приложения
def main():
    app = Application.builder().token(TOKEN).build()

    # Conversation Handler для отправки видео
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(send_video_callback, pattern="^send_video$")],
        states={
            WAITING_VIDEO_LINKS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_video_links)
            ],
        },
        fallbacks=[],
    )

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("download", download))
    app.add_handler(CommandHandler("recreate_table", recreate_table))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(start, pattern="^start$"))
    app.add_handler(CallbackQueryHandler(help_command, pattern="^help$"))

    # Запуск бота
    async def on_startup(app: Application):
        app.bot_data["db_pool"] = await get_db_pool()
        logging.info("Бот запущен")

    app.run_polling(on_startup=on_startup)

if __name__ == "__main__":
    main()