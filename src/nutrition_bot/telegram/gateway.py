from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    Update,
    WebAppInfo,
)


def split_message(text: str, *, limit: int = 4032) -> tuple[str, ...]:
    """Split plain Telegram text without discarding data or splitting surrogate pairs.

    Reserve room for the part heading added by the durable outbox. Prefer complete
    lines, but full source names and provenance may themselves exceed one message.
    """
    if limit < 2:
        raise ValueError("A message must accommodate a Unicode character")
    parts: list[str] = []
    start = size = 0
    last_break: int | None = None
    position = 0
    while position < len(text):
        width = 2 if ord(text[position]) > 0xFFFF else 1
        if size + width > limit:
            end = last_break if last_break is not None else position
            parts.append(text[start:end])
            start = position = end
            size = 0
            last_break = None
            continue
        size += width
        position += 1
        if text[position - 1] == "\n":
            last_break = position
    parts.append(text[start:])
    return tuple(parts)


def keyboard_rows(buttons: list[dict[str, str]]) -> list[list[InlineKeyboardButton]]:
    """Respect intentional action groups; older payloads retain their original layout."""
    if all("row" in button for button in buttons):
        rows: dict[str, list[InlineKeyboardButton]] = {}
        for item in buttons:
            rows.setdefault(item["row"], []).append(
                InlineKeyboardButton(text=item["text"], callback_data=item["callback_data"])
            )
        return [rows[key] for key in sorted(rows, key=int)]
    return [
        [
            InlineKeyboardButton(text=item["text"], callback_data=item["callback_data"])
            for item in buttons[start : start + 3]
        ]
        for start in range(0, len(buttons), 3)
    ]


class RetryableError(Exception):
    def __init__(self, delay: float = 0):
        self.delay = delay


class RejectedError(Exception):
    """A permanent failure of one outgoing request."""


class FatalGatewayError(Exception):
    """Credentials/configuration or polling ownership needs operator attention."""


class Gateway(Protocol):
    async def preflight(self) -> None: ...

    async def configure_menu(self, chat_id: int) -> None: ...

    async def configure_miniapp(self, chat_id: int, url: str) -> None: ...

    async def poll(self, offset: int) -> list[Update]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        button_token: str | None,
        buttons: list[dict[str, str]] | None = None,
    ) -> int: ...

    async def download_photo(self, message: Message) -> bytes: ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...


class TelegramGateway:
    def __init__(self, bot: Bot):
        self.bot = bot

    @staticmethod
    def translate(exc: TelegramAPIError) -> Exception:
        if isinstance(exc, TelegramRetryAfter):
            return RetryableError(float(exc.retry_after))
        if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
            return RetryableError()
        if isinstance(exc, (TelegramUnauthorizedError, TelegramConflictError)):
            return FatalGatewayError()
        if isinstance(exc, (TelegramBadRequest, TelegramForbiddenError)):
            return RejectedError()
        return FatalGatewayError()

    async def preflight(self) -> None:
        try:
            info = await self.bot.get_webhook_info(request_timeout=15)
            if info.url:
                raise FatalGatewayError("A webhook is configured; long polling cannot start")
        except TelegramAPIError as exc:
            raise self.translate(exc) from None

    async def configure_menu(self, chat_id: int) -> None:
        try:
            await self.bot.set_my_commands(
                [
                    BotCommand(command=command, description=description)
                    for command, description in (
                        ("home", "Open daily menu"),
                        ("today", "Today's progress"),
                        ("favorites", "Saved meals"),
                        ("week", "Weekly report"),
                        ("settings", "Preferences and reminders"),
                        ("help", "Help and setup"),
                    )
                ],
                scope=BotCommandScopeChat(chat_id=chat_id),
                request_timeout=15,
            )
        except TelegramAPIError as exc:
            raise self.translate(exc) from None

    async def configure_miniapp(self, chat_id: int, url: str) -> None:
        try:
            await self.bot.set_chat_menu_button(
                chat_id=chat_id,
                menu_button=MenuButtonWebApp(text="Open diary", web_app=WebAppInfo(url=url)),
                request_timeout=15,
            )
        except TelegramAPIError as exc:
            raise self.translate(exc) from None

    async def poll(self, offset: int) -> list[Update]:
        try:
            return await self.bot.get_updates(
                offset=offset,
                timeout=25,
                limit=100,
                allowed_updates=["message", "edited_message", "callback_query"],
                request_timeout=35,
            )
        except TelegramAPIError as exc:
            raise self.translate(exc) from None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        button_token: str | None,
        buttons: list[dict[str, str]] | None = None,
    ) -> int:
        keyboard = None
        if buttons is not None:
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows(buttons))
        elif button_token:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Refresh status", callback_data=f"status:{button_token}"
                        )
                    ]
                ]
            )
        try:
            message = await self.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=keyboard, request_timeout=15
            )
            return message.message_id
        except TelegramAPIError as exc:
            raise self.translate(exc) from None

    async def download_photo(self, message: Message) -> bytes:
        from nutrition_bot.adapters.ai.photos import download_meal_photo

        return await download_meal_photo(self.bot, message)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            await self.bot.answer_callback_query(
                callback_query_id=callback_id, text=text, request_timeout=15
            )
        except TelegramAPIError as exc:
            raise self.translate(exc) from None
