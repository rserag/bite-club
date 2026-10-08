from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nutrition_bot.telegram.gateway import TelegramGateway, split_message


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


@pytest.mark.parametrize(
    "actions, expected",
    [
        (
            [
                ("Details", "2"),
                ("Log food", "0"),
                ("All food logged", "1"),
                ("Not all logged", "1"),
            ],
            [["Log food"], ["All food logged", "Not all logged"], ["Details"]],
        ),
        (
            [
                ("Log again", "0"),
                ("Edit", "1"),
                ("Undo edit", "1"),
                ("Details", "2"),
                ("More", "2"),
            ],
            [["Log again"], ["Edit", "Undo edit"], ["Details", "More"]],
        ),
        (
            [("Restore meal", "0"), ("Details", "2")],
            [["Restore meal"], ["Details"]],
        ),
    ],
)
async def test_deliberate_action_groups_reach_telegram_unchanged(actions, expected):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    buttons = [
        {"text": label, "callback_data": f"opaque:{i}", "row": row}
        for i, (label, row) in enumerate(actions)
    ]
    await TelegramGateway(bot).send_message(101, "Synthetic summary", "opaque", buttons)
    keyboard = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    assert [[button.text for button in row] for row in keyboard] == expected
    assert {button.callback_data for row in keyboard for button in row} == {
        button["callback_data"] for button in buttons
    }


@pytest.mark.parametrize("text", ["A\n" * 5000, "𝓐" * 5000, "x" * 8000, "Rice\n" + "𝓐" * 5000])
def test_message_parts_preserve_every_source_character_and_telegram_size(text):
    parts = split_message(text)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert all(0 < len(part.encode("utf-16-le")) // 2 <= 4032 for part in parts)


def test_message_split_prefers_whole_lines_when_they_fit():
    first = "Source: " + "x" * 2000 + "\n"
    second = "Basis: " + "y" * 2000 + "\n"
    assert split_message(first + second, limit=2100) == (first, second)
