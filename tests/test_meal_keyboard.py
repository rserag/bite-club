from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nutrition_bot.telegram.gateway import TelegramGateway


async def test_meal_keyboard_passes_only_supplied_opaque_actions_to_telegram():
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    gateway = TelegramGateway(bot)
    buttons = [
        {"text": "Edit", "callback_data": "meal:edit:syntheticopaque"},
        {"text": "Delete", "callback_data": "meal:delete:syntheticopaque"},
        {"text": "Undo", "callback_data": "meal:undo:syntheticopaque"},
    ]
    assert await gateway.send_message(101, "Synthetic receipt", "syntheticopaque", buttons) == 77
    args = bot.send_message.call_args.kwargs
    assert args["text"] == "Synthetic receipt"
    keyboard = args["reply_markup"].inline_keyboard[0]
    assert [(button.text, button.callback_data) for button in keyboard] == [
        (item["text"], item["callback_data"]) for item in buttons
    ]
    assert all(button.url is None for button in keyboard)


@pytest.mark.parametrize("token", [None, "statusopaque"])
async def test_status_keyboard_compatibility(token):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    await TelegramGateway(bot).send_message(101, "Synthetic status", token)
    keyboard = bot.send_message.call_args.kwargs["reply_markup"]
    if token is None:
        assert keyboard is None
    else:
        assert keyboard.inline_keyboard[0][0].callback_data == "status:statusopaque"
