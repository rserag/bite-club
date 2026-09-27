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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update


class RetryableError(Exception):
    def __init__(self, delay: float = 0):
        self.delay = delay


class RejectedError(Exception):
    """A permanent failure of one outgoing request."""


class FatalGatewayError(Exception):
    """Credentials/configuration or polling ownership needs operator attention."""


class Gateway(Protocol):
    async def preflight(self) -> None: ...

    async def poll(self, offset: int) -> list[Update]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        button_token: str | None,
        buttons: list[dict[str, str]] | None = None,
    ) -> int: ...

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
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=item["text"], callback_data=item["callback_data"])
                        for item in buttons
                    ]
                ]
            )
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

    async def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            await self.bot.answer_callback_query(
                callback_query_id=callback_id, text=text, request_timeout=15
            )
        except TelegramAPIError as exc:
            raise self.translate(exc) from None
