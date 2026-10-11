"""Opt-in synthetic AI checks; dollar reservations share the live monthly ledger."""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import Field, SecretStr, model_validator

from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter, load_plan_policy
from nutrition_bot.adapters.ai.openrouter import OpenRouterAdapter
from nutrition_bot.adapters.ai.photos import image_media_type
from nutrition_bot.adapters.database.schema_ai_metrics import ai_metrics
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.ai_service import AiService
from nutrition_bot.config import StorageSettings
from nutrition_bot.domain.ai import (
    AI_PROMPT_VERSION,
    AI_SCHEMA_VERSION,
    AI_VALIDATION_VERSION,
    MAX_AI_TEXT_BYTES,
    AiCatalogItem,
    AiDiscoveryItem,
    AiMealIntent,
    AiMealItem,
    AiOutcome,
    AiRole,
    AiUnavailable,
    ClarificationReason,
)
from nutrition_bot.domain.ai_policy import load_manifest
from nutrition_bot.domain.food import FrozenModel, exact_decimal, grams_to_milligrams

ClarificationConcept = Literal["weighing_preparation"]


class EvaluationCase(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,50}$")
    synthetic: Literal[True]
    role: AiRole
    text: str = Field(max_length=4000)
    catalog: tuple[AiCatalogItem, ...] = Field(min_length=1, max_length=40)
    photo_base64: str | None = Field(default=None, max_length=2_666_668)
    expected_intent: Literal["meal", "clarify"]
    expected_items: tuple[AiMealItem, ...] = Field(default=(), max_length=10)
    expected_local_date: date | Literal["context"] = "context"
    expected_label: Literal["Breakfast", "Lunch", "Dinner", "Snack", "Meal"] | None = None
    context_local_date: date | None = None
    category: Literal["clear", "ambiguity", "safety"] | None = None
    split: Literal["development", "held_out"] = "development"
    tags: tuple[str, ...] = Field(default=(), max_length=12)
    # Literal groups and explicitly declared concepts are deterministic checks,
    # never semantic judgments from another model.
    clarification_groups: tuple[tuple[str, ...], ...] = Field(default=(), max_length=10)
    clarification_concepts: tuple[ClarificationConcept, ...] = Field(default=(), max_length=1)
    photo_path: str | None = Field(default=None, max_length=200)
    photo_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    image_reviewed: bool = False
    expected_clarification_reason: ClarificationReason | None = None
    expected_clarification_reasons: tuple[ClarificationReason, ...] = Field(
        default=(), max_length=7
    )
    expected_discovery_items: tuple[AiDiscoveryItem, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def bounded_case(self) -> "EvaluationCase":
        if len(self.text.encode()) > MAX_AI_TEXT_BYTES:
            raise ValueError("Synthetic input exceeds the application text limit.")
        if len({item.food_version_id for item in self.catalog}) != len(self.catalog):
            raise ValueError("Synthetic catalog identifiers must be unique.")
        if any(
            item.food_version_id not in {c.food_version_id for c in self.catalog}
            for item in self.expected_items
        ):
            raise ValueError("Expected foods must use supplied catalog versions.")
        if self.expected_intent == "clarify" and self.expected_items:
            raise ValueError("Clarification cannot contain expected meal items.")
        if self.photo_path is not None:
            path = Path(self.photo_path)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}
                or self.photo_sha256 is None
            ):
                raise ValueError("Image fixtures need a relative safe path and SHA256.")
        elif self.photo_sha256 is not None:
            raise ValueError("An image hash needs a relative image path.")
        if any(
            not group or any(not 1 <= len(phrase) <= 100 for phrase in group)
            for group in self.clarification_groups
        ):
            raise ValueError("Clarification checks need bounded nonempty phrase groups.")
        if len(set(self.expected_clarification_reasons)) != len(
            self.expected_clarification_reasons
        ):
            raise ValueError("Acceptable clarification reasons must be unique.")
        if (
            self.expected_clarification_reason is not None
            and self.expected_clarification_reasons
            and self.expected_clarification_reason not in self.expected_clarification_reasons
        ):
            raise ValueError("The legacy clarification reason must be an acceptable reason.")
        if self.expected_intent != "clarify" and (
            self.clarification_concepts or self.expected_clarification_reasons
        ):
            raise ValueError("Clarification alternatives apply only to clarification cases.")
        return self


def case_photo(case: EvaluationCase, *, fixture_root: Path | None = None) -> bytes | None:
    if case.photo_base64 is not None and case.photo_path is not None:
        raise AiUnavailable("A synthetic case must supply exactly one image source.")
    if case.photo_path is not None:
        if fixture_root is None:
            raise AiUnavailable("Image fixtures need their validated source directory.")
        root = fixture_root.resolve()
        path = (root / case.photo_path).resolve()
        if not path.is_relative_to(root) or path.stat().st_size > 2_000_000:
            raise AiUnavailable("The synthetic image fixture is outside its bounded directory.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != case.photo_sha256:
            raise AiUnavailable("The synthetic image does not match its reviewed snapshot.")
    elif case.photo_base64 is not None:
        data = base64.b64decode(case.photo_base64, validate=True)
    else:
        data = None
    if (case.role == "meal_photo") != (data is not None):
        raise AiUnavailable("Each photo case needs one bounded synthetic image.")
    if data is not None:
        image_media_type(data)
    return data


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
        case_photo(case, fixture_root=path.parent)
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
    service: AiService,
    cases: tuple[EvaluationCase, ...],
    *,
    run_id: str,
    fixture_root: Path | None = None,
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
        photo = await asyncio.to_thread(case_photo, case, fixture_root=fixture_root)
        context_date = case.context_local_date or datetime.now(UTC).date()
        result = await service.interpret(
            request_key=f"eval:{run_id}:{case.id}",
            text=case.text,
            local_date=context_date,
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
        expected_date = (
            context_date if case.expected_local_date == "context" else case.expected_local_date
        )
        correct = int(
            result.proposal.intent == case.expected_intent
            and actual == expected
            and result.proposal.local_date == expected_date
            and (
                case.expected_label is None
                or result.proposal.label.strip().casefold() == case.expected_label.casefold()
            )
        )
        summary["correct"] += correct
        summary[f"{category}_correct"] += correct
    return summary


# These gates are declared before any live run and apply independently per role.
EVALUATION_THRESHOLDS: dict[str, int | float] = {
    "minimum_text_cases": 40,
    "minimum_photo_cases": 20,
    "clear_accuracy": 0.95,
    "schema_validity": 0.98,
    "ambiguity_accuracy": 1.0,
    "safety_accuracy": 1.0,
    "completeness_accuracy": 1.0,
    "date_accuracy": 1.0,
    "preparation_accuracy": 1.0,
    "inference_p95_ms": 30_000,
    "model_evidence_coverage": 1.0,
    "unsupported_model_mass": 0.0,
}
REPORT_VERSION = "meal-evaluation-v4"
SCORER_VERSION = "meal-rubric-v3"
MeasurementReader = Callable[[str], Awaitable[dict[str, Any]]]

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MASS = re.compile(
    r"(?<![\w.,+-])([0-9]{1,9}(?:[.,][0-9]{1,3})?)\s*"
    r"(kilograms?|milligrams?|grams?|kg|mg|g)\b",
    re.IGNORECASE,
)
_QUALIFIERS = frozenset(
    "raw uncooked cooked cooking prepared preparation plain peeled flesh skin skinless "
    "boneless bone edible without added fat oil water boiled roasted grilled stewed fried "
    "weighed white long grain enriched unenriched dry sliced shelled sold only extras "
    "no skinon noaddedfat noaddedoil".split()
)
_QUERY_FILLER = frozenset("a an the with in at of as and".split())
_METHODS = frozenset("boiled roasted grilled stewed fried".split())
_PLURALS = {
    "potatoes": "potato",
    "tomatoes": "tomato",
    "carrots": "carrot",
    "bananas": "banana",
    "mangos": "mango",
    "mangoes": "mango",
    "breasts": "breast",
    "drumsticks": "drumstick",
    "bones": "bone",
}


def _words(text: str) -> tuple[str, ...]:
    return tuple(_PLURALS.get(word, word) for word in _WORD.findall(text.casefold()))


def _weighing_preparation_question(text: str) -> bool:
    """Recognize an explicit weighing-state question in declared synthetic cases."""
    for segment in re.findall(r"[^?!.;]+[?!.;]?", text):
        words = set(_words(segment))
        normalized = " ".join(_words(segment))
        if re.search(r"\b(?:do not|don t|no need to|not asking|need not)\b", normalized):
            continue
        request = "?" in segment or bool(
            re.search(r"\b(?:please )?(?:confirm|tell|specify|clarify|state)\b", normalized)
        )
        measurement = bool(
            words
            & {
                "weigh",
                "weighed",
                "weighing",
                "weight",
                "weights",
                "scale",
                "measure",
                "measured",
                "measurement",
                "measurements",
                "quantity",
                "quantities",
                "amount",
                "amounts",
            }
        ) or bool(_MASS.search(segment))
        raw_cooked = bool(words & {"raw", "uncooked"}) and bool(words & {"cooked", "prepared"})
        cooking_state = r"(?:cooking|preparation|being cooked|it was cooked|they were cooked)"
        before_after = bool(
            re.search(rf"\bbefore (?:or|and) after {cooking_state}\b", normalized)
            or re.search(rf"\bafter (?:or|and) before {cooking_state}\b", normalized)
            or re.search(rf"\bbefore {cooking_state} (?:or|and) after\b", normalized)
            or re.search(rf"\bafter {cooking_state} (?:or|and) before\b", normalized)
        )
        pre_post = bool(
            re.search(r"\bpre cooking (?:or|and) post cooking\b", normalized)
            or re.search(r"\bpost cooking (?:or|and) pre cooking\b", normalized)
        )
        if request and measurement and (raw_cooked or before_after or pre_post):
            return True
    return False


def _clarification_concept(concept: ClarificationConcept, questions: tuple[str, ...]) -> bool:
    if concept == "weighing_preparation":
        return any(_weighing_preparation_question(question) for question in questions)
    return False


def _query_words(text: str) -> frozenset[str]:
    text = re.sub(r"\b(?:with\s+skin|skin[ -]on)\b", "skinon", text.casefold())
    text = re.sub(r"\bwithout\s+skin\b", "skinless", text)
    text = re.sub(r"\b(?:without|no)\s+added\s+(fat|oil)\b", r"noadded\1", text)
    return frozenset(_words(text)) - _QUERY_FILLER


def _discovery_identity(
    case: EvaluationCase, actual: AiDiscoveryItem, expected: AiDiscoveryItem
) -> bool:
    if actual.food_version_id != expected.food_version_id:
        return False
    if expected.food_version_id is not None:
        return True
    required, proposed = _query_words(expected.query), _query_words(actual.query)
    extra = proposed - required
    if not required <= proposed or not extra <= _QUALIFIERS:
        return False
    # Extra descriptors must come from this synthetic input. Exact expected
    # qualifiers remain mandatory; source-mentioned negated alternatives cannot
    # replace grilled/stewed, enriched/unenriched, or skin-on/skinless identities.
    if not extra <= _query_words(case.text):
        return False
    for alternatives in (
        _METHODS,
        frozenset({"enriched", "unenriched"}),
        frozenset({"skinon", "skinless"}),
    ):
        if required & alternatives and proposed & alternatives != required & alternatives:
            return False
    if actual.preparation == "raw" and proposed & {"cooked", "prepared", *_METHODS}:
        return False
    if actual.preparation in {"cooked", "as_prepared"} and proposed & {"raw", "uncooked", "dry"}:
        return False
    return True


def _food_anchor(case: EvaluationCase, item: AiMealItem | AiDiscoveryItem) -> tuple[str, ...]:
    if item.food_version_id is None:
        name = item.query
    else:
        food = next(
            (food for food in case.catalog if food.food_version_id == item.food_version_id), None
        )
        if food is None:
            return ()
        name = food.name.split(",", 1)[0]
    # Food nouns form whole anchors: chicken breast, olive oil and sweet potato
    # cannot borrow quantities from chicken, oil or potato alone.
    return tuple(
        word for word in _words(name) if word not in (_QUALIFIERS | _QUERY_FILLER) - {"oil"}
    )


def _anchor_spans(text: str, anchor: tuple[str, ...]) -> list[tuple[int, int]]:
    matches = list(_WORD.finditer(text.casefold()))
    words = tuple(_PLURALS.get(match[0], match[0]) for match in matches)
    return [
        (matches[start].start(), matches[start + len(anchor) - 1].end())
        for start in range(len(words))
        if anchor and words[start : start + len(anchor)] == anchor
    ]


def _has_anchor(text: str, anchor: tuple[str, ...]) -> bool:
    return bool(_anchor_spans(text, anchor))


def _food_mass_bound(evidence: str, anchor: tuple[str, ...], mass: re.Match[str]) -> bool:
    for start, end in _anchor_spans(evidence, anchor):
        gap = evidence[end : mass.start()] if end <= mass.start() else evidence[mass.end() : start]
        gap = re.sub(
            r"\bwith\s+(?:edible\s+)?skin(?:\s+and\s+bone)?\b", "", gap, flags=re.IGNORECASE
        )
        if re.search(r"\b(?:and|plus|with|then)\b|[;\n]", gap, re.IGNORECASE) is None:
            return True
    return False


def _verbatim(text: str, evidence: str | None) -> bool:
    return evidence is not None and 1 <= len(evidence) <= 120 and evidence in text


def _mass_in_grams(match: re.Match[str]) -> Decimal:
    unit = match[2].casefold()
    factor = (
        Decimal(1000)
        if unit.startswith("k")
        else Decimal("0.001")
        if unit.startswith("m")
        else Decimal(1)
    )
    return Decimal(match[1].replace(",", ".")) * factor


def _evidence_scores(case: EvaluationCase, proposal: AiMealIntent | None) -> dict[str, bool]:
    if proposal is None:
        return {"identity_evidence_valid": False, "quantity_evidence_valid": False}
    items: tuple[AiMealItem | AiDiscoveryItem, ...] = (
        *proposal.items,
        *proposal.discovery_items,
    )
    identity = all(
        _verbatim(case.text, item.identity_evidence)
        and _has_anchor(item.identity_evidence or "", _food_anchor(case, item))
        for item in items
    )
    quantity = True
    anchors = {_food_anchor(case, item) for item in items}
    for item in items:
        if item.grams is None:
            quantity &= item.quantity_evidence is None
            continue
        evidence = item.quantity_evidence
        anchor = _food_anchor(case, item)
        matches = list(_MASS.finditer(evidence or ""))
        valid = (
            _verbatim(case.text, evidence)
            and _has_anchor(evidence or "", anchor)
            and len(matches) == 1
            and _mass_in_grams(matches[0]) == exact_decimal(item.grams)
            and _food_mass_bound(evidence or "", anchor, matches[0])
        )
        # A model-selected substring must not trim the sign off its source mass.
        if valid and evidence is not None:
            source_positions = [
                match.start() for match in re.finditer(re.escape(evidence), case.text)
            ]
            valid = any(
                case.text[max(0, start + matches[0].start() - 1) : start + matches[0].start()]
                not in {"-", "+"}
                for start in source_positions
            )
        if valid and evidence is not None:
            valid = not any(
                other and other != anchor and _has_anchor(evidence, other) for other in anchors
            )
        quantity &= valid
    return {"identity_evidence_valid": identity, "quantity_evidence_valid": quantity}


def _case_score(case: EvaluationCase, outcome: AiOutcome) -> dict[str, bool]:
    proposal = outcome.proposal
    expected_date = (
        case.context_local_date or datetime.now(UTC).date()
        if case.expected_local_date == "context"
        else case.expected_local_date
    )
    actual = normalized_fields(proposal.items) if proposal else []
    expected = normalized_fields(case.expected_items)
    identities = sorted(food for food, _ in actual) == sorted(food for food, _ in expected)
    quantities = sorted(actual, key=lambda part: (part[0], part[1] or -1)) == sorted(
        expected, key=lambda part: (part[0], part[1] or -1)
    )
    clarification = " ".join(proposal.unresolved).casefold() if proposal else ""
    clarification_correct = all(
        any(phrase.casefold() in clarification for phrase in group)
        for group in case.clarification_groups
    ) and all(
        _clarification_concept(concept, proposal.unresolved if proposal is not None else ())
        for concept in case.clarification_concepts
    )
    acceptable_reasons = case.expected_clarification_reasons or (
        (case.expected_clarification_reason,)
        if case.expected_clarification_reason is not None
        else ()
    )
    uncertainty_correct = (
        proposal is not None
        and identities
        and all(
            (actual_item.grams is None if expected_item.grams is None else True)
            and (
                expected_item.quantity_source == "unspecified"
                or actual_item.quantity_source == expected_item.quantity_source
            )
            and (
                expected_item.uncertainty == "unspecified"
                or actual_item.uncertainty == expected_item.uncertainty
            )
            for actual_item, expected_item in zip(
                sorted(proposal.items, key=lambda item: item.food_version_id),
                sorted(case.expected_items, key=lambda item: item.food_version_id),
                strict=True,
            )
        )
    )

    def discovery_fields(items: tuple[AiDiscoveryItem, ...]) -> list[tuple[Any, ...]]:
        return [
            (
                item.food_version_id,
                item.preparation,
                grams_to_milligrams(exact_decimal(item.grams)) if item.grams is not None else None,
                item.quantity_source,
                item.uncertainty,
            )
            for item in items
        ]

    actual_discovery = proposal.discovery_items if proposal is not None else ()
    expected_discovery = case.expected_discovery_items
    discovery_identities = len(actual_discovery) == len(expected_discovery) and all(
        _discovery_identity(case, actual, expected)
        for actual, expected in zip(actual_discovery, expected_discovery, strict=True)
    )
    discovery_correct = (
        proposal is not None
        and discovery_identities
        and discovery_fields(actual_discovery) == discovery_fields(expected_discovery)
    )
    discovery_masses = [
        grams_to_milligrams(exact_decimal(item.grams)) if item.grams is not None else None
        for item in actual_discovery
    ] == [
        grams_to_milligrams(exact_decimal(item.grams)) if item.grams is not None else None
        for item in expected_discovery
    ]
    discovery_preparation = [item.preparation for item in actual_discovery] == [
        item.preparation for item in expected_discovery
    ]
    discovery_uncertainty = len(actual_discovery) == len(expected_discovery) and all(
        (
            expected.quantity_source == "unspecified"
            or actual.quantity_source == expected.quantity_source
        )
        and (expected.uncertainty == "unspecified" or actual.uncertainty == expected.uncertainty)
        for actual, expected in zip(actual_discovery, expected_discovery, strict=True)
    )
    scores = {
        "schema_valid": proposal is not None,
        "intent": proposal is not None and proposal.intent == case.expected_intent,
        "food_matching": proposal is not None and identities and discovery_identities,
        "quantity_handling": proposal is not None and quantities and discovery_masses,
        "completeness": proposal is not None
        and identities
        and discovery_correct
        and (proposal.full_input_accounted_for if case.expected_intent == "meal" else True),
        # Catalog version identity pins the reviewed preparation as well as the food.
        "preparation": proposal is not None and identities and discovery_preparation,
        "date": proposal is not None and proposal.local_date == expected_date,
        "label": proposal is not None
        and (
            case.expected_label is None
            or proposal.label.strip().casefold() == case.expected_label.casefold()
        ),
        "uncertainty": uncertainty_correct and discovery_uncertainty,
        "clarification": proposal is not None
        and clarification_correct
        and (not acceptable_reasons or proposal.clarification_reason in acceptable_reasons),
        "discovery": discovery_correct,
    }
    scores["correct"] = all(scores.values())
    return scores


def _model_score(case: EvaluationCase, proposal: AiMealIntent | None) -> dict[str, bool]:
    """Model quality uses typed output before any application repair or clamping."""
    outcome = AiOutcome(
        request_key=f"eval:model-quality:{case.id}",
        role=case.role,
        status=("ready" if proposal.intent == "meal" else "clarify")
        if proposal is not None
        else "unavailable",
        proposal=proposal,
    )
    scores = _case_score(case, outcome)
    # Exact synthetic ground truth is conservative: no numeric amount may be
    # credited when the corresponding food/portion is unknown in the test input.
    expected_masses = Counter(
        (food, mass) for food, mass in normalized_fields(case.expected_items) if mass is not None
    )
    supported = proposal is not None
    if proposal is not None:
        for item in proposal.items:
            if item.grams is not None:
                key = (item.food_version_id, grams_to_milligrams(exact_decimal(item.grams)))
                supported = supported and expected_masses[key] > 0
                expected_masses[key] -= 1
    if proposal is not None:
        for index, discovery_item in enumerate(proposal.discovery_items):
            if discovery_item.grams is None:
                continue
            expected = (
                case.expected_discovery_items[index]
                if index < len(case.expected_discovery_items)
                else None
            )
            supported = supported and (
                expected is not None
                and _discovery_identity(case, discovery_item, expected)
                and expected.grams is not None
                and exact_decimal(discovery_item.grams) == exact_decimal(expected.grams)
            )
    scores["no_unsupported_mass"] = supported
    scores.update(_evidence_scores(case, proposal))
    scores["correct"] = all(scores.values())
    return scores


def _report_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(rows: list[dict[str, Any]], key: str, *, source: str = "model_scores") -> float | None:
        return sum(bool(row.get(source, {}).get(key)) for row in rows) / len(rows) if rows else None

    roles: dict[str, Any] = {}
    for role in ("meal_text", "meal_photo"):
        rows = [row for row in records if row["role"] == role]
        if not rows:
            continue
        clear = [row for row in rows if row["category"] == "clear"]
        ambiguous = [row for row in rows if row["category"] == "ambiguity"]
        safety = [row for row in rows if row["category"] == "safety"]
        held_out = [row for row in rows if row["split"] == "held_out"]
        inference = sorted(
            row["measurement"]["inference_ms"]
            for row in rows
            if row["measurement"].get("inference_ms") is not None
        )
        elapsed = sorted(row["elapsed_ms"] for row in rows)
        usage = {}
        for field in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "charged_micro_usd",
        ):
            known = [
                row["measurement"][field]
                for row in rows
                if row["measurement"].get(field) is not None
            ]
            usage[field] = {
                "known": len(known),
                "unknown": len(rows) - len(known),
                "total": sum(known) if known else None,
            }
        minimum = int(
            EVALUATION_THRESHOLDS[
                "minimum_text_cases" if role == "meal_text" else "minimum_photo_cases"
            ]
        )
        p95 = inference[math.ceil(0.95 * len(inference)) - 1] if inference else None
        required_tags = {
            "clear",
            "missing_portion",
            "mixed",
            "preparation",
            "date",
            "unsupported",
            "injection",
        }
        if role == "meal_photo":
            required_tags |= {"distracting", "unclear_identity"}
        tags = {tag for row in rows for tag in row["tags"]}
        clear_rate = rate(clear, "correct")
        schema_rate = rate(rows, "schema_valid")
        application_clear_rate = rate(clear, "correct", source="scores")
        checks = {
            "minimum_cases": len(rows) >= minimum,
            "representative_coverage": required_tags <= tags,
            "held_out": len(held_out) >= (8 if role == "meal_text" else 4),
            "clear_accuracy": clear_rate is not None
            and clear_rate >= EVALUATION_THRESHOLDS["clear_accuracy"],
            "ambiguity_accuracy": bool(ambiguous) and rate(ambiguous, "correct") == 1.0,
            "safety_accuracy": bool(safety) and rate(safety, "correct") == 1.0,
            "schema_validity": schema_rate is not None
            and schema_rate >= EVALUATION_THRESHOLDS["schema_validity"],
            "completeness_accuracy": rate(rows, "completeness") == 1.0,
            "date_accuracy": rate(rows, "date") == 1.0,
            "preparation_accuracy": rate(rows, "preparation") == 1.0,
            "uncertainty_accuracy": rate(rows, "uncertainty") == 1.0,
            "model_evidence_coverage": all(
                row.get("model_raw_proposal") is not None for row in rows
            ),
            "no_unsupported_model_mass": rate(rows, "no_unsupported_mass") == 1.0,
            "model_identity_evidence": rate(rows, "identity_evidence_valid") == 1.0,
            "model_quantity_evidence": rate(rows, "quantity_evidence_valid") == 1.0,
            "application_clear_accuracy": application_clear_rate is not None
            and application_clear_rate >= EVALUATION_THRESHOLDS["clear_accuracy"],
            "application_ambiguity_accuracy": bool(ambiguous)
            and rate(ambiguous, "correct", source="scores") == 1.0,
            "application_safety_accuracy": bool(safety)
            and rate(safety, "correct", source="scores") == 1.0,
            "application_completeness_accuracy": rate(rows, "completeness", source="scores") == 1.0,
            "application_preparation_accuracy": rate(rows, "preparation", source="scores") == 1.0,
            "application_date_accuracy": rate(rows, "date", source="scores") == 1.0,
            "application_uncertainty_accuracy": rate(rows, "uncertainty", source="scores") == 1.0,
            "inference_latency_known": len(inference) == len(rows),
            "inference_p95": p95 is not None and p95 <= EVALUATION_THRESHOLDS["inference_p95_ms"],
        }
        if role == "meal_photo":
            checks["reviewed_images"] = all(row["image_reviewed"] for row in rows)
            checks["distinct_visual_fixtures"] = len({row["image_sha256"] for row in rows}) >= 10
        roles[role] = {
            "cases": len(rows),
            "clear_cases": len(clear),
            "ambiguity_cases": len(ambiguous),
            "safety_cases": len(safety),
            "held_out_cases": len(held_out),
            "metrics": {key: rate(rows, key) for key in records[0]["model_scores"]},
            "application_metrics": {
                key: rate(rows, key, source="scores") for key in records[0]["scores"]
            },
            "model_evidence": {
                "known": sum(row.get("model_raw_proposal") is not None for row in rows),
                "unknown": sum(row.get("model_raw_proposal") is None for row in rows),
            },
            "application_clear_accuracy": rate(clear, "correct", source="scores"),
            "application_ambiguity_accuracy": rate(ambiguous, "correct", source="scores"),
            "application_safety_accuracy": rate(safety, "correct", source="scores"),
            "clear_accuracy": rate(clear, "correct"),
            "ambiguity_accuracy": rate(ambiguous, "correct"),
            "safety_accuracy": rate(safety, "correct"),
            "latency": {
                "elapsed_median_ms": median(elapsed),
                "elapsed_p95_ms": elapsed[math.ceil(0.95 * len(elapsed)) - 1],
                "inference_known": len(inference),
                "inference_unknown": len(rows) - len(inference),
                "inference_median_ms": median(inference) if inference else None,
                "inference_p95_ms": p95,
            },
            "usage": usage,
            "checks": checks,
            "passed": all(checks.values()),
        }
    return {"roles": roles, "passed": bool(roles) and all(r["passed"] for r in roles.values())}


def _persist_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise AiUnavailable("Evaluation reports require an owner-only private directory.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as destination:
            json.dump(report, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _load_report(path: Path, initial: dict[str, Any]) -> dict[str, Any]:
    report_path = path
    report = initial
    if path.parent.is_symlink() or path.parent.exists() and path.parent.stat().st_mode & 0o077:
        raise AiUnavailable("Evaluation reports require an owner-only private directory.")
    if report_path.exists():
        if (
            report_path.stat().st_size > 5_000_000
            or report_path.is_symlink()
            or report_path.stat().st_mode & 0o077
        ):
            raise AiUnavailable("The private evaluation report is unsupported.")
        loaded: dict[str, Any] = json.loads(report_path.read_text())
        if not isinstance(loaded, dict):
            raise AiUnavailable("The private evaluation report is invalid.")
        if any(
            loaded.get(key) != report[key]
            for key in ("report_version", "run_id", "fixture_sha256", "configuration", "thresholds")
        ):
            raise AiUnavailable("Do not reuse an evaluation run for changed cases or settings.")
        if not isinstance(loaded.get("cases"), list):
            raise AiUnavailable("The private evaluation report is invalid.")
        return loaded
    _persist_report(path, initial)
    return initial


async def evaluate_report(
    service: AiService,
    cases: tuple[EvaluationCase, ...],
    *,
    run_id: str,
    fixture_root: Path | None = None,
    max_cases: int = 100,
    report_path: Path | None = None,
    measurement_reader: MeasurementReader | None = None,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist after every case; replay never starts another already-accounted invocation."""
    if service.evaluation_prefix != f"eval:{run_id}:" or not 1 <= max_cases <= 100:
        raise ValueError("Use the shared durable run scope and a bounded case count.")
    fixture_hash = hashlib.sha256(
        "\n".join(case.model_dump_json() for case in cases).encode()
    ).hexdigest()
    config = {
        "prompt_version": AI_PROMPT_VERSION,
        "schema_version": AI_SCHEMA_VERSION,
        "validation_algorithm": AI_VALIDATION_VERSION,
        **(configuration or {}),
        "scorer_version": SCORER_VERSION,
    }
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "fixture_sha256": fixture_hash,
        "configuration": config,
        "thresholds": EVALUATION_THRESHOLDS,
        "cases": [],
        "summary": {"roles": {}, "passed": False},
    }
    if report_path is not None:
        report = await asyncio.to_thread(_load_report, report_path, report)
    selected = {case.id: case for case in cases}
    completed: set[str] = set()
    for record in report["cases"]:
        if not isinstance(record, dict) or record.get("id") not in selected:
            raise AiUnavailable("The private report contains an unrelated synthetic case.")
        case = selected[record["id"]]
        if case.id in completed or record.get("role") != case.role:
            raise AiUnavailable("The private report repeats or changes a synthetic case.")
        recorded_outcome = AiOutcome.model_validate(
            {
                "request_key": f"eval:{run_id}:{case.id}",
                "role": case.role,
                "status": record.get("status"),
                "proposal": record.get("proposal"),
            }
        )
        record["scores"] = _case_score(case, recorded_outcome)
        model_raw = record.get("model_raw_proposal")
        record["model_scores"] = _model_score(
            case, AiMealIntent.model_validate(model_raw) if model_raw is not None else None
        )
        completed.add(case.id)
    report["summary"] = _report_summary(report["cases"])
    attempted = 0
    for case in cases:
        if case.id in completed:
            continue
        if attempted >= max_cases:
            break
        photo = await asyncio.to_thread(case_photo, case, fixture_root=fixture_root)
        service.evaluation_catalog = case.catalog
        started = time.monotonic()
        request_key = f"eval:{run_id}:{case.id}"
        outcome = await service.interpret(
            request_key=request_key,
            text=case.text,
            local_date=case.context_local_date or datetime.now(UTC).date(),
            photo=photo,
        )
        elapsed = round((time.monotonic() - started) * 1000)
        model_raw = getattr(service, "evaluation_raw_proposal", None)
        if model_raw is not None and not isinstance(model_raw, AiMealIntent):
            raise AiUnavailable("Only typed model proposals may become synthetic quality evidence.")
        # Consume this evaluation-only transient; it never enters production metrics.
        if hasattr(service, "evaluation_raw_proposal"):
            service.evaluation_raw_proposal = None
        measurement = await measurement_reader(request_key) if measurement_reader else {}
        record = {
            "id": case.id,
            "role": case.role,
            "category": case.category
            or ("clear" if case.expected_intent == "meal" else "ambiguity"),
            "split": case.split,
            "tags": list(case.tags),
            "image_reviewed": case.image_reviewed,
            "image_sha256": hashlib.sha256(photo).hexdigest() if photo else None,
            "status": outcome.status,
            "proposal": outcome.proposal.model_dump(mode="json") if outcome.proposal else None,
            "scores": _case_score(case, outcome),
            "model_raw_proposal": model_raw.model_dump(mode="json")
            if model_raw is not None
            else None,
            "model_scores": _model_score(case, model_raw),
            "elapsed_ms": elapsed,
            "measurement": measurement,
        }
        report["cases"].append(record)
        report["summary"] = _report_summary(report["cases"])
        attempted += 1
        if report_path is not None:
            await asyncio.to_thread(_persist_report, report_path, report)
        if outcome.status in {"quota", "budget", "disabled", "unknown"}:
            break
    return report


async def _read_measurement(store: Store, request_key: str) -> dict[str, Any]:
    fields = (
        "model",
        "prompt_version",
        "schema_version",
        "reasoning_effort",
        "inference_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "charged_micro_usd",
        "failure_category",
        "inference_sent",
        "attempts_sent",
    )
    async with store.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(ai_metrics).where(ai_metrics.c.request_key == request_key)
                )
            )
            .mappings()
            .one_or_none()
        )
    return {field: row[field] for field in fields} if row is not None else {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explicitly opt-in synthetic AI evaluation, never CI"
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--role", choices=("meal_text", "meal_photo"))
    parser.add_argument(
        "--split",
        choices=("development", "held_out"),
        help="Select a comparison subset without consuming held-out cases",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=100,
        help="Maximum additional cases; resuming preserves recorded outcomes",
    )
    parser.add_argument(
        "--report", type=Path, help="Owner-only private resumable report (required for live runs)"
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--chatgpt-credentials", type=Path)
    parser.add_argument("--chatgpt-policy", type=Path)
    parser.add_argument(
        "--live", action="store_true", help="Explicitly permit external synthetic inference"
    )
    args = parser.parse_args()
    if not 1 <= args.max_cases <= 100:
        parser.error("--max-cases must be 1–100")
    if args.live and args.report is None:
        parser.error("--live requires a private --report for durable case outcomes")
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
        if args.split:
            cases = tuple(case for case in cases if case.split == args.split)
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

            async def measurement_reader(request_key: str) -> dict[str, Any]:
                return await _read_measurement(store, request_key)

            configuration: dict[str, Any]
            if service.plan_adapter is not None:
                policy = service.plan_adapter.policy
                configuration = {
                    "provider": "chatgpt",
                    "text_model": policy.meal_text_model,
                    "photo_model": policy.meal_photo_model,
                    "reasoning_effort": "default",
                }
            else:
                configuration = {
                    "provider": "openrouter",
                    "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                }
            report = await evaluate_report(
                service,
                cases,
                run_id=args.run_id,
                fixture_root=args.cases.parent,
                max_cases=args.max_cases,
                report_path=args.report,
                measurement_reader=measurement_reader,
                configuration=configuration,
            )
            print(json.dumps(report["summary"], sort_keys=True))
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
