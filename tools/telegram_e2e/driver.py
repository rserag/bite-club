import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from tools.telegram_e2e.config import InfrastructureError, Settings, SetupError
from tools.telegram_e2e.reporting import Recorder

T = TypeVar("T")


@dataclass(frozen=True)
class Message:
    id: int
    text: str
    buttons: tuple[str, ...] = ()
    raw: Any = None
    edited: bool = False


class Collector:
    """Event sequence watermark handles edits without relying on increasing message IDs."""

    def __init__(self, bot_id: int) -> None:
        self.bot_id = bot_id
        self.events: list[Message] = []
        self.changed = asyncio.Event()
        self.seen: set[tuple[int, str, bool, tuple[str, ...]]] = set()

    def feed(self, chat_id: int, sender_id: int, message: Message) -> None:
        if chat_id != self.bot_id or sender_id != self.bot_id:
            return
        key = (message.id, message.text, message.edited, message.buttons)
        if key in self.seen:
            return
        self.seen.add(key)
        self.events.append(message)
        self.changed.set()

    async def expect(self, since: int, contains: str, seconds: float) -> Message:
        try:
            async with asyncio.timeout(seconds):
                while True:
                    self.changed.clear()
                    fresh = self.events[since:]
                    if fresh:
                        if len(fresh) != 1:
                            raise AssertionError("ambiguous_or_duplicate_bot_responses")
                        result = fresh[0]
                        if contains not in result.text:
                            raise AssertionError(f"Expected response containing: {contains}")
                        return result
                    await self.changed.wait()
        except TimeoutError:
            raise InfrastructureError("response_timeout_mutation_not_retried") from None


class Driver:
    def __init__(self, settings: Settings, client: Any, recorder: Recorder) -> None:
        self.settings, self.client, self.recorder = settings, client, recorder
        self.collector = Collector(settings.bot_id)
        self.peer: Any = None
        self.last_action = 0.0
        self.consumed = 0
        self.last_sent: Message | None = None
        self.handler: Any = None

    async def network(self, operation: Callable[[], Awaitable[T]]) -> T:
        from telethon.errors import FloodWaitError, RPCError

        try:
            async with asyncio.timeout(self.settings.step_timeout):
                return await operation()
        except FloodWaitError as exc:
            # Stop this run; do not retry the mutation or evade Telegram's wait.
            raise InfrastructureError(f"telegram_flood_wait_seconds_{exc.seconds}") from None
        except (RPCError, TimeoutError, OSError):
            raise InfrastructureError("telegram_transport_error") from None

    async def connect(self) -> None:
        from telethon import events

        await self.network(self.client.connect)
        if not await self.network(self.client.is_user_authorized):
            raise SetupError("login_required")
        me = await self.network(self.client.get_me)
        if me is None or me.bot or me.id != self.settings.user_id:
            raise SetupError("test_user_identity_mismatch")
        entity = await self.network(lambda: self.client.get_entity(self.settings.bot_username))
        if not entity.bot or entity.id != self.settings.bot_id:
            raise SetupError("test_bot_identity_mismatch")
        self.peer = entity

        async def receive(event: Any) -> None:
            message = self.convert(
                event.message, edited=isinstance(event, events.MessageEdited.Event)
            )
            before = len(self.collector.events)
            self.collector.feed(event.chat_id, event.sender_id, message)
            if len(self.collector.events) != before:
                self.recorder.add("bot_edit" if message.edited else "bot_message", message.text)

        self.handler = receive
        self.client.add_event_handler(receive, events.NewMessage(chats=entity, incoming=True))
        self.client.add_event_handler(receive, events.MessageEdited(chats=entity, incoming=True))

    @staticmethod
    def convert(raw: Any, *, edited: bool = False) -> Message:
        return Message(
            raw.id,
            raw.raw_text or "",
            tuple(button.text for row in (raw.buttons or []) for button in row),
            raw,
            edited,
        )

    def assert_idle(self) -> None:
        if len(self.collector.events) != self.consumed:
            raise AssertionError("unexpected_late_bot_message")

    async def step(
        self, label: str, operation: Callable[[], Awaitable[Any]], expected: str
    ) -> Message:
        await asyncio.sleep(
            max(0, self.settings.pace_seconds - (time.monotonic() - self.last_action))
        )
        self.assert_idle()
        since = len(self.collector.events)
        self.recorder.add("action", label)
        self.recorder.add("expected", expected)
        try:
            await self.network(operation)
        finally:
            self.last_action = time.monotonic()
        result = await self.collector.expect(since, expected, self.settings.step_timeout)
        self.consumed = len(self.collector.events)
        return result

    async def send(self, text: str, expected: str, *, reply_to: Message | None = None) -> Message:
        async def operation() -> None:
            raw = await self.client.send_message(
                self.peer, text, reply_to=reply_to.id if reply_to else None, parse_mode=None
            )
            self.last_sent = self.convert(raw)

        return await self.step(text, operation, expected)

    async def edit(self, message: Message, text: str, expected: str) -> Message:
        return await self.step(
            "edit: " + text,
            lambda: self.client.edit_message(self.peer, message.id, text, parse_mode=None),
            expected,
        )

    async def click(self, message: Message, label: str, expected: str) -> Message:
        if message.buttons.count(label) != 1:
            raise AssertionError("button_missing_or_ambiguous: " + label)

        async def operation() -> None:
            acknowledgement = await message.raw.click(text=label)
            if acknowledgement is None:
                raise AssertionError("callback_acknowledgement_missing")
            self.recorder.add(
                "callback_ack", getattr(acknowledgement, "message", "") or "acknowledged"
            )

        return await self.step("click: " + label, operation, expected)

    async def close(self) -> None:
        if self.handler is not None:
            self.client.remove_event_handler(self.handler)
        await self.client.disconnect()
