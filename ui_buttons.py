from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

import app as base

BTN_NEW = "🆕 Новий сектор"
BTN_LOCATION = "📍 Надіслати геолокацію"
BTN_MANUAL = "⌨️ Ввести адресу або координати"
BTN_HELP = "ℹ️ Допомога"
BTN_CANCEL = "❌ Скасувати"
BTN_BACK = "⬅️ Назад"
BTN_CUSTOM_AZ = "✏️ Ввести інший азимут"
BTN_USERS = "👥 Користувачі"

AZIMUTH_BUTTONS = [
    ["0° Північ", "45° Пн-Сх", "90° Схід"],
    ["135° Пд-Сх", "180° Південь", "225° Пд-Зх"],
    ["270° Захід", "315° Пн-Зх"],
]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_NEW)], [KeyboardButton(BTN_HELP)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію",
    )


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_NEW)], [KeyboardButton(BTN_USERS), KeyboardButton(BTN_HELP)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію",
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_LOCATION, request_location=True)],
            [KeyboardButton(BTN_MANUAL)],
            [KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть спосіб введення точки",
    )


def azimuth_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text) for text in row] for row in AZIMUTH_BUTTONS]
    rows.extend([
        [KeyboardButton(BTN_CUSTOM_AZ)],
        [KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)],
    ])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть або введіть азимут",
    )


def keyboard_for_user(user_id: int | None = None):
    try:
        import user_control
        if user_id is not None and user_control.is_admin(user_id):
            return admin_keyboard()
    except Exception:
        pass
    return main_keyboard()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await base.deny(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Оберіть, як зазначити точку базової станції:",
        reply_markup=location_keyboard(),
    )
    return base.WAIT_LOCATION


async def manual_location_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Введіть адресу, координати або посилання на карту.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Наприклад: 50.9500, 28.6500",
        ),
    )
    return base.WAIT_LOCATION


async def location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await base.location(update, context)
    if result == base.WAIT_AZIMUTH:
        await update.effective_message.reply_text(
            "Оберіть готовий азимут або введіть власне значення.",
            reply_markup=azimuth_keyboard(),
        )
    return result


async def custom_azimuth_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Введіть азимут від 0° до 359.99°. Радіус можна вказати другим числом: 125 8.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Наприклад: 125 або 125 8",
        ),
    )
    return base.WAIT_AZIMUTH


def normalize_azimuth_button(text: str) -> str:
    mapping = {
        "0° Північ": "0",
        "45° Пн-Сх": "45",
        "90° Схід": "90",
        "135° Пд-Сх": "135",
        "180° Південь": "180",
        "225° Пд-Зх": "225",
        "270° Захід": "270",
        "315° Пн-Зх": "315",
    }
    return mapping.get(text, text)


async def azimuth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message and update.effective_message.text:
        update.effective_message.text = normalize_azimuth_button(update.effective_message.text)
    result = await base.azimuth(update, context)
    return result


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id if update.effective_user else None
    await update.effective_message.reply_text(
        "Дію скасовано.",
        reply_markup=keyboard_for_user(user_id),
    )
    return ConversationHandler.END


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    await update.effective_message.reply_text(
        "<b>Як користуватися ботом</b>\n"
        "1. Натисніть «Новий сектор».\n"
        "2. Надішліть геолокацію або введіть адресу/координати.\n"
        "3. Оберіть азимут або введіть власний.\n"
        "4. Отримайте фото та інтерактивну карту сектора 120°.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_for_user(user_id),
    )


def build_bot():
    application = base.ApplicationBuilder().token(base.settings().token).concurrent_updates(False).build()

    new_filter = filters.Regex(f"^{BTN_NEW}$")
    help_filter = filters.Regex(f"^{BTN_HELP}$")
    cancel_filter = filters.Regex(f"^{BTN_CANCEL}$")
    back_filter = filters.Regex(f"^{BTN_BACK}$")
    manual_filter = filters.Regex(f"^{BTN_MANUAL}$")
    custom_az_filter = filters.Regex(f"^{BTN_CUSTOM_AZ}$")
    az_buttons_filter = filters.Regex(r"^(0° Північ|45° Пн-Сх|90° Схід|135° Пд-Сх|180° Південь|225° Пд-Зх|270° Захід|315° Пн-Зх)$")

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(new_filter, start),
        ],
        states={
            base.WAIT_LOCATION: [
                MessageHandler(new_filter, start),
                MessageHandler(cancel_filter, cancel),
                MessageHandler(back_filter, start),
                MessageHandler(help_filter, help_handler),
                MessageHandler(manual_filter, manual_location_prompt),
                MessageHandler(filters.LOCATION, location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, location),
            ],
            base.WAIT_AZIMUTH: [
                MessageHandler(cancel_filter, cancel),
                MessageHandler(back_filter, start),
                MessageHandler(help_filter, help_handler),
                MessageHandler(custom_az_filter, custom_azimuth_prompt),
                MessageHandler(az_buttons_filter, azimuth),
                MessageHandler(filters.TEXT & ~filters.COMMAND, azimuth),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            MessageHandler(cancel_filter, cancel),
            MessageHandler(new_filter, start),
        ],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(MessageHandler(help_filter, help_handler))
    return application


base.main_keyboard = main_keyboard
base.location_keyboard = location_keyboard
base.start = start
base.cancel = cancel
base.build_bot = build_bot
