"""Optional same-process HTTP UI. All writes use the normal durable inbox."""

import asyncio
import hashlib
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from importlib.resources import files
from typing import Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from aiogram.types import Update
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nutrition_bot.adapters.database.foods import normalize_food
from nutrition_bot.adapters.database.meals import get_meal
from nutrition_bot.adapters.database.schema import actions, inbox, outbox, profile
from nutrition_bot.application.service import QueueFullError, Service
from nutrition_bot.domain.food import Preparation, exact_decimal, grams_to_milligrams
from nutrition_bot.miniapp.auth import AuthenticationError, authenticate
from nutrition_bot.miniapp.reads import amount, dashboard, food_choices, meal_history

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; object-src 'none'; form-action 'self'; "
        "frame-ancestors https://web.telegram.org https://*.telegram.org"
    ),
}


class Portion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version_id: int = Field(gt=0, le=2**63 - 1)
    grams: str = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$", max_length=9)


class SourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["usda", "openfoodfacts"] = "usda"
    source_id: str = Field(pattern=r"^[0-9]{1,14}$")
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    preparation: Literal["raw", "cooked", "as_sold", "as_prepared"]


class SourcePortion(SourceSelection):
    grams: str = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$", max_length=9)


class RequestCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=36, max_length=36)
    action: Literal[
        "meal",
        "source_meal",
        "source_add",
        "label_handoff",
        "edit",
        "recipe_portion",
        "delete",
        "undo",
        "repeat",
        "favorite",
        "save_favorite",
    ]
    reference: str | None = Field(default=None, max_length=45)
    day: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    label: Literal["Breakfast", "Lunch", "Dinner", "Snack", "Meal"] = "Meal"
    items: list[Portion | SourcePortion] = Field(default_factory=list, max_length=10)
    name: str | None = Field(default=None, min_length=1, max_length=60, pattern=r"^[^=\n\r]+$")
    portion: str | None = Field(default=None, pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$")
    unit: Literal["g", "serving"] | None = None
    estimated: bool = False
    detach_recipe: bool = False
    source: SourceSelection | None = None

    def command(self) -> str:
        request_uuid = UUID(self.request_id)
        if str(request_uuid) != self.request_id or request_uuid.version != 4:
            raise ValueError("Use a new request identifier.")
        if self.detach_recipe and self.action != "edit":
            raise ValueError("Recipe conversion is only available for a full meal replacement.")
        if self.action == "source_add":
            if (
                self.source is None
                or self.items
                or self.portion is not None
                or self.unit is not None
            ):
                raise ValueError("Review the exact food source before adding it.")
            source = self.source
            return (
                f"/source-add {source.source_id} {source.hash} "
                f"{source.preparation} {source.provider}"
            )
        if self.source is not None:
            raise ValueError("Only adding a food accepts a source selection.")
        if self.action == "recipe_portion":
            if self.items or self.detach_recipe or not self.portion or self.unit is None:
                raise ValueError("Choose a recipe portion in its original unit.")
            maximum = 50_000_000 if self.unit == "g" else 1_000_000
            if not 0 < grams_to_milligrams(exact_decimal(self.portion)) <= maximum:
                raise ValueError("Choose a positive portion within the saved batch.")
            approximate = "about " if self.estimated else ""
            return f"/edit {self.meal_reference()} portion {approximate}{self.portion} {self.unit}"
        if self.portion is not None or self.unit is not None or self.estimated:
            raise ValueError("Only a recipe portion accepts this amount and unit.")
        if self.action in {"meal", "source_meal", "label_handoff", "edit"}:
            if not self.items and self.action != "label_handoff":
                raise ValueError("Choose a saved food and measured grams.")
            for item in self.items:
                if not 0 < grams_to_milligrams(exact_decimal(item.grams)) <= 50_000_000:
                    raise ValueError("Use measured grams between 0.001 and 50000.")
            if self.action in {"source_meal", "label_handoff"}:
                if not self.day:
                    raise ValueError("Choose the meal date.")
                if self.action == "label_handoff" and len(self.items) > 9:
                    raise ValueError("Leave room for the label food in the ten-entry meal.")
                if self.action == "source_meal" and not any(
                    isinstance(item, SourcePortion) for item in self.items
                ):
                    raise ValueError("Review a food source and choose the meal date.")
                payload = {
                    "label": self.label,
                    "date": self.day,
                    "items": [item.model_dump() for item in self.items],
                }
                prefix = "/source-label " if self.action == "label_handoff" else "/source-meal "
                return prefix + json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if any(isinstance(item, SourcePortion) for item in self.items):
                raise ValueError("Use the source meal action for reviewed new sources.")
            portions = "; ".join(
                f"{item.grams}g #{item.version_id}"
                for item in self.items
                if isinstance(item, Portion)
            )
            if self.action == "meal":
                prefix = f"{self.day} " if self.day else ""
                label = f"{self.label}: " if self.label != "Meal" else ""
                return f"/meal {prefix}{label}{portions}"
            replacement = "replace meal:" if self.detach_recipe else "replace:"
            return f"/edit {self.meal_reference()} {replacement} {portions}"
        if self.items:
            raise ValueError("This action does not accept food entries.")
        if self.action == "favorite":
            if not self.reference or not re.fullmatch(r"F[1-9][0-9]*v[1-9][0-9]*", self.reference):
                raise ValueError("Choose the current favorite.")
            return f"/eat {self.reference}"
        reference = self.meal_reference()
        if self.action == "save_favorite":
            if not self.name or not self.name.strip():
                raise ValueError("Give this favorite a name.")
            return f"/favorite save {self.name.strip()} = {reference}"
        return f"/{self.action} {reference}"

    def meal_reference(self) -> str:
        if not self.reference or not re.fullmatch(r"M[1-9][0-9]*r[1-9][0-9]*", self.reference):
            raise ValueError("Refresh the meal and choose its current revision.")
        return self.reference


def update_id(request_id: str, owner_id: int) -> int:
    digest = hashlib.sha256(f"bite-club-miniapp-v1:{owner_id}:{request_id}".encode()).digest()
    # Keep this namespace distinct from real positive Telegram updates and from
    # the scheduler's synthetic IDs. Full value is returned as a string to JS.
    return -((1 << 61) + (int.from_bytes(digest[:8], "big") & ((1 << 61) - 1)))


class MiniApp:
    def __init__(self, service: Service):
        self.service = service
        self.submit_lock = asyncio.Lock()
        self.requests: deque[float] = deque()
        self.source_requests: deque[float] = deque()

    @web.middleware
    async def secure(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        try:
            if request.path.startswith("/api/"):
                if any(
                    key.casefold() in {"initdata", "init_data", "token", "authorization"}
                    for key in request.query
                ):
                    raise web.HTTPBadRequest(text="Credentials belong in a header.")
                authenticate(
                    request.headers.get("X-Telegram-Init-Data", ""),
                    bot_token=self.service.settings.telegram_bot_token.get_secret_value(),
                    owner_id=self.service.settings.allowed_telegram_user_id,
                )
                now = time.monotonic()
                while self.requests and self.requests[0] < now - 60:
                    self.requests.popleft()
                if len(self.requests) >= 120:
                    raise web.HTTPTooManyRequests(text="Pause briefly and try again.")
                self.requests.append(now)
            response = await handler(request)
        except AuthenticationError:
            response = web.json_response(
                {"error": "Open Bite Club again from your private Telegram chat."}, status=401
            )
        except web.HTTPException as error:
            response = web.json_response(
                {"error": (error.text or "Request failed.")[:200]}, status=error.status
            )
        except (ValueError, ValidationError):
            response = web.json_response(
                {"error": "Check the food, amount and meal revision."}, status=400
            )
        except QueueFullError:
            response = web.json_response({"error": "The diary is busy; retry shortly."}, status=503)
        except Exception:
            # No request headers, launch credentials, body or traceback in logs.
            response = web.json_response(
                {"error": "The diary is temporarily unavailable."}, status=503
            )
        response.headers.update(SECURITY_HEADERS)
        return response

    async def static(self, request: web.Request) -> web.Response:
        name = request.match_info.get("asset", "index.html")
        mime = {"index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css"}
        if name not in mime:
            raise web.HTTPNotFound(text="Not found.")
        content = files("nutrition_bot.miniapp").joinpath("static", name).read_bytes()
        return web.Response(body=content, content_type=mime[name])

    async def overview(self, request: web.Request) -> web.Response:
        async with self.service.store.engine.connect() as connection:
            timezone = await connection.scalar(
                sa.select(profile.c.timezone).where(profile.c.id == 1)
            )
            timezone = timezone or self.service.settings.app_timezone
            today = datetime.now(ZoneInfo(timezone)).date()
            selected = date.fromisoformat(request.query.get("day", today.isoformat()))
            if selected > today or (today - selected).days > 366:
                raise web.HTTPBadRequest(text="Choose a date within the last year.")
            data = await dashboard(connection, selected, timezone)
            data["today"] = today.isoformat()
        return web.json_response(data)

    async def foods(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "").strip()
        if len(query) > 100:
            raise web.HTTPBadRequest(text="Use a shorter food name.")
        async with self.service.store.engine.connect() as connection:
            data = await food_choices(connection, query, preparation=self.preparation(request))
        return web.json_response({"foods": data})

    @staticmethod
    def preparation(request: web.Request) -> Preparation | None:
        value = request.query.get("preparation", "")
        if not value:
            return None
        if value not in {"raw", "cooked", "as_sold", "as_prepared"}:
            raise web.HTTPBadRequest(text="Choose raw, cooked, packaged or prepared food.")
        return cast(Preparation, value)

    def limit_source_requests(self) -> None:
        now = time.monotonic()
        while self.source_requests and self.source_requests[0] < now - 60:
            self.source_requests.popleft()
        if len(self.source_requests) >= 12:
            raise web.HTTPTooManyRequests(text="Pause briefly before searching another source.")
        self.source_requests.append(now)

    async def sources(self, request: web.Request) -> web.Response:
        self.limit_source_requests()
        query = " ".join(request.query.get("q", "").split())
        if not 1 <= len(query) <= 120:
            raise web.HTTPBadRequest(text="Enter a food name before searching sources.")
        result = await self.service.food_catalog.search(
            query, remote=True, preparation=self.preparation(request)
        )
        return web.json_response(result.model_dump(mode="json"))

    async def source_preview(self, request: web.Request) -> web.Response:
        self.limit_source_requests()
        provider = request.query.get("provider", "usda")
        if provider not in {"usda", "openfoodfacts"}:
            raise web.HTTPBadRequest(text="Choose a supported food source.")
        preview = await self.service.food_catalog.lookup(
            request.match_info["source_id"], provider=provider
        )
        result: dict[str, object] = preview.model_dump(mode="json")
        if preview.document is not None:
            async with self.service.store.engine.connect() as connection:
                normalized = await normalize_food(connection, preview.document.record)
            result["nutrients"] = [
                {"code": item.code, "unit": item.unit, "per_100g": amount(item.amount_scaled)}
                for item in normalized
            ]
        return web.json_response(result)

    async def history(self, request: web.Request) -> web.Response:
        before_text = request.query.get("before")
        before = int(before_text) if before_text else None
        if before is not None and not 0 < before <= 2**63 - 1:
            raise web.HTTPBadRequest(text="Refresh meal history.")
        async with self.service.store.engine.connect() as connection:
            data = await meal_history(connection, before)
        return web.json_response({"meals": data})

    async def submit(self, request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Use JSON.")
        raw = await request.read()
        command = RequestCommand.model_validate(json.loads(raw))
        text = command.command()
        identifier = update_id(command.request_id, self.service.settings.allowed_telegram_user_id)
        async with self.submit_lock:
            async with self.service.store.engine.connect() as connection:
                previous = (
                    (
                        await connection.execute(
                            sa.select(inbox).where(inbox.c.update_id == identifier)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if previous is not None:
                payload = previous["payload"]
                if not isinstance(payload, dict):
                    raise web.HTTPGone(text="This old request expired. Refresh the diary.")
                message = payload.get("message", {})
                if not isinstance(message, dict) or message.get("text") != text:
                    raise web.HTTPConflict(
                        text="A request identifier cannot be reused for changed input."
                    )
                return web.json_response(
                    {"request": command.request_id, "status": previous["status"]}
                )
            if command.action == "edit" and not command.detach_recipe:
                matched = re.fullmatch(r"M([1-9][0-9]*)r([1-9][0-9]*)", command.meal_reference())
                assert matched is not None
                async with self.service.store.engine.connect() as connection:
                    meal = await get_meal(connection, int(matched[1]))
                if any(item.recipe_share for item in meal.items):
                    raise web.HTTPConflict(
                        text="Edit this recipe's cooked portion or servings. Converting it to "
                        "direct foods requires an explicit full replacement."
                    )
            synthetic = Update.model_validate(
                {
                    "update_id": identifier,
                    "message": {
                        # Telegram message IDs are 32-bit positive values. Use
                        # a separate positive int64 namespace for ledger source
                        # identities while the inbox update itself is negative.
                        "message_id": -identifier,
                        "date": int(time.time()),
                        "from": {
                            "id": self.service.settings.allowed_telegram_user_id,
                            "is_bot": False,
                            "first_name": "Mini App",
                        },
                        "chat": {
                            "id": self.service.settings.allowed_telegram_chat_id,
                            "type": "private",
                        },
                        "text": text,
                    },
                }
            )
            await self.service.accept_internal([synthetic])
        return web.json_response({"request": command.request_id, "status": "pending"}, status=202)

    async def receipt(self, request: web.Request) -> web.Response:
        request_id = request.match_info["request"]
        parsed = UUID(request_id)
        if str(parsed) != request_id or parsed.version != 4:
            raise web.HTTPNotFound(text="Request not found.")
        identifier = update_id(request_id, self.service.settings.allowed_telegram_user_id)
        async with self.service.store.engine.connect() as connection:
            status = await connection.scalar(
                sa.select(inbox.c.status).where(inbox.c.update_id == identifier)
            )
            if status is None:
                raise web.HTTPNotFound(text="Request not found.")
            rows = (
                (
                    await connection.execute(
                        sa.select(outbox.c.payload, outbox.c.status)
                        .join(actions, actions.c.key == outbox.c.action_key)
                        .where(
                            actions.c.update_id == identifier,
                            outbox.c.kind == "message",
                            outbox.c.owner_user_id
                            == self.service.settings.allowed_telegram_user_id,
                            outbox.c.chat_id == self.service.settings.allowed_telegram_chat_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return web.json_response(
            {
                "status": status,
                "replies": [
                    {
                        "text": row["payload"].get("text", ""),
                        "delivery": row["status"],
                        "needs_approval": bool(row["payload"].get("draft_id")),
                        "continue_in_chat": bool(row["payload"].get("label_handoff")),
                    }
                    for row in rows
                    if isinstance(row["payload"], dict)
                ],
            }
        )


def create_app(service: Service) -> web.Application:
    adapter = MiniApp(service)
    application = web.Application(middlewares=[adapter.secure], client_max_size=16_384)
    application.router.add_get("/", adapter.static)
    application.router.add_get("/assets/{asset}", adapter.static)
    application.router.add_get("/api/dashboard", adapter.overview)
    application.router.add_get("/api/foods", adapter.foods)
    application.router.add_get("/api/food-sources", adapter.sources)
    application.router.add_get("/api/food-sources/{source_id}", adapter.source_preview)
    application.router.add_get("/api/meals", adapter.history)
    application.router.add_post("/api/commands", adapter.submit)
    application.router.add_get("/api/requests/{request}", adapter.receipt)
    return application


async def start_miniapp(
    service: Service, *, host: str = "127.0.0.1", port: int = 8080
) -> web.AppRunner:
    runner = web.AppRunner(create_app(service), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host=host, port=port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
