"""Bounded meal drafts: explicit masses, explicit approximations, or exact names.

Parsing never supplies a missing quantity or authorizes an estimate. Every query
must still resolve exactly against the reviewed local catalog. Reserved syntax in
food names, including ``and``, can be addressed with a food-version ID instead.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nutrition_bot.domain.meal_text import (
    _AMBIGUOUS_NUMBER,
    _LABEL,
    _MAX_ITEMS,
    _MAX_TEXT_LENGTH,
    _SEPARATORS,
    MealTextError,
    _parse_date,
    _parse_item,
    _validate_query,
)

_APPROXIMATION = re.compile(
    r"^(?:(?:about|around|approximately|approx|roughly|estimated)\s+|~\s*)",
    re.IGNORECASE,
)
_APPROXIMATION_BASIS = "User-described approximate amount"
_MALFORMED_MASS = re.compile(r"\b(?:g|kg|mg)\b|[0-9]ish\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DraftFood:
    query: str
    grams: Decimal | None
    original_quantity: str | None
    original_unit: str | None
    estimate_basis: str | None


@dataclass(frozen=True, slots=True)
class ParsedDraftMeal:
    label: str
    local_date: date
    items: tuple[DraftFood, ...]


def parse_draft_meal(text: str, today: date) -> ParsedDraftMeal:
    """Parse the entire input; reject unsupported syntax without partial results.

    Only an item-prefix approximation marker may be removed, and it must precede
    an otherwise valid measured item. Bare names keep all their text for exact
    matching; counts, volumes, ranges and unsupported qualifiers are never stripped.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_LENGTH:
        raise MealTextError
    if any(
        unicodedata.category(character).startswith("C")
        for character in text
        if character not in "\n\t\r"
    ):
        raise MealTextError
    body = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"^/meal(?:@[A-Za-z0-9_]+)?(?:\s+|$)", "", body, flags=re.IGNORECASE)
    if body.startswith("/"):
        raise MealTextError
    body = re.sub(r"^I\s+ate(?:\s+|$)", "", body, flags=re.IGNORECASE)
    local_date, body = _parse_date(body, today)
    label = "Meal"
    label_match = _LABEL.match(body)
    if label_match:
        label = label_match.group(1).capitalize()
        body = body[label_match.end() :]
    if _AMBIGUOUS_NUMBER.search(body):
        raise MealTextError
    parts = _SEPARATORS.split(body)
    if not 1 <= len(parts) <= _MAX_ITEMS:
        raise MealTextError
    return ParsedDraftMeal(label, local_date, tuple(_draft_item(part.strip()) for part in parts))


def _draft_item(part: str) -> DraftFood:
    approximation = _APPROXIMATION.match(part)
    basis = None
    if approximation:
        part = part[approximation.end() :]
        basis = _APPROXIMATION_BASIS
    if any(character in part for character in "~≈≃≅"):
        raise MealTextError
    try:
        item = _parse_item(part)
    except MealTextError:
        if approximation or _MALFORMED_MASS.search(part):
            raise
        _validate_query(part)
        return DraftFood(part, None, None, None, None)
    return DraftFood(item.query, item.grams, item.original_quantity, item.original_unit, basis)
