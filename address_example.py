from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup

import ui_buttons


async def manual_location_prompt(update, context):
    if ui_buttons.is_duplicate(context, "manual-location", 1.5):
        return ui_buttons.base.WAIT_LOCATION
    await update.effective_message.reply_text(
        "Введіть адресу у довільному форматі, координати або посилання на карту.\n\n"
        "Приклади:\n"
        "<code>Житомир Грушевського 5</code>\n"
        "<code>Грушевського 5, Житомир</code>\n"
        "<code>м. Житомир, вул. Грушевського, буд. 5</code>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(ui_buttons.BTN_BACK), KeyboardButton(ui_buttons.BTN_CANCEL)]],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Наприклад: Житомир Грушевського 5",
        ),
    )
    return ui_buttons.base.WAIT_LOCATION


ui_buttons.manual_location_prompt = manual_location_prompt

# Новий спрощений сценарій завантажується останнім і повністю замінює старе меню.
import simple_flow  # noqa: E402,F401
