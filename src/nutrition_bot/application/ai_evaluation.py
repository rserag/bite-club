"""Opt-in synthetic AI checks; dollar reservations share the live monthly ledger."""

import argparse
import asyncio
import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr

from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter, load_plan_policy
from nutrition_bot.adapters.ai.openrouter import OpenRouterAdapter
from nutrition_bot.adapters.ai.photos import image_media_type
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.ai_service import AiService
from nutrition_bot.config import StorageSettings
from nutrition_bot.domain.ai import AiCatalogItem, AiMealItem, AiRole, AiUnavailable
from nutrition_bot.domain.ai_policy import load_manifest
from nutrition_bot.domain.food import FrozenModel, exact_decimal, grams_to_milligrams


class EvaluationCase(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    synthetic: Literal[True]
    role: AiRole
    text: str = Field(max_length=4000)
    catalog: tuple[AiCatalogItem, ...] = Field(min_length=1, max_length=40)
    photo_base64: str | None = Field(default=None, max_length=2_666_668)
    expected_intent: Literal["meal", "clarify"]
    expected_items: tuple[AiMealItem, ...] = Field(default=(), max_length=10)


def load_cases(path: Path) -> tuple[EvaluationCase, ...]:
    if path.stat().st_size > 10_000_000:
        raise AiUnavailable("The synthetic evaluation file is too large.")
    cases = tuple(
        EvaluationCase.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    )
    if not 1 <= len(cases) <= 100 or len({case.id for case in cases}) != len(cases):
        raise AiUnavailable("Provide 1–100 uniquely named synthetic evaluation cases.")
    for case in cases:
        if (case.role == "meal_photo") != (case.photo_base64 is not None):
            raise AiUnavailable("Each photo case needs one bounded synthetic image.")
        if case.photo_base64 is not None:
            image_media_type(base64.b64decode(case.photo_base64, validate=True))
    return cases


def normalized_fields(items: tuple[AiMealItem, ...]) -> list[tuple[int, int | None]]:
    return [
        (
            item.food_version_id,
            grams_to_milligrams(exact_decimal(item.grams)) if item.grams is not None else None,
        )
        for item in items
    ]


async def evaluate(
    service: AiService, cases: tuple[EvaluationCase, ...], *, run_id: str
) -> dict[str, int]:
    if service.evaluation_prefix != f"eval:{run_id}:":
        raise ValueError("The evaluation run must share its durable reservation scope.")
    summary = {
        "cases": 0,
        "completed": 0,
        "correct": 0,
        "clarifications": 0,
        "unavailable": 0,
        "clear_cases": 0,
        "clear_correct": 0,
        "ambiguity_cases": 0,
        "ambiguity_correct": 0,
    }
    for case in cases:
        service.evaluation_catalog = case.catalog
        photo = base64.b64decode(case.photo_base64, validate=True) if case.photo_base64 else None
        result = await service.interpret(
            request_key=f"eval:{run_id}:{case.id}",
            text=case.text,
            local_date=datetime.now(UTC).date(),
            photo=photo,
        )
        summary["cases"] += 1
        category = "clear" if case.expected_intent == "meal" else "ambiguity"
        summary[f"{category}_cases"] += 1
        if result.proposal is None:
            summary["unavailable"] += 1
            continue
        summary["completed"] += 1
        summary["clarifications"] += int(result.proposal.intent == "clarify")
        actual = normalized_fields(result.proposal.items)
        expected = normalized_fields(case.expected_items)
        correct = int(result.proposal.intent == case.expected_intent and actual == expected)
        summary["correct"] += correct
        summary[f"{category}_correct"] += correct
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explicitly opt-in synthetic AI evaluation, never CI"
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--role", choices=("meal_text", "meal_photo"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--chatgpt-credentials", type=Path)
    parser.add_argument("--chatgpt-policy", type=Path)
    parser.add_argument(
        "--live", action="store_true", help="Explicitly permit external synthetic inference"
    )
    args = parser.parse_args()
    if (
        not args.run_id.isascii()
        or not args.run_id.replace("-", "").replace("_", "").isalnum()
        or not 3 <= len(args.run_id) <= 50
    ):
        parser.error("--run-id must be 3–50 ASCII letters, numbers, hyphens or underscores")
    try:
        cases = load_cases(args.cases)
        if args.role:
            cases = tuple(case for case in cases if case.role == args.role)
        if not cases:
            raise ValueError
    except (OSError, ValueError):
        raise SystemExit("Synthetic evaluation cases could not be validated.") from None
    if not args.live:
        print(json.dumps({"validated_cases": len(cases), "external_requests": 0}))
        return

    async def run() -> None:
        settings = StorageSettings()
        store = Store(settings)
        prefix = f"eval:{args.run_id}:"
        service: AiService | None = None
        try:
            await store.check_schema()
            if args.chatgpt_credentials and args.chatgpt_policy:
                plan = ChatGPTPlanAdapter(
                    args.chatgpt_credentials, load_plan_policy(args.chatgpt_policy)
                )
                service = AiService(
                    store,
                    plan_adapter=plan,
                    evaluation_prefix=prefix,
                    evaluation_catalog=cases[0].catalog,
                )
            elif args.manifest and os.environ.get("OPENROUTER_API_KEY"):
                adapter = OpenRouterAdapter(SecretStr(os.environ["OPENROUTER_API_KEY"]))
                service = AiService(
                    store,
                    load_manifest(args.manifest),
                    adapter,
                    evaluation_prefix=prefix,
                    evaluation_catalog=cases[0].catalog,
                )
            else:
                raise AiUnavailable(
                    "Configure one reviewed provider and its app-owned credentials."
                )
            print(json.dumps(await evaluate(service, cases, run_id=args.run_id), sort_keys=True))
        finally:
            if service is not None:
                await service.close()
            await store.close()

    try:
        asyncio.run(run())
    except (AiUnavailable, OSError, ValueError):
        raise SystemExit(
            "AI evaluation could not finish safely; reserved usage was retained."
        ) from None


if __name__ == "__main__":
    main()
