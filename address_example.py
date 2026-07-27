from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup

import ui_buttons


async def manual_location_prompt(update, context):
    if ui_buttons.is_duplicate(context, "manual-location", 1.5):
        return ui_buttons.base.WAIT_LOCATION
    await update.effective_message.reply_text(
        "Введіть адресу, координати або посилання на карту.\n\n"
        "Приклад адреси: <code>Житомир, вул. Грушевського, 5</code>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(ui_buttons.BTN_BACK), KeyboardButton(ui_buttons.BTN_CANCEL)]],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Житомир, вул. Грушевського, 5",
        ),
    )
    return ui_buttons.base.WAIT_LOCATION


ui_buttons.manual_location_prompt = manual_location_prompt
