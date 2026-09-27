"""A bounded grammar for measured meals, with no guesses or discarded text.

Food queries are opaque catalog names (or ``#<food-version-id>``). Parsing does
not establish a food's identity: callers must resolve the entire query against
reviewed catalog data before saving anything.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext

from nutrition_bot.domain.food import grams_to_milligrams

MEAL_TEXT_HELP = (
    "Use exact measured amounts, for example: /meal Lunch: 150g rice; 200g chicken. "
    "Use g, kg or mg for every food, with at most 10 foods. "
    "You can put today, yesterday or a past YYYY-MM-DD date before the meal label. "
    "Counts, volume measures and rough estimates need clarification; nothing was saved."
)
_MAX_TEXT_LENGTH = 6_000
_MAX_FOOD_LENGTH = 500
_MAX_ITEMS = 10
_MAX_GRAMS = Decimal("50000")
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
_MASS = rf"(?P<quantity>{_NUMBER})\s*(?P<unit>kg|mg|g)"
_PREFIX_MASS = re.compile(rf"{_MASS}\s+(?P<query>.+)", re.IGNORECASE)
_SUFFIX_MASS = re.compile(rf"(?P<query>.+?)\s+{_MASS}", re.IGNORECASE)
_ANY_MASS = re.compile(rf"{_NUMBER}\s*(?:kg|mg|g)\b", re.IGNORECASE)
_SEPARATORS = re.compile(r";|\n|\s+and\s+", re.IGNORECASE)
_UNSUPPORTED_WORDS = re.compile(
    r"\b(?:about|around|approximately|approx|roughly|estimated?|estimation|"
    r"maybe|probably|some|handful|couple|half|quarter|"
    r"today|yesterday|tomorrow|tonight|last|next|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_JOINERS = re.compile(r"\b(?:and|or|plus|then|also)\b", re.IGNORECASE)
_UNSUPPORTED_UNITS = re.compile(
    r"(?:\b|(?<=[0-9]))(?:ml|cl|dl|liters?|litres?|milliliters?|millilitres?|"
    r"cups?|tbsp|tsp|tablespoons?|teaspoons?|slices?|servings?|scoops?|pieces?)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_NUMBER = re.compile(r"[-+−–—]\s*[0-9]|[0-9]\s*[/,:]\s*[0-9]")
_DATE_WORD = re.compile(r"(today|yesterday)(?=\s|$)", re.IGNORECASE)
_DATE_LITERAL = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?=\s|$)")
_LABEL = re.compile(r"(breakfast|lunch|dinner|snack)\s*:\s*", re.IGNORECASE)


class MealTextError(ValueError):
    """An unsupported meal; a fixed message never reflects the raw input."""

    def __init__(self) -> None:
        super().__init__(MEAL_TEXT_HELP)


@dataclass(frozen=True, slots=True)
class MeasuredFood:
    query: str
    grams: Decimal
    original_quantity: str
    original_unit: str


@dataclass(frozen=True, slots=True)
class ParsedMeal:
    label: str
    local_date: date
    items: tuple[MeasuredFood, ...]


def parse_meal(text: str, today: date) -> ParsedMeal:
    """Parse a whole measured meal or reject it without producing partial items.

    Dates use the caller's current local calendar day. Names containing an
    ambiguous separator or reserved syntax can be addressed by their catalog
    food-version ID instead. No catalog matching or date inference occurs here.
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
    body = re.sub(r"^I\s+ate\s+", "", body, flags=re.IGNORECASE)
    local_date, body = _parse_date(body, today)
    label = "Meal"
    label_match = _LABEL.match(body)
    if label_match:
        label = label_match.group(1).capitalize()
        body = body[label_match.end() :]
    if any(character in body for character in "~≈≃≅") or _AMBIGUOUS_NUMBER.search(body):
        raise MealTextError
    parts = _SEPARATORS.split(body)
    if not 1 <= len(parts) <= _MAX_ITEMS:
        raise MealTextError
    items = tuple(_parse_item(part.strip()) for part in parts)
    return ParsedMeal(label=label, local_date=local_date, items=items)


def _parse_date(body: str, today: date) -> tuple[date, str]:
    word = _DATE_WORD.match(body)
    if word:
        try:
            day = today - timedelta(days=1) if word.group(1).casefold() == "yesterday" else today
        except OverflowError:
            raise MealTextError from None
        return day, body[word.end() :].lstrip()
    literal = _DATE_LITERAL.match(body)
    if literal:
        try:
            day = date.fromisoformat(literal.group(1))
        except ValueError:
            raise MealTextError from None
        if day > today:
            raise MealTextError
        return day, body[literal.end() :].lstrip()
    return today, body


def _parse_item(part: str) -> MeasuredFood:
    match = _PREFIX_MASS.fullmatch(part) or _SUFFIX_MASS.fullmatch(part)
    if match is None:
        raise MealTextError
    query = match.group("query").strip()
    _validate_query(query)
    quantity, unit = match.group("quantity"), match.group("unit")
    # Input length is bounded; this precision exceeds every accepted amount's
    # significant digits and does not inherit a caller's Decimal context.
    with localcontext() as context:
        context.prec = _MAX_TEXT_LENGTH + 10
        grams = (
            Decimal(quantity)
            * {"g": Decimal(1), "kg": Decimal(1000), "mg": Decimal("0.001")}[unit.casefold()]
        ).normalize()
    if not 0 < grams <= _MAX_GRAMS:
        raise MealTextError
    try:
        grams_to_milligrams(grams)
    except ValueError:
        raise MealTextError from None
    return MeasuredFood(query=query, grams=grams, original_quantity=quantity, original_unit=unit)


def _validate_query(query: str) -> None:
    if not 1 <= len(query) <= _MAX_FOOD_LENGTH:
        raise MealTextError
    if query.startswith("#"):
        if re.fullmatch(r"#[1-9][0-9]{0,18}", query) is None or int(query[1:]) > 2**63 - 1:
            raise MealTextError
        return
    if (
        not any(character.isalpha() for character in query)
        or _ANY_MASS.search(query)
        or _UNSUPPORTED_WORDS.search(query)
        or _UNSUPPORTED_UNITS.search(query)
        or _UNSUPPORTED_JOINERS.search(query)
        or any(character in query for character in "#;\n:@=+!?")
        or query.endswith(("-", "−", "–", "—", "/"))
    ):
        raise MealTextError
    # Bare numerals can hide counts or malformed quantities. Percentages and
    # alphanumeric product names are retained as part of the exact catalog query.
    for number in re.finditer(r"[0-9]+(?:\.[0-9]+)?", query):
        before = query[number.start() - 1] if number.start() else ""
        after = query[number.end()] if number.end() < len(query) else ""
        if not before.isalpha() and not after.isalpha() and after != "%":
            raise MealTextError
