import os
import logging
import asyncpg
import nest_asyncio
import pandas as pd
import re
from dotenv import load_dotenv
from tempfile import NamedTemporaryFile
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)

nest_asyncio.apply()

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
# Получаем список ID администраторов из переменной окружения ADMIN_IDS (разделенных запятыми)
ADMIN_IDS = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMIN_IDS.split(",") if x.strip().isdigit()]

# Состояния разговора
WAITING_VIDEO_LINKS, WAITING_SCORE, WAITING_COMMENT = range(3)
URL_REGEX = re.compile(r'^https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)$')

async def get_db_pool():
    try:
        logger.info("Создание пула соединений с базой данных")
        return await asyncpg.create_pool(DATABASE_URL)
    except Exception as e:
        logger.error(f"Ошибка при создании пула соединений: {e}")
        return None

async def post_init(application: Application) -> None:
    application.bot_data["db_pool"] = await get_db_pool()
    if not application.bot_data["db_pool"]:
        logger.error("Критическая ошибка: база данных недоступна. Бот останавливается.")
        raise RuntimeError("Database connection failed")
    async with application.bot_data["db_pool"].acquire() as conn:
        # Создание таблицы (исправленный синтаксис PRIMARY KEY)
        await conn.execute("""
CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    link TEXT NOT NULL UNIQUE,
    total_score INTEGER DEFAULT 0,
    ratings_count INTEGER DEFAULT 0,
    avg_score FLOAT DEFAULT 0,
    comments TEXT[] DEFAULT ARRAY[]::TEXT[]
)
        """)
    logger.info("База данных подключена и таблица создана успешно")

def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎥 Отправить видео", callback_data='send_video_prompt')],
        [InlineKeyboardButton("⭐ Начать оценку", callback_data='start_review')],
        [InlineKeyboardButton("📥 Скачать таблицу", callback_data='download')],
        [InlineKeyboardButton("🧹 Очистить таблицу", callback_data='clear_table')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    markup = get_main_menu_keyboard()
    if update.message:
        await update.message.reply_text("Привет! Выберите действие:", reply_markup=markup)
    else:
        await update.callback_query.message.reply_text("Привет! Выберите действие:", reply_markup=markup)
    return ConversationHandler.END

async def send_video_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Отправьте ссылки через пробел:")
    return WAITING_VIDEO_LINKS

async def receive_video_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        await update.message.reply_text("Ошибка: отправьте текст со ссылками!")
        return WAITING_VIDEO_LINKS
    
    links = [link.strip() for link in update.message.text.split()]
    invalid_links = [link for link in links if not URL_REGEX.match(link)]
    
    if invalid_links:
        await update.message.reply_text(
            f"Некорректные ссылки: {', '.join(invalid_links)}\n"
            "Пример: http://example.com или https://www.site.com/path"
        )
        return WAITING_VIDEO_LINKS
    
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.message.reply_text("Ошибка подключения к БД!")
        return ConversationHandler.END
    
    async with db_pool.acquire() as conn:
        for link in links:
            await conn.execute("""
                INSERT INTO videos (link) VALUES ($1) ON CONFLICT (link) DO NOTHING
            """, link)
    
    await update.message.reply_text("✅ Ссылки сохранены!")
    
    # Предлагаем добавить ещё видео или вернуться в меню
    keyboard = [
        [InlineKeyboardButton("Отправить ещё видео", callback_data='send_video_prompt')],
        [InlineKeyboardButton("Вернуться в меню", callback_data='back_to_menu')]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Хотите отправить ещё видео или вернуться в меню?", reply_markup=markup)
    
    return ConversationHandler.END

async def start_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.callback_query.answer("Ошибка подключения к БД!")
        return ConversationHandler.END
    return await ask_for_rating(update, context)

async def ask_for_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_pool = context.bot_data.get("db_pool")
    async with db_pool.acquire() as conn:
        video = await conn.fetchrow("SELECT link FROM videos WHERE ratings_count = 0 LIMIT 1")

    # Если больше нет видео для оценки
    if not video:
        keyboard = [
            [InlineKeyboardButton("Вернуться в меню", callback_data='back_to_menu')]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        text = "Спасибо, вы оценили все видео! 🎉"
        if update.callback_query:
            await update.callback_query.message.reply_text(text, reply_markup=markup)
        else:
            await update.message.reply_text(text, reply_markup=markup)
        return ConversationHandler.END

    context.user_data["current_video"] = video["link"]
    text_to_send = f"Оцените видео от 1 до 10:\n{video['link']}"
    
    if update.callback_query:
        await update.callback_query.message.reply_text(text_to_send)
    else:
        await update.message.reply_text(text_to_send)
    return WAITING_SCORE

async def receive_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rating = int(update.message.text)
        if rating < 1 or rating > 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Ошибка: введите число от 1 до 10.")
        return WAITING_SCORE
    
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.message.reply_text("Ошибка подключения к БД!")
        return ConversationHandler.END
    
    video_link = context.user_data.get("current_video")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE videos
            SET total_score = total_score + $1,
                ratings_count = ratings_count + 1,
                avg_score = (total_score + $1)::FLOAT / (ratings_count + 1)
            WHERE link = $2
        """, rating, video_link)

    await update.message.reply_text("Теперь оставьте комментарий (мин. 15 символов):")
    return WAITING_COMMENT

async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if len(comment) < 15:
        await update.message.reply_text("Комментарий слишком короткий! Введите минимум 15 символов.")
        return WAITING_COMMENT

    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.message.reply_text("Ошибка подключения к БД!")
        return ConversationHandler.END

    video_link = context.user_data.get("current_video")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE videos
            SET comments = array_append(comments, $1)
            WHERE link = $2
        """, comment, video_link)

    await update.message.reply_text("✅ Комментарий сохранён!")
    return await ask_for_rating(update, context)

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Доступ к скачиванию таблицы только для администраторов
    if update.effective_user.id not in ADMIN_IDS:
        await update.callback_query.answer("У вас нет доступа к этой функции.", show_alert=True)
        return
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.callback_query.answer("Ошибка подключения к БД!")
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM videos")
    if not rows:
        await update.callback_query.message.reply_text("Таблица пуста!")
        return

    df = pd.DataFrame(rows, columns=["id", "link", "total_score", "ratings_count", "avg_score", "comments"])
    with NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        df.to_csv(tmp.name, index=False, sep=";", encoding="utf-8-sig")
        with open(tmp.name, "rb") as f:
            await update.callback_query.message.reply_document(
                document=InputFile(f, filename="videos.csv"),
                caption="📊 Текущие данные:"
            )
    os.unlink(tmp.name)

async def clear_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Доступ к очистке таблицы только для администраторов
    if update.effective_user.id not in ADMIN_IDS:
        await update.callback_query.answer("У вас нет доступа к этой функции.", show_alert=True)
        return
    db_pool = context.bot_data.get("db_pool")
    if not db_pool:
        await update.callback_query.message.reply_text("Ошибка подключения к БД!")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE videos")
    await update.callback_query.message.reply_text("Таблица очищена!")

# Новый колбэк: "Вернуться в меню"
async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    markup = get_main_menu_keyboard()
    await update.callback_query.message.reply_text("Привет! Выберите действие:", reply_markup=markup)

# Новый колбэк: "Помощь"
async def help_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    help_text = (
        "Это бот для работы с видео:\n\n"
        "• <b>Отправить видео</b> – добавить ссылки на видео в базу данных.\n"
        "• <b>Начать оценку</b> – оценить видео и оставить комментарий.\n"
        "• <b>Скачать таблицу</b> – получить CSV-файл с данными (поддерживается кириллица). (Доступ только для администраторов.)\n"
        "• <b>Очистить таблицу</b> – удалить все записи из базы. (Доступ только для администраторов.)\n\n"
        "Если у вас есть вопросы, пишите разработчику."
    )
    await update.callback_query.message.reply_text(help_text, parse_mode="HTML")

# Новая функция: добавление администратора через команду /add_admin
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_admin <ID>")
        return
    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный формат ID.")
        return
    if new_admin_id in ADMIN_IDS:
        await update.message.reply_text("Этот ID уже является администратором.")
    else:
        ADMIN_IDS.append(new_admin_id)
        await update.message.reply_text(f"ID {new_admin_id} добавлен в список администраторов.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Произошло исключение: {context.error}")
    if update and update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Произошла ошибка, попробуйте позже.")

# ConversationHandler для отправки видео и оценки
conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(send_video_prompt, pattern='^send_video_prompt$'),
        CallbackQueryHandler(start_review, pattern='^start_review$')
    ],
    states={
        WAITING_VIDEO_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_video_links)],
        WAITING_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rating)],
        WAITING_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment)]
    },
    fallbacks=[CommandHandler('cancel', lambda u, c: ConversationHandler.END)]
)

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(download, pattern='^download$'))
    app.add_handler(CallbackQueryHandler(clear_table, pattern='^clear_table$'))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(CallbackQueryHandler(help_section, pattern='^help$'))
    app.add_handler(CommandHandler("add_admin", add_admin))
    
    app.add_error_handler(error_handler)
    
    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
