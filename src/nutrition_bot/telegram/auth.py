from aiogram.types import Message, Update

from nutrition_bot.config import BotSettings


def authorized(update: Update, settings: BotSettings) -> bool:
    callback = update.callback_query
    if callback:
        message = callback.message
        return bool(
            isinstance(message, Message)
            and message.chat.type == "private"
            and message.chat.id == settings.allowed_telegram_chat_id
            and callback.from_user.id == settings.allowed_telegram_user_id
            and not callback.from_user.is_bot
            and message.from_user
            and message.from_user.id == settings.bot_id
            and not message.business_connection_id
        )
    message = update.message or update.edited_message
    return bool(
        message
        and message.chat.type == "private"
        and message.chat.id == settings.allowed_telegram_chat_id
        and message.from_user
        and message.from_user.id == settings.allowed_telegram_user_id
        and not message.from_user.is_bot
        and not message.business_connection_id
    )
