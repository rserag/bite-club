"""Explicit personal names for immutable reviewed food versions."""

import re
import unicodedata
from dataclasses import dataclass

from nutrition_bot.domain.meal_text import _AMBIGUOUS_NUMBER, MealTextError, _validate_query

_NAME_HELP = (
    "Use an alias of 1–80 readable characters without quantities, dates, commands, "
    "approximation words or meal separators."
)
_RESERVED_MASS = re.compile(
    r"\b(?:g|kg|mg|grams?|kilograms?|milligrams?|oz|ounces?|lbs?|pounds?)\b|[0-9]ish\b",
    re.IGNORECASE,
)
_EATING_PREFIX = re.compile(r"^I\s+ate(?:\s+|$)", re.IGNORECASE)
_COUNT_PREFIX = re.compile(
    r"^(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen)\b",
    re.IGNORECASE,
)


class AliasError(ValueError):
    """A fixed, safe message suitable for the Telegram interface."""


@dataclass(frozen=True, slots=True)
class Alias:
    id: int
    revision: int
    name: str
    food_version_id: int
    active: bool


def alias_display_name(name: str) -> str:
    """Validate the whole name and retain its display spelling and inner spacing."""
    if not isinstance(name, str) or any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in name
    ):
        raise AliasError(_NAME_HELP)
    display = unicodedata.normalize("NFC", name.strip())
    if (
        not 1 <= len(display) <= 80
        or display.startswith(("#", "/"))
        or any(character in display for character in "~≈≃≅")
        or _AMBIGUOUS_NUMBER.search(display)
        or _RESERVED_MASS.search(display)
        or _EATING_PREFIX.match(display)
        or _COUNT_PREFIX.match(display)
        or any(character.isdecimal() and not character.isascii() for character in display)
    ):
        raise AliasError(_NAME_HELP)
    try:
        _validate_query(display)
    except MealTextError:
        raise AliasError(_NAME_HELP) from None
    return display


def normalize_alias(name: str) -> str:
    """Canonical Unicode spelling, case-folding and whitespace for exact lookup."""
    display = alias_display_name(name)
    return unicodedata.normalize("NFC", " ".join(display.casefold().split()))
