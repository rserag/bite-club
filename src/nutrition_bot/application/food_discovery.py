"""Interactive food discovery. Source previews never imply food consumption."""

import re
import time
from dataclasses import replace
from datetime import date, datetime
from typing import Any, Literal

import sqlalchemy as sa
from aiogram.types import Message
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.meals import MealItemInput, create_meal
from nutrition_bot.adapters.database.schema_food_discovery import food_lookup_jobs
from nutrition_bot.application.food_catalog import (
    SearchResult,
    SourcePreview,
    accept_cached_source,
    infer_preparation,
    local_food_search,
    read_cached_source,
    validate_source_id,
)
from nutrition_bot.application.meal_conversation import (
    MealReply,
    _date_timestamp,
    _resolve_items,
    receipt,
)
from nutrition_bot.domain.food import FrozenModel, PositiveAmount, Preparation, grams_to_milligrams
from nutrition_bot.domain.meal_draft_text import parse_draft_meal
from nutrition_bot.domain.meal_text import MealTextError, MeasuredFood, ParsedMeal, parse_meal

FLOW_TTL_SECONDS = 1800


def food_search_input(
    text: str, payload: dict[str, Any], reference: datetime
) -> tuple[str, dict[str, Any]]:
    """Retain amounts already supplied; providers receive only the parsed food name."""
    try:
        parsed = parse_draft_meal(text, reference.date())
    except MealTextError:
        return text, payload
    first, *remaining = parsed.items
    updated = dict(payload)
    if first.original_quantity is not None and first.original_unit is not None:
        updated["quantity"] = (
            ("about " if first.estimate_basis else "")
            + first.original_quantity
            + first.original_unit
        )
    if remaining:
        updated["pending"] = [
            {
                "query": item.query,
                "quantity": (
                    ("about " if item.estimate_basis else "")
                    + item.original_quantity
                    + item.original_unit
                    if item.original_quantity is not None and item.original_unit is not None
                    else None
                ),
            }
            for item in remaining
        ]
    if "date" not in updated or re.match(
        r"^(?:today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s|$)", text, re.IGNORECASE
    ):
        updated["date"] = parsed.local_date.isoformat()
    if parsed.label != "Meal":
        updated["label"] = parsed.label
    return first.query, updated


class SourceMealItem(FrozenModel):
    version_id: int | None = Field(default=None, strict=True, gt=0, le=2**63 - 1)
    source_id: str | None = Field(default=None, min_length=1, max_length=14)
    provider: Literal["usda", "openfoodfacts"] = "usda"
    hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    preparation: Preparation | None = None
    grams: PositiveAmount

    @model_validator(mode="after")
    def validate_reference(self) -> "SourceMealItem":
        source = self.source_id is not None
        if source == (self.version_id is not None):
            raise ValueError("Choose one saved food or reviewed source")
        if source:
            assert self.source_id is not None
            if validate_source_id(self.source_id, self.provider) != self.source_id:
                raise ValueError("Use the normalized source identifier")
            if self.hash is None or self.preparation in (None, "unspecified"):
                raise ValueError("Review the exact source and preparation first")
        elif self.hash is not None or self.preparation is not None:
            raise ValueError("Saved foods use their immutable version")
        if grams_to_milligrams(self.grams) > 50_000_000:
            raise ValueError("Amount exceeds 50000 grams")
        return self


class SourceMeal(FrozenModel):
    label: str = Field(default="Meal", min_length=1, max_length=120)
    date: date
    items: tuple[SourceMealItem, ...] = Field(min_length=1, max_length=10)


class SourceLabelHandoff(FrozenModel):
    label: str = Field(default="Meal", min_length=1, max_length=120)
    date: date
    items: tuple[SourceMealItem, ...] = Field(default=(), max_length=9)


async def source_label_handoff(
    connection: AsyncConnection,
    request: SourceLabelHandoff,
    message: Message,
    *,
    reference: datetime,
) -> MealReply:
    from nutrition_bot.application.label_guide import start

    if request.date > reference.date() or message.from_user is None:
        raise ValueError("Choose a valid meal context")
    items: list[dict[str, Any]] = []
    for item in request.items:
        quantity = format(item.grams, "f") + "g"
        if item.source_id is not None:
            preview = await read_cached_source(
                connection, item.source_id, time.time(), provider=item.provider
            )
            if preview is None or preview.content_sha256 != item.hash or preview.document is None:
                raise ValueError("Review the source again")
            document = preview.document
            if document.record.preparation not in ("unspecified", item.preparation):
                raise ValueError("Preparation differs from the source")
            items.append(
                {
                    "source_id": item.source_id,
                    "hash": item.hash,
                    "provider": item.provider,
                    "preparation": item.preparation,
                    "name": document.record.name,
                    "quantity": quantity,
                }
            )
        else:
            assert item.version_id is not None
            food = await get_food_version(connection, item.version_id)
            if food.record.preparation == "unspecified":
                raise ValueError("Choose a clear preparation")
            items.append(
                {"food_id": food.version_id, "name": food.record.name, "quantity": quantity}
            )
    reply = await start(
        connection,
        message.from_user.id,
        {
            "continuation": "meal",
            "items": items,
            "date": request.date.isoformat(),
            "label": request.label,
            "source_chat_id": message.chat.id,
            "source_message_id": message.message_id,
        },
    )
    return replace(reply, label_handoff=True)


async def source_meal(
    connection: AsyncConnection,
    request: SourceMeal,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
) -> MealReply:
    if request.date > reference.date():
        raise ValueError("Choose today or a past meal date")
    items = []
    for item in request.items:
        if item.source_id is not None:
            assert item.hash is not None and item.preparation is not None
            food = await accept_cached_source(
                connection, item.source_id, item.hash, item.preparation, provider=item.provider
            )
            version_id = food.version_id
        else:
            assert item.version_id is not None
            food = await get_food_version(connection, item.version_id)
            version_id = item.version_id
        if food.record.preparation == "unspecified":
            raise ValueError("Choose a food with a clear preparation first")
        items.append(
            MealItemInput(
                food_version_id=version_id,
                edible_milligrams=grams_to_milligrams(item.grams),
                original_quantity=format(item.grams, "f"),
                original_unit="g",
            )
        )
    saved = await create_meal(
        connection,
        items=tuple(items),
        label=request.label,
        local_date=request.date,
        timezone=str(reference.tzinfo),
        consumed_at=_date_timestamp(request.date, reference),
        action_key=action_key,
        source_chat_id=source_chat_id or message.chat.id,
        source_message_id=source_message_id or message.message_id,
    )
    return receipt(saved, "Saved meal")


async def source_command(
    connection: AsyncConnection,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply | None:
    async with connection.begin_nested() as savepoint:
        result = await _source_command(
            connection, text, message, action_key=action_key, reference=reference
        )
        if result is not None and result.kind == "food_source_rejected":
            await savepoint.rollback()
        return result


async def _source_command(
    connection: AsyncConnection,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply | None:
    if not text.startswith(("/source-meal ", "/source-add ", "/source-use ", "/source-label ")):
        return None
    try:
        if len(text) > 6000:
            raise ValueError("Use a shorter source request")
        if text.startswith("/source-label "):
            return await source_label_handoff(
                connection,
                SourceLabelHandoff.model_validate_json(text[14:]),
                message,
                reference=reference,
            )
        if text.startswith("/source-meal "):
            return await source_meal(
                connection,
                SourceMeal.model_validate_json(text[13:]),
                message,
                action_key=action_key,
                reference=reference,
            )
        parts = text.split()
        if parts[0] == "/source-use" and len(parts) == 6:
            request = SourceMeal.model_validate(
                {
                    "date": parts[5],
                    "items": [
                        {
                            "source_id": parts[1],
                            "hash": parts[2],
                            "preparation": parts[3],
                            "grams": parts[4].removesuffix("g"),
                        }
                    ],
                }
            )
            return await source_meal(
                connection, request, message, action_key=action_key, reference=reference
            )
        if parts[0] != "/source-add" or len(parts) not in (4, 5):
            raise ValueError("Review a source preview before selecting it")
        item = SourceMealItem.model_validate(
            {
                "source_id": parts[1],
                "hash": parts[2],
                "preparation": parts[3],
                "grams": "1",
                "provider": parts[4] if len(parts) == 5 else "usda",
            }
        )
        assert item.source_id is not None and item.hash is not None and item.preparation is not None
        food = await accept_cached_source(
            connection, item.source_id, item.hash, item.preparation, provider=item.provider
        )
        return MealReply(
            f"Saved food: {food.record.name} ({food.record.preparation}) · #{food.version_id}.\n"
            "Food composition was saved; no meal was logged.",
            "food_added",
        )
    except ValueError:
        return MealReply(
            "That source or meal request is invalid, changed or expired. Review the source again; "
            "nothing was saved.",
            "food_source_rejected",
        )


def _choices(payload: dict[str, Any], revision: int, status: str = "local") -> MealReply:
    from nutrition_bot.application.navigation import menu

    lines = ["Choose the food and preparation:"]
    buttons = []
    for index, food in enumerate(payload.get("locals", [])[:5], 1):
        lines.append(f"{index}. {food['name']} · {food['preparation'].replace('_', ' ')} · Saved")
        buttons.append(
            (
                f"{index} · {food['preparation'].replace('_', ' ')} · {food['name']}"[:64],
                f"flow:{revision}:discover:local={food['version_id']}",
            )
        )
    offset = len(buttons)
    for index, food in enumerate(payload.get("sources", [])[: 9 - offset], offset + 1):
        prep = food["preparation_hint"].replace("_", " ")
        provider_label = "Open Food Facts" if food.get("provider") == "openfoodfacts" else "USDA"
        lines.append(f"{index}. {food['name']} · {prep} · {provider_label} {food['data_type']}")
        buttons.append(
            (
                f"{index} · {prep} · {food['name']}"[:64],
                f"flow:{revision}:discover:source={food['source_id']}",
            )
        )
    if status == "searching":
        lines.append("Searching additional foods… Saved choices are available now.")
    elif status not in {"local", "remote", "offline"}:
        lines.append(
            "Online food search is unavailable. Saved foods still work; retry later or add a label."
        )
    if not buttons:
        lines.append("No matching food yet. Try a more specific name, including raw or cooked.")
    lines.append("This is a food selection; nothing was logged.")
    if payload.get("remote_enabled"):
        buttons.append(("Search all foods", f"flow:{revision}:discover:search"))
    buttons.append(("Enter food label", f"flow:{revision}:discover:label"))
    buttons.append(("Cancel", "cancel"))
    return menu("\n".join(lines), tuple(buttons))


async def enqueue_lookup(
    connection: AsyncConnection,
    *,
    action_key: str,
    owner_id: int,
    chat_id: int,
    revision: int,
    kind: str,
    request: dict[str, Any],
) -> None:
    # A replaced flow's queued requests need no provider call. Running requests
    # remain bounded and their completion guard discards the stale result.
    await connection.execute(
        sa.update(food_lookup_jobs)
        .where(food_lookup_jobs.c.owner_id == owner_id, food_lookup_jobs.c.status == "pending")
        .values(status="cancelled", request=None, completed_at=time.time())
    )
    await connection.execute(
        sa.insert(food_lookup_jobs).values(
            id=f"{action_key}:food:{revision}:{kind}",
            action_key=action_key,
            owner_id=owner_id,
            chat_id=chat_id,
            flow_revision=revision,
            kind=kind,
            request=request,
            status="pending",
            created_at=time.time(),
        )
    )


async def start_barcode(
    connection: AsyncConnection,
    code: str,
    payload: dict[str, Any],
    *,
    action_key: str,
    owner_id: int,
    chat_id: int,
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu
    from nutrition_bot.domain.barcodes import normalize_barcode

    try:
        code = normalize_barcode(code)
    except ValueError:
        return menu(
            "Enter a valid 8, 12, 13 or 14 digit product barcode. "
            "You can also use /label to enter its nutrition label.",
            (("Cancel", "cancel"),),
        )
    payload = {
        **payload,
        "query": code,
        "preparation": "as_sold",
        "locals": [],
        "sources": [],
        "remote_enabled": False,
        "provider": "openfoodfacts",
    }
    revision = await begin(connection, owner_id, "food_discovery_wait", payload)
    await enqueue_lookup(
        connection,
        action_key=action_key,
        owner_id=owner_id,
        chat_id=chat_id,
        revision=revision,
        kind="preview",
        request={"source_id": code, "provider": "openfoodfacts"},
    )
    return menu(
        "Looking up that product barcode…",
        (("Enter food label", f"flow:{revision}:discover:label"), ("Cancel", "cancel")),
    )


async def start_search(
    connection: AsyncConnection,
    query: str,
    payload: dict[str, Any],
    *,
    action_key: str,
    owner_id: int,
    chat_id: int,
    remote_enabled: bool = True,
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu

    query = " ".join(query.split())
    if not 1 <= len(query) <= 120:
        return menu("Use a food name from 1 to 120 characters.", (("Cancel", "cancel"),))
    preparation = infer_preparation(query)
    locals_ = await local_food_search(connection, query, preparation=preparation, limit=5)
    payload = {
        **payload,
        "query": query,
        "preparation": preparation,
        "remote_enabled": remote_enabled,
        "locals": [item.model_dump(mode="json") for item in locals_],
        "sources": [],
    }
    revision = await begin(connection, owner_id, "food_discovery_choices", payload)
    if remote_enabled:
        await enqueue_lookup(
            connection,
            action_key=action_key,
            owner_id=owner_id,
            chat_id=chat_id,
            revision=revision,
            kind="search",
            request={"query": query, "preparation": preparation},
        )
    return _choices(payload, revision, "searching" if remote_enabled else "offline")


async def apply_search_result(
    connection: AsyncConnection,
    owner_id: int,
    payload: dict[str, Any],
    result: SearchResult,
) -> MealReply:
    from nutrition_bot.adapters.database.schema_ui import ui_flows

    locals_ = payload.get("locals", [])
    known = {item["version_id"] for item in locals_}
    payload = {
        **payload,
        "locals": (
            locals_
            + [
                item.model_dump(mode="json")
                for item in result.local
                if item.version_id not in known
            ]
        )[:5],
        "sources": [item.model_dump(mode="json") for item in result.remote[:5]],
    }
    revision = await connection.scalar(
        sa.select(ui_flows.c.revision).where(ui_flows.c.owner_id == owner_id)
    )
    if revision is None:
        raise ValueError("Food flow expired")
    # Enrichment changes no already-displayed local snapshot, amount or date.
    # Keep those buttons usable and do not extend activity on a background result.
    await connection.execute(
        sa.update(ui_flows).where(ui_flows.c.owner_id == owner_id).values(payload=payload)
    )
    return _choices(payload, revision, result.source_status)


async def apply_preview_result(
    connection: AsyncConnection,
    owner_id: int,
    payload: dict[str, Any],
    preview: SourcePreview,
) -> MealReply:
    from nutrition_bot.application.navigation import begin

    if preview.document is None or preview.content_sha256 is None:
        revision = await begin(connection, owner_id, "food_discovery_choices", payload)
        return _choices(payload, revision, preview.source_status)
    document = preview.document
    required = payload.get("preparation")
    if required and document.record.preparation not in (required, "unspecified"):
        revision = await begin(connection, owner_id, "food_discovery_choices", payload)
        return _choices(payload, revision, "Preparation differs; choose another food")
    payload = {
        **payload,
        "source": {
            "source_id": document.source_id,
            "hash": preview.content_sha256,
            "preparation": document.record.preparation,
            "name": document.record.name,
            "provider": document.provider,
        },
    }
    revision = await begin(connection, owner_id, "food_discovery_preview", payload)
    return preview_card(payload, revision, preview)


def preview_card(payload: dict[str, Any], revision: int, preview: SourcePreview) -> MealReply:
    from nutrition_bot.application.navigation import menu

    document = preview.document
    assert document is not None
    source = payload["source"]
    prep = source["preparation"]
    source_label = (
        "Open Food Facts" if document.provider == "openfoodfacts" else "USDA FoodData Central"
    )
    lines = [
        document.record.name,
        f"Preparation: {prep.replace('_', ' ')}",
        f"Source: {source_label} {document.source_id} · {document.data_type}",
        "Nutrients per 100 g:",
    ]
    nutrients = {item.code: item for item in document.record.nutrients}
    for code, label in (
        ("energy", "Energy"),
        ("protein", "Protein"),
        ("carbohydrate", "Carbohydrate"),
        ("fat", "Fat"),
        ("fiber", "Fiber"),
    ):
        value = nutrients.get(code)
        lines.append(
            f"{label}: "
            + (f"{value.amount} {value.unit}" if value and value.amount is not None else "unknown")
        )
    if document.warnings:
        lines.append(
            "Source has missing, unsupported or qualified values; unknown nutrients remain unknown."
        )
    if document.record.source_url:
        lines.append("Source details: " + document.record.source_url)
    if preview.source_status.startswith("cached_"):
        lines.append(
            "Using a previously fetched source preview while online lookup is unavailable."
        )
    if prep != "unspecified" and document.record.preparation == "unspecified":
        lines.append("The source did not specify preparation; confirm it matches your food.")
    if payload.get("quantity"):
        lines.append(f"Amount: {payload['quantity']} · Date: {payload.get('date', 'today')}")
    if payload.get("items"):
        lines.append(
            "Other meal items: "
            + "; ".join(f"{item['quantity']} {item['name']}" for item in payload["items"])
        )
    if prep == "unspecified":
        lines.append(
            "Preparation is unresolved. Confirm the source fits your food "
            "before choosing its preparation."
        )
        buttons = tuple(
            (p.replace("_", " ").capitalize(), f"flow:{revision}:discover:prep={p}")
            for p in ("raw", "cooked", "as_sold", "as_prepared")
        )
    else:
        final = (
            payload.get("quantity")
            and not payload.get("pending")
            and payload.get("continuation") != "recipe"
        )
        buttons = (
            (
                "Confirm food & log"
                if final and not payload["quantity"].startswith("about ")
                else "Use this food",
                f"flow:{revision}:discover:use",
            ),
        )
    return menu(
        "\n".join(lines),
        buttons + (("Choose another food", f"flow:{revision}:discover:back"), ("Cancel", "cancel")),
    )


async def select_food(
    connection: AsyncConnection,
    payload: dict[str, Any],
    selected: dict[str, Any],
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu

    if payload.get("continuation") == "recipe":
        if "source_id" in selected:
            food = await accept_cached_source(
                connection,
                selected["source_id"],
                selected["hash"],
                selected["preparation"],
                provider=selected.get("provider", "usda"),
            )
            version_id = food.version_id
        else:
            version_id = selected["food_id"]
        from nutrition_bot.application.recipe_guide import selected_food

        return await selected_food(
            connection,
            payload,
            version_id,
            selected["name"],
            owner_id=owner_id,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            retention_days=retention_days,
            **context,
        )
    payload = {**payload, "selected": selected}
    if not payload.get("quantity"):
        await begin(connection, owner_id, "food_discovery_amount", payload)
        return menu(
            f"How much {selected['name']} did you eat?\nSend measured grams, e.g. 80 g. "
            "For a rough amount, send about 80 g.",
            (("Cancel", "cancel"),),
        )
    items = payload.get("items", []) + [{**selected, "quantity": payload["quantity"]}]
    pending = payload.get("pending", [])
    if pending:
        next_item, *remaining = pending
        return await start_search(
            connection,
            next_item["query"],
            {
                **payload,
                "items": items,
                "quantity": next_item.get("quantity"),
                "pending": remaining,
            },
            action_key=action_key,
            owner_id=owner_id,
            chat_id=message.chat.id,
            remote_enabled=payload.get("remote_enabled", False),
        )
    revision = await begin(
        connection, owner_id, "food_discovery_review", {**payload, "items": items}
    )
    return meal_card(items, revision, payload.get("date"))


def meal_card(items: list[dict[str, Any]], revision: int, day: str | None) -> MealReply:
    from nutrition_bot.application.navigation import menu

    rough = any(item["quantity"].startswith("about ") for item in items)
    lines = [f"Review meal · {day or 'today'}"] + [
        f"• {item['quantity']} {item['name']}" for item in items
    ]
    if rough:
        lines.append("Rough quantities will open a draft for explicit estimate approval.")
    buttons: tuple[tuple[str, str], ...] = (
        ("Review estimate" if rough else "Confirm & log", f"flow:{revision}:discover:save"),
    )
    if len(items) < 10:
        buttons += (("Add another food", f"flow:{revision}:discover:more"),)
    return menu(
        "\n".join(lines),
        buttons + (("Cancel", "cancel"),),
    )


async def save_guided_meal(
    connection: AsyncConnection,
    payload: dict[str, Any],
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import _command, cancel

    day = date.fromisoformat(payload.get("date") or reference.date().isoformat())
    items = payload["items"]
    # Validate every amount before source publication; the caller rolls back
    # source and ledger changes together if a late validation fails.
    rough = any(item["quantity"].startswith("about ") for item in items)
    command_items = []
    measured = []
    for item in items:
        quantity = item["quantity"]
        parsed = parse_meal(quantity.removeprefix("about ") + " #1", reference.date())
        amount = parsed.items[0].grams
        if "source_id" in item:
            food = await accept_cached_source(
                connection,
                item["source_id"],
                item["hash"],
                item["preparation"],
                provider=item.get("provider", "usda"),
            )
            version_id = food.version_id
        else:
            version_id = item["food_id"]
        measured.append(SourceMealItem(version_id=version_id, grams=amount))
        command_items.append(quantity + " #" + str(version_id))
    await cancel(connection, owner_id)
    if rough:
        label = payload.get("label", "Meal")
        command = (
            "/meal "
            + day.isoformat()
            + " "
            + (label + ": " if label != "Meal" else "")
            + "; ".join(command_items)
        )
        return await _command(
            connection,
            command,
            message,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            owner_id=owner_id,
            retention_days=retention_days,
        )
    return await source_meal(
        connection,
        SourceMeal(label=payload.get("label", "Meal"), date=day, items=tuple(measured)),
        message,
        action_key=action_key,
        reference=reference,
        source_chat_id=payload.get("source_chat_id"),
        source_message_id=payload.get("source_message_id"),
    )


async def discovery_action(
    connection: AsyncConnection,
    row: Any,
    operation: str,
    message: Message,
    **context: Any,
) -> MealReply:
    try:
        async with connection.begin_nested():
            return await _discovery_action(connection, row, operation, message, **context)
    except ValueError:
        return MealReply(
            "This food preview changed or expired. Review the source and amount again; "
            "nothing was saved.",
            "food_source_rejected",
            ui_buttons=(("Home", "home"), ("Cancel", "cancel")),
        )


async def _discovery_action(
    connection: AsyncConnection,
    row: Any,
    operation: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu

    payload = row["payload"]
    operation = operation.removeprefix("discover:")
    common = dict(
        action_key=action_key,
        reference=reference,
        bot_id=bot_id,
        owner_id=owner_id,
        retention_days=retention_days,
        **context,
    )
    if operation == "label":
        from nutrition_bot.application.label_guide import start

        return await start(connection, owner_id, payload)
    if operation in {"search", "back"}:
        return await start_search(
            connection,
            payload["query"],
            payload,
            action_key=action_key,
            owner_id=owner_id,
            chat_id=message.chat.id,
            remote_enabled=payload.get("remote_enabled", False),
        )
    if operation.startswith("local=") and row["stage"] == "food_discovery_choices":
        identifier = int(operation[6:])
        match = next(
            (food for food in payload.get("locals", []) if food["version_id"] == identifier), None
        )
        if match:
            return await select_food(
                connection,
                payload,
                {"food_id": identifier, "name": match["name"]},
                message,
                **common,
            )
    if operation.startswith("source=") and row["stage"] == "food_discovery_choices":
        source_identifier = operation[7:]
        candidate = next(
            (food for food in payload.get("sources", []) if food["source_id"] == source_identifier),
            None,
        )
        if candidate:
            revision = await begin(connection, owner_id, "food_discovery_wait", payload)
            await enqueue_lookup(
                connection,
                action_key=action_key,
                owner_id=owner_id,
                chat_id=message.chat.id,
                revision=revision,
                kind="preview",
                request={
                    "source_id": source_identifier,
                    "provider": candidate.get("provider", "usda"),
                },
            )
            return menu("Loading food details…", (("Cancel", "cancel"),))
    if operation.startswith("prep=") and row["stage"] == "food_discovery_preview":
        prep = operation[5:]
        if (
            prep in {"raw", "cooked", "as_sold", "as_prepared"}
            and payload["source"]["preparation"] == "unspecified"
        ):
            if payload.get("preparation") and payload["preparation"] != prep:
                return menu(
                    "That preparation differs from your food. Choose another source.",
                    (("Cancel", "cancel"),),
                )
            source = {**payload["source"], "preparation": prep}
            preview = await read_cached_source(
                connection,
                source["source_id"],
                time.time(),
                provider=source.get("provider", "usda"),
            )
            if preview is None or preview.content_sha256 != source["hash"]:
                raise ValueError("Review the source again")
            updated = {**payload, "source": source}
            revision = await begin(connection, owner_id, "food_discovery_preview", updated)
            return preview_card(updated, revision, preview)
    if operation == "use" and row["stage"] == "food_discovery_preview":
        source = payload["source"]
        if source["preparation"] == "unspecified":
            return menu("Choose the preparation before using this food.", (("Cancel", "cancel"),))
        # A final measured source card is itself the source and meal review.
        if (
            payload.get("quantity")
            and not payload.get("pending")
            and payload.get("continuation") != "recipe"
            and not payload["quantity"].startswith("about ")
        ):
            return await save_guided_meal(
                connection,
                {
                    **payload,
                    "items": payload.get("items", [])
                    + [{**source, "quantity": payload["quantity"]}],
                },
                message,
                **common,
            )
        return await select_food(connection, payload, source, message, **common)
    if operation == "save" and row["stage"] == "food_discovery_review":
        return await save_guided_meal(connection, payload, message, **common)
    if (
        operation == "more"
        and row["stage"] == "food_discovery_review"
        and len(payload["items"]) < 10
    ):
        await begin(
            connection, owner_id, "food_search", {**payload, "quantity": None, "pending": []}
        )
        return menu("Which food would you like to add?", (("Cancel", "cancel"),))
    return menu("That food step changed or expired. Start again.", (("Home", "home"),))


async def discovery_message(
    connection: AsyncConnection,
    row: Any,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    bot_id: int,
    owner_id: int,
    retention_days: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import menu

    if row["stage"] == "food_discovery_amount":
        match = re.fullmatch(
            r"(?:(about|around|roughly)\s+)?([0-9]+(?:\.[0-9]{1,3})?)\s*(g|kg|mg)?",
            text,
            re.IGNORECASE,
        )
        if not match:
            return menu(
                "Send measured grams, e.g. 80 g, or about 80 g for a rough amount.",
                (("Cancel", "cancel"),),
            )
        unit = (match[3] or "g").casefold()
        quantity = ("about " if match[1] else "") + match[2] + unit
        try:
            parse_meal(quantity.removeprefix("about ") + " #1", reference.date())
        except MealTextError:
            return menu(
                "Use a positive mass up to 50000 grams with milligram precision.",
                (("Cancel", "cancel"),),
            )
        return await select_food(
            connection,
            {**row["payload"], "quantity": quantity},
            row["payload"]["selected"],
            message,
            action_key=action_key,
            reference=reference,
            bot_id=bot_id,
            owner_id=owner_id,
            retention_days=retention_days,
            **context,
        )
    if row["stage"] == "food_discovery_choices":
        query, payload = food_search_input(text, row["payload"], reference)
        return await start_search(
            connection,
            query,
            payload,
            action_key=action_key,
            owner_id=owner_id,
            chat_id=message.chat.id,
            remote_enabled=row["payload"].get("remote_enabled", False),
        )
    return menu("Choose a food from the displayed buttons, or cancel.", (("Cancel", "cancel"),))


async def discover_measured_message(
    connection: AsyncConnection,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    owner_id: int,
    remote_enabled: bool,
) -> MealReply | None:
    """Recover only deterministic food intents, never send arbitrary chat text to USDA."""
    try:
        parsed = parse_draft_meal(text, reference.date())
    except MealTextError:
        return None
    if any(item.grams is None for item in parsed.items):
        return None
    known = []
    pending = []
    for item in parsed.items:
        assert (
            item.grams is not None
            and item.original_quantity is not None
            and item.original_unit is not None
        )
        quantity = (
            ("about " if item.estimate_basis else "") + item.original_quantity + item.original_unit
        )
        try:
            resolved = await _resolve_items(
                connection,
                ParsedMeal(
                    parsed.label,
                    parsed.local_date,
                    (
                        MeasuredFood(
                            item.query, item.grams, item.original_quantity, item.original_unit
                        ),
                    ),
                ),
            )
        except ValueError:
            pending.append({"query": item.query, "quantity": quantity})
        else:
            food = await get_food_version(connection, resolved[0].food_version_id)
            known.append(
                {
                    "food_id": food.version_id,
                    "name": food.record.name,
                    "quantity": quantity,
                }
            )
    if not pending:
        return None
    first, *remaining = pending
    return await start_search(
        connection,
        first["query"],
        {
            "continuation": "meal",
            "items": known,
            "quantity": first["quantity"],
            "pending": remaining,
            "date": parsed.local_date.isoformat(),
            "label": parsed.label,
            "source_chat_id": message.chat.id,
            "source_message_id": message.message_id,
        },
        action_key=action_key,
        owner_id=owner_id,
        chat_id=message.chat.id,
        remote_enabled=remote_enabled,
    )
