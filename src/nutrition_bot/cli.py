import argparse
import asyncio
import json
import logging
import os
import signal
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from nutrition_bot.config import BotSettings, NutritionSourceSettings, StorageSettings
from nutrition_bot.logging import configure_logging
from nutrition_bot.runtime.health import check_health
from nutrition_bot.runtime.lock import database_lock


def migrate(settings: StorageSettings) -> None:
    from alembic import command
    from alembic.config import Config

    # Migrations ship with the source checkout/container, not an assumed current directory.
    scripts = Path(__file__).resolve().parent / "migrations"
    if not scripts.is_dir():
        scripts = Path(__file__).resolve().parents[2] / "migrations"
    config = Config()
    config.set_main_option("script_location", str(scripts))
    config.attributes["database_url"] = settings.resolved_database_url
    with database_lock(settings.database_path):
        command.upgrade(config, "head")


async def run(settings: BotSettings) -> None:
    from aiogram import Bot

    from nutrition_bot.adapters.database.store import Store
    from nutrition_bot.application.service import Service
    from nutrition_bot.runtime.worker import run_worker
    from nutrition_bot.telegram.gateway import TelegramGateway

    if not settings.database_path.is_file():
        raise RuntimeError("Run migrate before starting the bot")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    store = Store(settings)
    try:
        from nutrition_bot.application.ai_service import AiService

        ai_service = AiService(store)
        if settings.llm_provider == "openrouter":
            if not settings.openrouter_api_key or not settings.ai_endpoint_manifest:
                raise ValueError("OpenRouter needs a private key and reviewed endpoint manifest")
            from nutrition_bot.adapters.ai.openrouter import OpenRouterAdapter
            from nutrition_bot.domain.ai_policy import load_manifest

            ai_service = AiService(
                store,
                load_manifest(settings.ai_endpoint_manifest),
                OpenRouterAdapter(settings.openrouter_api_key),
            )
        elif settings.llm_provider == "chatgpt":
            if not settings.chatgpt_credentials_path or not settings.chatgpt_policy_path:
                raise ValueError("ChatGPT needs app-owned credentials and a reviewed plan policy")
            from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter, load_plan_policy

            ai_service = AiService(
                store,
                plan_adapter=ChatGPTPlanAdapter(
                    settings.chatgpt_credentials_path,
                    load_plan_policy(settings.chatgpt_policy_path),
                ),
            )
        try:
            await ai_service.recover_abandoned()
            async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
                gateway = TelegramGateway(bot)
                if settings.miniapp_enabled and not settings.miniapp_url:
                    raise ValueError("Configure the HTTPS Mini App URL")
                service = Service(store, settings, ai_service)
                runner = None
                try:
                    if settings.miniapp_enabled:
                        from nutrition_bot.miniapp.server import start_miniapp

                        runner = await start_miniapp(
                            service, host=settings.miniapp_host, port=settings.miniapp_port
                        )
                    await run_worker(service, gateway, stop)
                finally:
                    if runner is not None:
                        await runner.cleanup()
        finally:
            await ai_service.close()
    finally:
        await store.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


async def retry_replies(settings: BotSettings) -> None:
    from nutrition_bot.adapters.database.store import Store
    from nutrition_bot.application.service import Service

    store = Store(settings)
    try:
        await store.check_schema()
        service = Service(store, settings)
        await service.cleanup()
        count = await service.retry_failed_replies()
        print(json.dumps({"requeued_replies": count}))
    finally:
        await store.close()


async def import_food(
    settings: StorageSettings, input_file: Path, *, reviewed: bool, food_id: int | None
) -> None:
    from decimal import Decimal

    from nutrition_bot.adapters.database.foods import normalize_food, publish_reviewed_food
    from nutrition_bot.adapters.database.store import Store
    from nutrition_bot.domain.food import ReviewedFoodInput

    if not settings.database_path.is_file():
        raise ValueError("Run migrate first")
    # Bound input size; parsing decimal JSON literals never passes through a float.
    with input_file.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("Food input exceeds size limit")
    record = ReviewedFoodInput.model_validate(json.loads(raw, parse_float=Decimal))
    store = Store(settings)
    try:
        await store.check_schema()
        async with store.write() as connection:
            if reviewed:
                saved = await publish_reviewed_food(connection, record, food_id=food_id)
                output = {"status": "catalog_saved", **saved.model_dump(mode="json")}
            else:
                values = await normalize_food(connection, record)
                output = {
                    "status": "preview_only",
                    "record": record.model_dump(mode="json"),
                    "per_100g": [item.model_dump(mode="json") for item in values],
                    "amount_scale": 1_000_000,
                    "next_step": "Review values, source basis and units; rerun with --reviewed.",
                }
        print(json.dumps(output))
    finally:
        await store.close()


async def source_command(settings: NutritionSourceSettings, args: argparse.Namespace) -> None:
    from contextlib import AsyncExitStack

    from nutrition_bot.adapters.database.foods import get_food_version
    from nutrition_bot.adapters.database.store import Store
    from nutrition_bot.application.food_catalog import FoodCatalog, accept_cached_source
    from nutrition_bot.domain.food_source import FoodProvider

    if not settings.database_path.is_file():
        raise ValueError("Run migrate first")
    store = Store(settings)
    try:
        await store.check_schema()
        async with AsyncExitStack() as stack:
            provider: FoodProvider | None = None
            if (
                settings.usda_api_key
                and args.command in {"food-search", "food-fetch"}
                and not args.offline
            ):
                import httpx

                from nutrition_bot.adapters.nutrition.usda import USDAProvider

                client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False))
                provider = USDAProvider(
                    client, settings.usda_api_key, settings.usda_timeout_seconds
                )
            catalog = FoodCatalog(store, provider)
            if args.command == "food-search":
                result = await catalog.search(args.query, remote=args.remote, offline=args.offline)
                print(result.model_dump_json())
            elif args.command == "food-fetch":
                preview = await catalog.lookup(
                    args.fdc_id, refresh=args.refresh, offline=args.offline
                )
                print(preview.model_dump_json())
            elif args.command == "food-select":
                async with store.write() as connection:
                    saved = await accept_cached_source(
                        connection, args.fdc_id, args.expected_hash, args.preparation
                    )
                print(json.dumps({"status": "catalog_saved", **saved.model_dump(mode="json")}))
            else:
                async with store.engine.connect() as connection:
                    saved = await get_food_version(connection, args.version_id)
                print(saved.model_dump_json())
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Telegram nutrition bot")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "migrate", "healthcheck", "retry-replies"):
        commands.add_parser(name)
    backup = commands.add_parser("backup", help="Create a private, consistent local snapshot")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--image-digest", required=True)
    backup.add_argument("--media-root", type=Path)
    restore = commands.add_parser("restore-check", help="Verify a snapshot without network sends")
    restore.add_argument("--input", type=Path, required=True)
    manual = commands.add_parser("food-import", help="Preview or save a reviewed food JSON file")
    manual.add_argument("--input-file", type=Path, required=True)
    manual.add_argument("--reviewed", action="store_true")
    manual.add_argument("--food-id", type=int)
    search = commands.add_parser("food-search", help="Find local foods or USDA candidates")
    search.add_argument("--query", required=True)
    search_mode = search.add_mutually_exclusive_group()
    search_mode.add_argument("--remote", action="store_true")
    search_mode.add_argument("--offline", action="store_true")
    fetch = commands.add_parser("food-fetch", help="Preview a USDA record before selection")
    fetch.add_argument("--fdc-id", required=True)
    fetch_mode = fetch.add_mutually_exclusive_group()
    fetch_mode.add_argument("--refresh", action="store_true")
    fetch_mode.add_argument("--offline", action="store_true")
    select = commands.add_parser("food-select", help="Save the exact source preview you reviewed")
    select.add_argument("--fdc-id", required=True)
    select.add_argument("--expected-hash", required=True)
    select.add_argument(
        "--preparation", required=True, choices=["raw", "cooked", "as_sold", "as_prepared"]
    )
    show = commands.add_parser("food-show", help="Read an immutable local food version offline")
    show.add_argument("--version-id", type=int, required=True)
    args = parser.parse_args()
    if args.command == "food-import" and args.food_id is not None and args.food_id <= 0:
        parser.error("food-id must be positive")
    os.umask(0o077)
    configure_logging("INFO")
    try:
        if args.command == "backup":
            from nutrition_bot.runtime.backup import snapshot

            result = snapshot(
                StorageSettings().database_path,
                args.output,
                args.image_digest,
                media_root=args.media_root,
            )
            print(
                json.dumps({"status": "local_snapshot_verified", "schema": result.schema_revision})
            )
        elif args.command == "restore-check":
            from nutrition_bot.runtime.backup import verify_snapshot

            result = verify_snapshot(args.input)
            print(json.dumps({"status": "snapshot_verified", "schema": result.schema_revision}))
        elif args.command == "run":
            settings = BotSettings()
            configure_logging(settings.log_level)
            with database_lock(settings.database_path):
                asyncio.run(run(settings))
        elif args.command == "retry-replies":
            settings = BotSettings()
            with database_lock(settings.database_path):
                asyncio.run(retry_replies(settings))
        elif args.command == "migrate":
            migrate(StorageSettings())
            print("Database migrations applied.")
        elif args.command == "food-import":
            assert args.input_file is not None
            storage = StorageSettings()
            with database_lock(storage.database_path):
                asyncio.run(
                    import_food(
                        storage, args.input_file, reviewed=args.reviewed, food_id=args.food_id
                    )
                )
        elif args.command in {"food-search", "food-fetch", "food-select", "food-show"}:
            source_settings = NutritionSourceSettings()
            with database_lock(source_settings.database_path):
                asyncio.run(source_command(source_settings, args))
        else:
            health = check_health(StorageSettings())
            print(json.dumps(asdict(health)))
            raise SystemExit(0 if health.healthy else 1)
    except ValidationError as exc:
        if args.command.startswith("food-"):
            # Extra JSON keys are user content, so even validation locations are private.
            print(json.dumps({"error": "invalid_food_input_or_configuration"}))
            raise SystemExit(2) from None
        fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        print(json.dumps({"error": "invalid_configuration", "fields": fields}))
        raise SystemExit(2) from None
    except Exception as exc:
        logging.getLogger("nutrition_bot.cli").error(
            "command_failed", extra={"error_type": type(exc).__name__}
        )
        raise SystemExit(1) from None
