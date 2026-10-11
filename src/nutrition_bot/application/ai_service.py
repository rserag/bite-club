"""Optional meal interpretation outside write transactions and exact reviewed drafts."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

import sqlalchemy as sa
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter
from nutrition_bot.adapters.ai.openrouter import (
    AiTransportError,
    OpenRouterAdapter,
)
from nutrition_bot.adapters.database.drafts import create_draft
from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.schema_ai import (
    ai_attempts,
    ai_disabled_routes,
    ai_plan_invocations,
    ai_requests,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.application.ai_budget import (
    consume_ai_outcome,
    purge_ai_outcomes,
    reserve_attempt,
    save_outcome,
    settle_attempt,
)
from nutrition_bot.application.draft_conversation import draft_receipt
from nutrition_bot.application.food_retrieval import ai_food_context
from nutrition_bot.application.meal_conversation import MealReply, _date_timestamp
from nutrition_bot.domain.ai import (
    MAX_AI_TEXT_BYTES,
    AiCallMetrics,
    AiCatalogItem,
    AiDiscoveryItem,
    AiMealIntent,
    AiMealItem,
    AiOutcome,
    AiQuantity,
    AiRole,
    AiUnavailable,
)
from nutrition_bot.domain.ai_policy import AiEndpointManifest
from nutrition_bot.domain.drafts import (
    CookingMethod,
    DraftContent,
    PlannedItem,
    PreparationAttribute,
    PreparationDetails,
    ReportedCookingMethod,
)
from nutrition_bot.domain.food import exact_decimal, grams_to_milligrams

logger = logging.getLogger("nutrition_bot.ai_metrics")
_INVALID_DATE_NOTE = "Please provide one valid meal date, today or in the past."
_CONFLICTING_PORTION_NOTE = (
    "Please provide one edible weight in grams for each portion with conflicting amounts."
)


_CAPTION_MASS = re.compile(
    r"(?<![\w.,])([0-9]+(?:[.,][0-9]{1,3})?)\s*"
    r"(kilograms?|milligrams?|grams?|kg|mg|g)\b",
    re.IGNORECASE,
)
_ALTERNATIVE_MASS = re.compile(
    r"(?<![\w.,+\-−–—])(?P<first>[0-9]+(?:[.,][0-9]{1,3})?)\s*"
    r"(?P<first_unit>kilograms?|milligrams?|grams?|kg|mg|g)?\s+or\s+"
    r"(?:(?:about|around|approximate(?:ly)?|approx|roughly)\s+)?"
    r"(?P<second>[0-9]+(?:[.,][0-9]{1,3})?)\s*"
    r"(?P<second_unit>kilograms?|milligrams?|grams?|kg|mg|g)?(?!\w|[.,][0-9])",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")
_APPROXIMATE = re.compile(
    r"\b(?:about|around|approximate(?:ly)?|approx|rough(?:ly)?|estimate[ds]?|maybe|perhaps)\b|"
    r"\bI think\b|[~≈]",
    re.IGNORECASE,
)
_FOOD_NAME_NOISE = frozenset(
    "raw cooked synthetic alias as sold prepared fried boiled grilled baked steamed roasted "
    "stewed sauteed sautéed skin skinless boneless bone meat flesh only peeled unpeeled "
    "white brown long grain enriched unenriched fortified unfortified salted unsalted "
    "plain on in".split()
)
_MASS_CONNECTIVES = frozenset(
    "exactly weighed weighs weighing weight gram grams g kg kilogram kilograms "
    "mg milligram milligrams edible mass amount at was is of about around "
    "approximately approximate approx roughly rough estimate estimated estimates "
    "maybe perhaps after before cooking stewing roasting boiling steaming baking grilling "
    "frying raw cooked stewed grilled boiled steamed baked roasted sauteed sautéed "
    "measured measurement measurements portion portions total its their it the this that "
    "own my were only or to between either range".split()
)
_CONFLICT_REFERENCE_WORDS = (
    _MASS_CONNECTIVES
    | _FOOD_NAME_NOISE
    | frozenset(
        "i we our had ate eating have meal food serving servings for first second third "
        "either unsure uncertain cannot can remember which whether if one not don dont t "
        "know sure lunch dinner breakfast today yesterday on included including".split()
    )
)
_SOURCE_DELIMITER = re.compile(
    r"[,;\n]|(?<![0-9])\.(?![0-9])|\b(?:and|plus|then|with)\b", re.IGNORECASE
)
_MASS_EVIDENCE_SEPARATOR = re.compile(r"(?<![0-9]),|,(?![0-9])|[;\n]")
_SOURCE_CONTINUATION = (
    _MASS_CONNECTIVES
    | _FOOD_NAME_NOISE
    | frozenset(
        "the this that its it a was were portion portions total my own all quantities weights "
        "or to with after before "
        "cooking only no without skin bones bone salt oil peeled unpeeled shell flesh meat "
        "raw uncooked edible measured measurement measurements dry sauce butter milk sugar "
        "water added fat including ingredients use preparation i did didn t have haven "
        "not never unknown unmeasured "
        "weigh weighed measure".split()
    )
)
_UNCLEAR_MASS = re.compile(
    r"[-+−–—]\s*[0-9]|[0-9]\s*[/,:]\s*[0-9]|[0-9]\s+(?:to|or)\s+[0-9]|"
    r"\b(?:between|either|range)\b|\b(?:did not|didn't|not yet|have not|haven't|never|not)"
    r"\s+(?:weigh|measure)|\bnot (?:weighed|measured)\b|"
    r"\b(?:not|unknown|unmeasured)\s+edible\s+(?:weight|mass|portion|amount|flesh|meat)\b",
    re.IGNORECASE,
)
_NON_EDIBLE_MASS = re.compile(
    r"\b(?:bone-in|with bones|unpeeled|shell-on|in (?:its |the )?(?:shell|peel)|"
    r"including (?:the )?(?:bones|shell|peel|bowl|plate|container)|gross weight)\b",
    re.IGNORECASE,
)
_EDIBLE_MASS = re.compile(r"\bedible\s+(?:weight|mass|portion|amount|flesh|meat)\b", re.IGNORECASE)
_BENIGN_PREPARATION_TAIL = re.compile(
    r"with\s+no\s+(?:added\s+)?(?:oil|sauce|salt|fat|butter|milk|sugar)"
    r"(?:\s+or\s+(?:oil|sauce|salt|fat|butter|milk|sugar))*[\s.,!]*",
    re.IGNORECASE,
)
_NEGATIVE_PREPARATION = re.compile(
    r"\b(?:without|no)\s+(?:added\s+)?(?:oil|sauce|salt|fat|butter|milk|sugar|ingredients)"
    r"(?:\s+or\s+(?:oil|sauce|salt|fat|butter|milk|sugar))*\b",
    re.IGNORECASE,
)
_SKIN_PREPARATION = re.compile(r"\b(?:with\s+skin|meat\s+and\s+skin)\b", re.IGNORECASE)


def _food_words(food_name: str, aliases: tuple[str, ...] = ()) -> tuple[frozenset[str], ...]:
    """Keep each reviewed name/alias separate from preparation descriptor clauses."""
    groups = []
    for name in (food_name, *aliases):
        primary = re.split(r",|\b(?:with|without|no|not)\b", name.casefold(), maxsplit=1)[0]
        words = frozenset(
            word
            for word in re.findall(r"[^\W_]+", primary)
            if word not in _FOOD_NAME_NOISE and not word.isdecimal() and len(word) > 1
        )
        if words:
            groups.append(words)
    return tuple(groups)


def _names_food(source: str, food_words: tuple[frozenset[str], ...]) -> bool:
    tokens = set(re.findall(r"[^\W_]+", source.casefold()))
    return any(
        all(word in tokens or word + "s" in tokens or word + "es" in tokens for word in group)
        for group in food_words
    )


def _source_parts(text: str) -> list[tuple[int, int, str]]:
    """Keep signed masses, decimal commas and numeric ranges in their original clauses."""
    parts = []
    start = 0
    for separator in _SOURCE_DELIMITER.finditer(text):
        before, after = text[: separator.start()], text[separator.end() :]
        if separator[0] == "," and before[-1:].isdigit() and after[:1].isdigit():
            continue
        if (
            separator[0].casefold() == "and"
            and re.search(r"[0-9]\s*$", before)
            and re.match(r"\s*[0-9]", after)
        ):
            continue
        if separator[0].casefold() == "with" and re.match(
            r"\s*(?:skin|bones|salt|oil|no)\b", after, re.IGNORECASE
        ):
            continue
        parts.append((start, separator.start(), text[start : separator.start()]))
        start = separator.end()
    parts.append((start, len(text), text[start:]))
    return parts


def _source_context(
    text: str, evidence: str | None, food_words: tuple[frozenset[str], ...]
) -> str | None:
    """Use the original enclosing clause, never a model-trimmed qualifier span.

    Punctuation-only mass continuations stay attached to their food. A clause
    naming another food cannot lend its state, mass or uncertainty to this one.
    Repeated identical excerpts are ambiguous and cannot authorize a mass.
    """
    parts = _source_parts(text)
    if evidence is None:
        relevant = [
            _source_context(text, part, food_words)
            for _, _, part in parts
            if _names_food(part, food_words)
        ]
        # Legacy missing spans use the same original adjacent qualifiers. A
        # repeated/ambiguous source clause cannot authorize a reviewed state.
        return (
            " ".join(part for part in relevant if part is not None)
            if relevant and all(part is not None for part in relevant)
            else None
        )
    start = text.find(evidence)
    if start < 0 or text.find(evidence, start + 1) >= 0:
        return None
    end = start + len(evidence)
    included = [
        index for index, (left, right, _) in enumerate(parts) if left < end and right > start
    ]
    if not included:
        return None
    first, last = included[0], included[-1]

    def continuation(part: str) -> bool:
        words = set(re.findall(r"[^\W_]+", _CAPTION_MASS.sub("", part).casefold()))
        return (
            bool(words)
            and all(word.isdecimal() or word in _SOURCE_CONTINUATION for word in words)
            and (not _CAPTION_MASS.search(part) or _measurement_reference(part))
        )

    def separator(left: int, right: int) -> bool:
        value = text[left:right]
        return value.strip() in {",", ".", ";"} or "\n" in value and not value.strip()

    while (
        first > 0
        and separator(parts[first - 1][1], parts[first][0])
        and continuation(parts[first - 1][2])
    ):
        first -= 1
    while (
        last + 1 < len(parts)
        and separator(parts[last][1], parts[last + 1][0])
        and continuation(parts[last + 1][2])
    ):
        last += 1
    return text[parts[first][0] : parts[last][1]]


def _single_food_quantity_evidence(evidence: str, food_words: tuple[frozenset[str], ...]) -> bool:
    """Known skin preparation is a descriptor, never a second food or portion."""
    association = _SKIN_PREPARATION.sub("", evidence)
    continuation = re.search(r"\b(?:and\s+then|then|and)\b", association, re.I)
    if continuation is not None:
        prefix = association[: continuation.start()]
        tail = association[continuation.end() :]
        words = set(re.findall(r"[^\W_]+", tail.casefold()))
        if (
            _names_food(prefix, food_words)
            and len(tuple(_CAPTION_MASS.finditer(prefix))) == 1
            and words
            and words.issubset(_PREPARATION_CONTINUATION)
            and not _CAPTION_MASS.search(tail)
            and any(re.search(pattern, tail, re.I) for pattern in _METHOD_PATTERNS.values())
        ):
            association = prefix
    if re.search(r"\b(?:and|plus|then)\b", association, re.IGNORECASE):
        return False
    connector = re.search(r"\bwith\b", association, re.IGNORECASE)
    if connector is None:
        return True
    prefix, suffix = association[: connector.start()], association[connector.start() :]
    return (
        _BENIGN_PREPARATION_TAIL.fullmatch(suffix) is not None
        and _names_food(prefix, food_words)
        and len(tuple(_CAPTION_MASS.finditer(prefix))) == 1
    )


def _measurement_reference(clause: str) -> bool:
    words = set(
        re.findall(
            r"[^\W_]+",
            _NEGATIVE_PREPARATION.sub("", _CAPTION_MASS.sub("", clause)).casefold(),
        )
    )
    return all(word.isdecimal() or word in _MASS_CONNECTIVES for word in words)


def _conflicting_portion(text: str, food_words: tuple[tuple[frozenset[str], ...], ...]) -> bool:
    """Distinct alternative masses must refer to one original food/portion.

    This deliberately excludes ranges, sums, separate servings and distinct
    before/after cooking measurements. A food-free measurement sentence may
    refer back only through food-free measurement continuations; unrelated
    prose or another food stops that reference. Model excerpts do not decide
    whether the original input contains a conflict.
    """
    parts = _source_parts(text)

    def named_groups(clause: str) -> set[frozenset[str]]:
        groups = {group for names in food_words for group in names if _names_food(clause, (group,))}
        # Generic and specific catalog names can identify the same food.
        return {group for group in groups if not any(group < other for other in groups)}

    def reference_words(clause: str) -> set[str]:
        return set(
            re.findall(
                r"[^\W_]+",
                _CAPTION_MASS.sub(
                    " ", _ALTERNATIVE_MASS.sub(" ", _ISO_DATE.sub(" ", clause))
                ).casefold(),
            )
        )

    def one_food_reference(clause: str, names: set[frozenset[str]]) -> bool:
        if len(names) != 1:
            return False
        group = next(iter(names))
        # Match the same inflections as the catalog identity check.
        food_tokens = {variant for word in group for variant in (word, word + "s", word + "es")}
        return reference_words(clause).issubset(
            _CONFLICT_REFERENCE_WORDS | _SOURCE_CONTINUATION | food_tokens
        )

    def grams(number: str, unit: str) -> Decimal:
        factor = (
            Decimal(1000)
            if unit.casefold().startswith("k")
            else Decimal("0.001")
            if unit.casefold().startswith("m")
            else Decimal(1)
        )
        return Decimal(number.replace(",", ".")) * factor

    for index, (_, _, clause) in enumerate(parts):
        for match in _ALTERNATIVE_MASS.finditer(clause):
            first_unit = match["first_unit"] or match["second_unit"]
            second_unit = match["second_unit"] or match["first_unit"]
            if first_unit is None or second_unit is None:
                continue
            first = grams(match["first"], first_unit)
            second = grams(match["second"], second_unit)
            if first <= 0 or second <= 0 or first == second:
                continue
            if re.search(
                r"\b(?:before cooking|weighed raw|dry weight)\b", clause, re.I
            ) and re.search(r"\b(?:after cooking|weighed cooked)\b", clause, re.I):
                continue
            names = named_groups(clause)
            words = reference_words(clause)
            if names:
                # A second named or unknown food cannot lend a competing mass.
                if one_food_reference(clause, names):
                    return True
                continue
            if not words.issubset(_CONFLICT_REFERENCE_WORDS):
                continue
            previous = index - 1
            while previous >= 0:
                antecedent = parts[previous][2]
                names = named_groups(antecedent)
                if names:
                    if one_food_reference(antecedent, names):
                        return True
                    break
                if antecedent.strip() and not _measurement_reference(antecedent):
                    break
                previous -= 1
    return False


def _supported_quantity(
    item: AiQuantity,
    *,
    text: str,
    food_name: str,
    role: AiRole,
    food_aliases: tuple[str, ...] = (),
) -> AiQuantity:
    """A visual impression cannot supply edible mass, even if the model claims it can."""
    if item.grams is None:
        return item.model_copy(
            update={
                "quantity_source": "unresolved",
                "uncertainty": "image_unmeasured" if role == "meal_photo" else "portion_missing",
                "quantity_evidence": None,
            }
        )
    evidence = item.quantity_evidence
    food_words = _food_words(food_name, food_aliases)
    named = _names_food(evidence or "", food_words)
    source = _source_context(text, evidence, food_words)
    supported = False
    if (
        evidence is not None
        and source is not None
        and named
        and _single_food_quantity_evidence(evidence, food_words)
        # Date validity/conflicts are checked before quantity normalization. Its
        # hyphens cannot become signs/ranges, while actual mass signs remain.
        and not _UNCLEAR_MASS.search(_ISO_DATE.sub(" ", source).replace(",", "."))
        and not (_NON_EDIBLE_MASS.search(source) and not _EDIBLE_MASS.search(source))
        and len(tuple(_CAPTION_MASS.finditer(source))) == 1
    ):
        masses = tuple(_CAPTION_MASS.finditer(evidence))
        for match in masses if len(masses) == 1 else ():
            if _MASS_EVIDENCE_SEPARATOR.search(evidence):
                # A punctuation-separated amount may refer back to a named
                # food, but cannot borrow another food's weighed quantity.
                mass_clause = next(
                    clause
                    for clause in _MASS_EVIDENCE_SEPARATOR.split(evidence)
                    if match[0] in clause
                )
                if not _names_food(mass_clause, food_words) and not _measurement_reference(
                    mass_clause
                ):
                    continue
            unit = match[2].casefold()
            factor = (
                Decimal(1000)
                if unit.startswith("k")
                else (Decimal("0.001") if unit.startswith("m") else Decimal(1))
            )
            if Decimal(match[1].replace(",", ".")) * factor == Decimal(item.grams):
                supported = True
                break
    if not supported:
        return item.model_copy(
            update={
                "grams": None,
                "quantity_source": "unresolved",
                "uncertainty": "image_unmeasured" if role == "meal_photo" else "portion_missing",
                "quantity_evidence": None,
            }
        )
    approximate = bool(_APPROXIMATE.search(source or ""))
    return item.model_copy(
        update={
            "quantity_source": "approximate" if approximate else "reported",
            "uncertainty": "user_approximation" if approximate else "none",
            # Raw source excerpts are transient validation evidence, never ledger provenance.
            "quantity_evidence": None,
        }
    )


def validate_proposal(
    proposal: AiMealIntent,
    *,
    catalog: tuple[AiCatalogItem, ...],
    text: str,
    local_date: date,
    role: AiRole,
) -> AiMealIntent | None:
    """Validate exact context identities and safely resolve quantity provenance locally."""
    known = {food.food_version_id: food for food in catalog}
    input_date = _input_date(text, local_date)
    if any(item.food_version_id not in known for item in proposal.items) or any(
        item.food_version_id is not None
        and (
            item.food_version_id not in known
            or item.preparation != known[item.food_version_id].preparation
        )
        for item in proposal.discovery_items
    ):
        return None
    if input_date is None:
        # An invalid/conflicting source date cannot authorize a meal or food
        # discovery. A correctly bounded empty clarification stays actionable.
        if (
            proposal.intent == "clarify"
            and not proposal.items
            and not proposal.discovery_items
            and proposal.clarification_reason == "incomplete"
            and proposal.local_date == local_date
        ):
            return proposal.model_copy(
                update={"unresolved": (_INVALID_DATE_NOTE,), "full_input_accounted_for": False}
            )
        return None
    if proposal.local_date != input_date:
        return None
    if (proposal.intent == "meal" or proposal.discovery_items) and _conflicting_portion(
        text,
        tuple(_food_words(food.name, food.aliases) for food in catalog)
        + tuple(_food_words(item.query) for item in proposal.discovery_items),
    ):
        return AiMealIntent(
            intent="clarify",
            label=proposal.label,
            local_date=proposal.local_date,
            items=(),
            unresolved=(_CONFLICTING_PORTION_NOTE,),
            full_input_accounted_for=False,
            clarification_reason="portion",
        )
    if any(
        not _preparation_matches(item.identity_evidence, text, known[item.food_version_id])
        for item in proposal.items
    ) or any(
        item.food_version_id is not None
        and not _preparation_matches(
            item.identity_evidence or item.query,
            text,
            known[item.food_version_id],
        )
        for item in proposal.discovery_items
    ):
        return AiMealIntent(
            intent="clarify",
            label=proposal.label,
            local_date=proposal.local_date,
            items=(),
            unresolved=("Preparation needs review.",),
            full_input_accounted_for=False,
            clarification_reason="preparation",
        )
    items = tuple(
        cast(
            AiMealItem,
            _supported_quantity(
                item,
                text=text,
                food_name=known[item.food_version_id].name,
                food_aliases=known[item.food_version_id].aliases,
                role=role,
            ),
        )
        for item in proposal.items
    )
    discovery = tuple(
        cast(
            AiDiscoveryItem,
            _supported_quantity(
                item,
                text=text,
                food_name=item.query,
                role=role,
            ),
        )
        for item in proposal.discovery_items
    )
    return proposal.model_copy(update={"items": items, "discovery_items": discovery})


_QUALIFIERS = {
    "fried": r"\b(?:fried|frying|fry)\b",
    "grilled": r"\b(?:grilled|grilling|grill)\b",
    "stewed": r"\b(?:stewed|stewing|stew)\b",
    "boiled": r"\b(?:boiled|boiling|boil)\b",
    "steamed": r"\b(?:steamed|steaming|steam)\b",
    "roasted": r"\b(?:roasted|roasting|roast)\b",
    "baked": r"\b(?:baked|baking|bake)\b",
    "sauteed": r"\b(?:sauteed|sautéed|saute|sauté)\b",
    "unsalted": r"\b(?:unsalted|without salt|no salt)\b",
    "salted": r"\b(?:salted|with salt|salt added)\b",
    "unenriched": r"\b(?:unenriched|not enriched)\b",
    "enriched": r"\benriched\b",
    "unfortified": r"\b(?:unfortified|not fortified)\b",
    "fortified": r"\bfortified\b",
    "skinless": r"\b(?:skinless|without skin|no skin)\b",
    "with_skin": r"\b(?:with skin|skin-on|meat and skin)\b",
    "boneless": r"\b(?:boneless|without bones|no bones)\b",
    "bone_in": r"\b(?:bone-in|with bones)\b",
    "without_oil": r"\b(?:without oil|no oil|oil-free)\b",
    "with_oil": r"\b(?:with oil|in oil|oil added)\b",
}


def _qualifiers(text: str) -> set[str]:
    patterns = _QUALIFIERS | _METHOD_PATTERNS
    found = {key for key, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)}
    for negative, positive in (("unenriched", "enriched"), ("unfortified", "fortified")):
        if negative in found:
            found.discard(positive)
    return found


_METHOD_PATTERNS = {
    **{
        key: _QUALIFIERS[key]
        for key in (
            "fried",
            "grilled",
            "stewed",
            "boiled",
            "steamed",
            "roasted",
            "baked",
            "sauteed",
        )
    },
    "pan_fried": r"\bpan[- ]fried\b",
    "deep_fried": r"\bdeep[- ]fried\b",
    "air_fried": r"\bair[- ]fried\b",
    "braised": r"\b(?:braised|braising|braise)\b",
    "poached": r"\b(?:poached|poaching|poach)\b",
    "smoked": r"\b(?:smoked|smoking|smoke)\b",
    "microwaved": r"\b(?:microwaved|microwaving|microwave)\b",
    "pressure_cooked": r"\bpressure[- ]cooked\b",
    "toasted": r"\b(?:toasted|toasting|toast)\b",
    "broiled": r"\b(?:broiled|broiling|broil)\b",
    "blanched": r"\b(?:blanched|blanching|blanch)\b",
}
_ATTRIBUTE_PATTERNS = {
    **{key: pattern for key, pattern in _QUALIFIERS.items() if key not in _METHOD_PATTERNS},
    "peeled": r"\bpeeled\b",
    "unpeeled": r"\bunpeeled\b",
    "drained": r"\bdrained\b",
    "undrained": r"\bundrained\b",
    "without_sauce": r"\b(?:without sauce|no sauce)\b",
    "with_sauce": r"\bwith sauce\b",
}
_PREPARATION_CONTINUATION = _SOURCE_CONTINUATION | frozenset(
    "i we later subsequently then served ate eaten eat had pan deep air pressure "
    "braised braising braise poached poaching poach smoked smoking smoke microwaved "
    "microwaving microwave toasted toasting toast broiled broiling broil blanched "
    "blanching blanch drained undrained possibly uncertain unsure cooked cook".split()
)


def _preparation_prefix(source: str, offset: int) -> str:
    """Certainty belongs to this descriptor, never an earlier mass or method."""
    start = next((left for left, right, _ in _source_parts(source) if left <= offset < right), 0)
    prefix = source[start:offset]
    previous = [match.end() for match in _CAPTION_MASS.finditer(prefix)]
    for pattern in (_METHOD_PATTERNS | _ATTRIBUTE_PATTERNS).values():
        previous.extend(match.end() for match in re.finditer(pattern, prefix, re.I))
    return prefix[max(previous, default=0) :]


def _preparation_ambiguous(source: str) -> bool:
    before = re.search(r"\b(?:before cooking|weighed raw|dry weight)\b", source, re.I)
    after = re.search(r"\b(?:after cooking|weighed cooked)\b", source, re.I)
    if before and after:
        return True
    found = {
        key for key, pattern in _ATTRIBUTE_PATTERNS.items() if re.search(pattern, source, re.I)
    }
    for negative, positive in (("unenriched", "enriched"), ("unfortified", "fortified")):
        if negative in found:
            found.discard(positive)
    if any(
        first in found and second in found
        for first, second in (
            ("skinless", "with_skin"),
            ("boneless", "bone_in"),
            ("without_oil", "with_oil"),
            ("unsalted", "salted"),
            ("peeled", "unpeeled"),
            ("drained", "undrained"),
            ("without_sauce", "with_sauce"),
        )
    ):
        return True
    for key in found:
        for match in re.finditer(_ATTRIBUTE_PATTERNS[key], source, re.I):
            prefix = _preparation_prefix(source, match.start())
            if re.search(
                r"\b(?:not|never|possibly|maybe|perhaps|might|may|unsure|unknown)\b", prefix, re.I
            ):
                return True
    return False


def _preparation_source(
    text: str, evidence: str | None, food_words: tuple[frozenset[str], ...]
) -> str | None:
    """Bind notes to one food clause and only food-free preparation continuations.

    A broad model quotation spanning other foods cannot lend their preparation.
    Unknown prose and any additional amount stop a continuation.
    """
    parts = _source_parts(text)
    if evidence is not None:
        start = text.find(evidence)
        if start < 0 or text.find(evidence, start + 1) >= 0:
            return None
        end = start + len(evidence)
        matching = [
            index
            for index, (left, right, part) in enumerate(parts)
            if left < end and right > start and _names_food(part, food_words)
        ]
    else:
        matching = [
            index for index, (_, _, part) in enumerate(parts) if _names_food(part, food_words)
        ]
    if len(matching) != 1:
        return None
    first = last = matching[0]

    def continuation(part: str) -> bool:
        words = set(re.findall(r"[^\W_]+", part.casefold()))
        return (
            (bool(words) or not part.strip())
            and words.issubset(_PREPARATION_CONTINUATION)
            and not _CAPTION_MASS.search(part)
        )

    while first > 0 and continuation(parts[first - 1][2]):
        # A preparation-only prefix is attached by punctuation, never a meal's
        # preceding food introduced by an ordinary 'and/then/with' connector.
        separator = text[parts[first - 1][1] : parts[first][0]]
        if separator.strip() not in {",", ".", ";", ""}:
            break
        first -= 1
    while last + 1 < len(parts) and continuation(parts[last + 1][2]):
        last += 1
    return text[parts[first][0] : parts[last][1]]


def _reported_preparation(
    text: str, evidence: str | None, food_name: str, aliases: tuple[str, ...] = ()
) -> PreparationDetails | None:
    source = _preparation_source(text, evidence, _food_words(food_name, aliases))
    if source is None or _preparation_ambiguous(source):
        return None
    weight_basis: Literal["before_cooking", "after_cooking"] | None = None
    if re.search(r"\b(?:before cooking|weighed raw|dry weight)\b", source, re.IGNORECASE):
        weight_basis = "before_cooking"
    elif re.search(r"\b(?:after cooking|weighed cooked)\b", source, re.IGNORECASE):
        weight_basis = "after_cooking"
    elif (
        re.search(r"\b(?:raw|uncooked|dry)\b", source, re.I)
        and re.search(r"\b(?:weighed|measured|weight)\b", source, re.I)
        and _CAPTION_MASS.search(source)
    ):
        weight_basis = "before_cooking"
    found_methods = []
    for method, pattern in _METHOD_PATTERNS.items():
        for match in re.finditer(pattern, source, re.IGNORECASE):
            preceding = _preparation_prefix(source, match.start())
            certainty = (
                "possible"
                if re.search(
                    r"\b(?:maybe|possibly|perhaps|might|may|unsure|unknown|not sure)\b",
                    preceding,
                    re.I,
                )
                else "excluded"
                if re.search(r"\b(?:not|never|wasn't|was not)\b", preceding, re.I)
                else "reported"
            )
            value = ReportedCookingMethod(method=cast(CookingMethod, method), certainty=certainty)
            found_methods.append((match.start(), value))
    methods = list(
        dict.fromkeys(value for _, value in sorted(found_methods, key=lambda value: value[0]))
    )
    # More specific forms already say fried; avoid a duplicate generic label.
    if any(value.method in {"pan_fried", "deep_fried", "air_fried"} for value in methods):
        methods = [value for value in methods if value.method != "fried"]
    attributes = {
        key for key, pattern in _ATTRIBUTE_PATTERNS.items() if re.search(pattern, source, re.I)
    }
    for negative, positive in (("unenriched", "enriched"), ("unfortified", "fortified")):
        if negative in attributes:
            attributes.discard(positive)
    if weight_basis is None and not methods and not attributes:
        return None
    return PreparationDetails(
        weight_basis=weight_basis,
        methods=tuple(methods),
        attributes=tuple(cast(PreparationAttribute, value) for value in sorted(attributes)),
    )


def _preparation_matches(evidence: str | None, text: str, food: AiCatalogItem) -> bool:
    # A legacy response without its food span is checked against clauses naming
    # that food. New responses cannot discard the original enclosing preparation.
    if evidence is not None and evidence not in text:
        return False
    food_words = _food_words(food.name, food.aliases)
    source = _source_context(text, evidence, food_words)
    if source is None or not _names_food(source, food_words):
        return False
    preparation_source = _preparation_source(text, evidence, food_words)
    if preparation_source is None or _preparation_ambiguous(preparation_source):
        return False
    if _preparation_ambiguous(source):
        return False
    # A clearly measured raw ingredient can subsequently be cooked. Its mass
    # and nutrient source stay raw; the serving method is recorded separately.
    details = _reported_preparation(text, evidence, food.name, food.aliases)
    raw_weight_basis = (
        food.preparation == "raw"
        and details is not None
        and details.weight_basis == "before_cooking"
        and len(tuple(_CAPTION_MASS.finditer(preparation_source))) == 1
    )
    specified = _qualifiers(source)
    if (
        not raw_weight_basis
        and details is not None
        and any(value.certainty != "reported" for value in details.methods)
    ):
        return False
    if raw_weight_basis:
        # Only the later cooking method is independent of raw weighing. Keep
        # every skin, salt, enrichment and added-oil catalog qualifier check.
        specified -= _qualifiers(preparation_source) & set(_METHOD_PATTERNS)
    if (
        re.search(r"\b(?:raw|uncooked|before cooking|dry weight)\b", source, re.IGNORECASE)
        and food.preparation != "raw"
    ):
        return False
    if (
        not raw_weight_basis
        and re.search(r"\b(?:cooked|after cooking)\b", source, re.IGNORECASE)
        and food.preparation == "raw"
    ):
        return False
    return specified.issubset(_qualifiers(food.name))


def _input_date(text: str, local_date: date) -> date | None:
    """A model cannot invent an earlier date or resolve conflicting date instructions."""
    dates = set()
    try:
        day_words = {
            word: index
            for index, word in enumerate(("one", "two", "three", "four", "five", "six", "seven"), 1)
        }
        for phrase in re.findall(r"\b([a-z]+|[0-9]+)\s+days?\s+ago\b", text, re.IGNORECASE):
            offset = int(phrase) if phrase.isdecimal() else day_words.get(phrase.casefold(), 0)
            if not 1 <= offset <= 7:
                return None
            dates.add(local_date - timedelta(days=offset))
        for literal in _ISO_DATE.finditer(text):
            dates.add(date.fromisoformat(literal[0]))
        for word in re.findall(r"\b(today|yesterday|tomorrow)\b", text, re.IGNORECASE):
            offset = {"today": 0, "yesterday": -1, "tomorrow": 1}[word.casefold()]
            dates.add(local_date + timedelta(days=offset))
    except (ValueError, OverflowError):
        return None
    if len(dates) > 1 or any(day > local_date for day in dates):
        return None
    return next(iter(dates)) if dates else local_date


@dataclass
class _InterpretationMetrics:
    calls: list[AiCallMetrics] = field(default_factory=list)
    replayed: bool = False


class AiService:
    def __init__(
        self,
        store: Store,
        manifest: AiEndpointManifest | None = None,
        adapter: OpenRouterAdapter | None = None,
        *,
        evaluation_prefix: str | None = None,
        plan_adapter: ChatGPTPlanAdapter | None = None,
        evaluation_catalog: tuple[AiCatalogItem, ...] | None = None,
        text_enabled: bool = True,
        photo_enabled: bool = True,
    ):
        if evaluation_prefix is not None and not (
            evaluation_prefix.startswith("eval:")
            and 8 <= len(evaluation_prefix) <= 80
            and evaluation_prefix.endswith(":")
        ):
            raise ValueError("Use a bounded evaluation run prefix.")
        self.store = store
        self.manifest = manifest
        self.adapter = adapter
        self.evaluation_prefix = evaluation_prefix
        if evaluation_catalog is not None and evaluation_prefix is None:
            raise ValueError("Synthetic evaluation catalogs require an evaluation run scope.")
        self.plan_adapter = plan_adapter
        self.evaluation_catalog = evaluation_catalog
        # Synthetic evaluation reads this immediately after a sequential invocation.
        # It is never populated for ordinary requests or stored in the database/logs.
        self.evaluation_raw_proposal: AiMealIntent | None = None
        self._enabled_roles = {
            role
            for role, enabled in (("meal_text", text_enabled), ("meal_photo", photo_enabled))
            if enabled
        }
        self._disabled_route_ids: set[str] = set()
        self._lock = asyncio.Lock()

    def enabled_for(self, role: AiRole) -> bool:
        if role not in self._enabled_roles:
            return False
        if self.plan_adapter is not None:
            return self.plan_adapter.enabled_for(role)
        if self.adapter is None or self.manifest is None:
            return False
        try:
            # Endpoint policy dates use UTC; spending month boundaries are frozen locally.
            from datetime import UTC

            route = self.manifest.route(role, datetime.now(UTC).date())
            if route.id in self._disabled_route_ids:
                return False
        except AiUnavailable:
            return False
        return True

    async def close(self) -> None:
        if self.plan_adapter is not None:
            await self.plan_adapter.close()
        if self.adapter is not None:
            await self.adapter.close()

    async def _catalog(self, text: str = "") -> tuple[AiCatalogItem, ...]:
        if self.evaluation_catalog is not None:
            return self.evaluation_catalog
        async with self.store.engine.connect() as connection:
            return await ai_food_context(connection, text)

    async def interpret(
        self,
        *,
        request_key: str,
        text: str,
        local_date: date,
        photo: bytes | None = None,
    ) -> AiOutcome:
        self.evaluation_raw_proposal = None
        created_at = time.time()
        started = time.monotonic()
        measurement = _InterpretationMetrics()
        calls = measurement.calls
        try:
            outcome = await self._interpret(
                request_key=request_key,
                text=text,
                local_date=local_date,
                photo=photo,
                measurement=measurement,
            )
        except asyncio.CancelledError:

            async def abandon() -> None:
                await self.recover_abandoned(request_key=request_key)
                if calls:
                    calls[-1].failure_category = "interrupted"
                await self._record_measurement(
                    AiOutcome(
                        request_key=request_key,
                        role="meal_photo" if photo is not None else "meal_text",
                        status="unknown",
                    ),
                    calls,
                    created_at=created_at,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            cleanup = asyncio.create_task(abandon())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        if not measurement.replayed:
            await self._record_measurement(
                outcome,
                calls,
                created_at=created_at,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return outcome

    async def _record_measurement(
        self, outcome: AiOutcome, calls: list[AiCallMetrics], *, created_at: float, elapsed_ms: int
    ) -> None:
        """Optional measurements cannot turn a successful interpretation into a failure."""
        from nutrition_bot.application.ai_metrics import record_metric

        sent = [call for call in calls if call.inference_sent]

        def stage(field: str) -> int | None:
            values = [cast(int | None, getattr(call, field)) for call in calls]
            known = [value for value in values if value is not None]
            return sum(known) if known else None

        def usage(field: str) -> int | None:
            values = [cast(int | None, getattr(call, field)) for call in sent]
            if not values or any(value is None for value in values):
                return None
            return sum(value for value in values if value is not None)

        latest = calls[-1] if calls else AiCallMetrics()
        failure = latest.failure_category
        if outcome.status == "unknown" and failure is None:
            failure = "interrupted"
        try:
            async with self.store.write() as connection:
                await record_metric(
                    connection,
                    request_key=outcome.request_key,
                    role=outcome.role,
                    source="evaluation" if self.evaluation_prefix else "normal",
                    handling="ai",
                    status=outcome.status,
                    created_at=created_at,
                    completed_at=time.time(),
                    elapsed_ms=elapsed_ms,
                    model=latest.model,
                    prompt_version=latest.prompt_version,
                    schema_version=latest.schema_version,
                    reasoning_effort=latest.reasoning_effort,
                    auth_ms=stage("auth_ms"),
                    model_catalog_ms=stage("model_catalog_ms"),
                    inference_ms=stage("inference_ms"),
                    input_tokens=usage("input_tokens"),
                    output_tokens=usage("output_tokens"),
                    reasoning_tokens=usage("reasoning_tokens"),
                    cached_tokens=usage("cached_tokens"),
                    charged_micro_usd=usage("charged_micro_usd"),
                    inference_sent=bool(sent),
                    attempts_sent=len(sent),
                    failure_category=failure,
                )
        except Exception:
            logger.warning("ai_measurement_unavailable")

    async def recover_abandoned(self, *, request_key: str | None = None) -> None:
        """Run once before a sole worker starts; preserve uncertain cost and never replay."""
        async with self.store.write() as connection:
            self._disabled_route_ids.update(
                (await connection.scalars(sa.select(ai_disabled_routes.c.route_id))).all()
            )
            matching = ai_requests.c.state == "running"
            if request_key is not None:
                matching = sa.and_(matching, ai_requests.c.request_key == request_key)
            keys: tuple[str, ...] = tuple(
                (
                    await connection.scalars(sa.select(ai_requests.c.request_key).where(matching))
                ).all()
            )
            if not keys:
                return
            now = time.time()
            await connection.execute(
                sa.update(ai_attempts)
                .where(ai_attempts.c.request_key.in_(keys), ai_attempts.c.state == "reserved")
                .values(state="unknown", completed_at=now)
            )
            await connection.execute(
                sa.update(ai_plan_invocations)
                .where(
                    ai_plan_invocations.c.request_key.in_(keys),
                    ai_plan_invocations.c.state == "started",
                )
                .values(state="unknown", completed_at=now)
            )
            await connection.execute(
                sa.update(ai_requests)
                .where(ai_requests.c.request_key.in_(keys))
                .values(state="unknown", completed_at=now)
            )

    async def _interpret(
        self,
        *,
        request_key: str,
        text: str,
        local_date: date,
        photo: bytes | None = None,
        measurement: _InterpretationMetrics | None = None,
    ) -> AiOutcome:
        role: AiRole = "meal_photo" if photo is not None else "meal_text"
        if (
            not 1 <= len(request_key) <= 128
            or self.evaluation_prefix is not None
            and not request_key.startswith(self.evaluation_prefix)
            or self.evaluation_prefix is None
            and request_key.startswith("eval:")
        ):
            raise ValueError("The AI request key is invalid.")
        if not self.enabled_for(role):
            return AiOutcome(request_key=request_key, role=role, status="disabled")
        if len(text.encode()) > MAX_AI_TEXT_BYTES:
            return AiOutcome(request_key=request_key, role=role, status="unavailable")
        if self.plan_adapter is not None:
            return await self._interpret_plan(
                request_key=request_key,
                text=text,
                local_date=local_date,
                photo=photo,
                role=role,
                measurement=measurement,
            )
        assert self.manifest is not None and self.adapter is not None
        # One paid request at a time. Persistent claims also prevent a second
        # worker/CLI from replaying a sent attempt after a process interruption.
        async with self._lock:
            async with self.store.write() as connection:
                await purge_ai_outcomes(connection)
                existing = (
                    (
                        await connection.execute(
                            sa.select(ai_requests).where(ai_requests.c.request_key == request_key)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if measurement is not None:
                        measurement.replayed = True
                    if existing["outcome"] is not None:
                        return AiOutcome.model_validate(existing["outcome"])
                    # Never restart a claimed request whose result was lost or consumed.
                    outcome = AiOutcome(request_key=request_key, role=role, status="unknown")
                    if existing["state"] == "running":
                        await connection.execute(
                            sa.update(ai_requests)
                            .where(ai_requests.c.request_key == request_key)
                            .values(state="unknown", completed_at=time.time())
                        )
                    return outcome
                # Global lock across process instances: a reserved attempt cannot
                # start another paid request. Crash-left reservations require reconciliation.
                if await connection.scalar(
                    sa.select(ai_attempts.c.id).where(ai_attempts.c.state == "reserved").limit(1)
                ) or await connection.scalar(
                    sa.select(ai_requests.c.request_key)
                    .where(ai_requests.c.state == "running")
                    .limit(1)
                ):
                    return AiOutcome(request_key=request_key, role=role, status="unknown")
                await connection.execute(
                    sa.insert(ai_requests).values(
                        request_key=request_key,
                        role=role,
                        state="running",
                        outcome=None,
                        created_at=time.time(),
                    )
                )
            from datetime import UTC

            route = self.manifest.route(role, datetime.now(UTC).date())
            catalog = await self._catalog(text)
            try:
                body = self.adapter.request_body(
                    route, text=text, catalog=catalog, local_date=local_date, photo=photo
                )
            except AiUnavailable:
                return await self._finish(request_key, role, "unavailable")
            for attempt in (1, 2):
                async with self.store.write() as connection:
                    attempt_id = await reserve_attempt(
                        connection,
                        request_key=request_key,
                        attempt=attempt,
                        route=route,
                        now=time.time(),
                        evaluation_prefix=self.evaluation_prefix,
                    )
                if attempt_id is None:
                    return await self._finish(request_key, role, "budget")
                try:
                    metrics = AiCallMetrics()
                    if measurement is not None:
                        measurement.calls.append(metrics)
                    # Every attempt is bounded, even injected transport clients.
                    async with asyncio.timeout(30):
                        completion = await self.adapter.complete(route, body, metrics=metrics)
                except (AiTransportError, TimeoutError):
                    if metrics.failure_category is None:
                        metrics.failure_category = "timeout"
                    async with self.store.write() as connection:
                        await settle_attempt(
                            connection,
                            attempt_id,
                            charge=None,
                            generation_id=None,
                            route_id=route.id,
                            now=time.time(),
                        )
                    return await self._finish(request_key, role, "unknown")
                # Cancellation deliberately leaves its durable reservation intact.
                async with self.store.write() as connection:
                    await settle_attempt(
                        connection,
                        attempt_id,
                        charge=completion.charged_micro_usd,
                        generation_id=completion.generation_id,
                        fault=completion.fault,
                        route_id=route.id,
                        now=time.time(),
                    )
                if completion.charged_micro_usd is None:
                    return await self._finish(request_key, role, "unknown")
                if completion.fault in {"policy", "usage", "pricing"}:
                    self._disabled_route_ids.add(route.id)
                    return await self._finish(request_key, role, "unavailable")
                if completion.proposal is not None:
                    if self.evaluation_prefix is not None:
                        self.evaluation_raw_proposal = completion.proposal
                    proposal = validate_proposal(
                        completion.proposal,
                        catalog=catalog,
                        text=text,
                        local_date=local_date,
                        role=role,
                    )
                    if proposal is None:
                        metrics.failure_category = "schema"
                        return await self._finish(request_key, role, "unavailable")
                    outcome = AiOutcome(
                        request_key=request_key,
                        role=role,
                        proposal=proposal,
                        status="ready" if proposal.intent == "meal" else "clarify",
                    )
                    async with self.store.write() as connection:
                        await save_outcome(connection, outcome, time.time())
                    return outcome
                # A schema-only error with returned billing can use one fully
                # reserved retry. Clarification and unknown billing never retry.
            return await self._finish(request_key, role, "unavailable")
        raise AssertionError("The bounded AI attempt loop must return.")

    async def _interpret_plan(
        self,
        *,
        request_key: str,
        text: str,
        local_date: date,
        photo: bytes | None,
        role: AiRole,
        measurement: _InterpretationMetrics | None = None,
    ) -> AiOutcome:
        from datetime import UTC

        assert self.plan_adapter is not None
        async with self._lock:
            async with self.store.write() as connection:
                await purge_ai_outcomes(connection)
                existing = (
                    (
                        await connection.execute(
                            sa.select(ai_requests).where(ai_requests.c.request_key == request_key)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if measurement is not None:
                        measurement.replayed = True
                    if existing["outcome"] is not None:
                        return AiOutcome.model_validate(existing["outcome"])
                    return AiOutcome(request_key=request_key, role=role, status="unknown")
                if await connection.scalar(
                    sa.select(ai_requests.c.request_key)
                    .where(ai_requests.c.state == "running")
                    .limit(1)
                ):
                    return AiOutcome(request_key=request_key, role=role, status="unknown")
                utc_day = datetime.now(UTC).date()
                day = utc_day.isoformat()
                evaluation_pool = self.evaluation_prefix is not None
                is_evaluation = sa.func.substr(ai_plan_invocations.c.request_key, 1, 5) == "eval:"
                count = int(
                    await connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(ai_plan_invocations)
                        .where(
                            ai_plan_invocations.c.day == day,
                            is_evaluation if evaluation_pool else ~is_evaluation,
                        )
                    )
                    or 0
                )
                limit = (
                    self.plan_adapter.policy.effective_evaluation_limit(utc_day)
                    if evaluation_pool
                    else self.plan_adapter.policy.daily_invocation_limit
                )
                if count >= limit:
                    return AiOutcome(request_key=request_key, role=role, status="quota")
                await connection.execute(
                    sa.insert(ai_requests).values(
                        request_key=request_key, role=role, state="running", created_at=time.time()
                    )
                )
            catalog = await self._catalog(text)
            try:
                body = self.plan_adapter.request_body(
                    role=role, text=text, catalog=catalog, local_date=local_date, photo=photo
                )
            except AiUnavailable:
                return await self._finish(request_key, role, "unavailable")
            async with self.store.write() as connection:
                await connection.execute(
                    sa.insert(ai_plan_invocations).values(
                        request_key=request_key, day=day, state="started", created_at=time.time()
                    )
                )
            try:
                metrics = AiCallMetrics()
                if measurement is not None:
                    measurement.calls.append(metrics)
                async with asyncio.timeout(30):
                    completion = await self.plan_adapter.complete(
                        role=role, body=body, metrics=metrics
                    )
            except (AiUnavailable, TimeoutError):
                if metrics.failure_category is None:
                    metrics.failure_category = "timeout"
                async with self.store.write() as connection:
                    await connection.execute(
                        sa.update(ai_plan_invocations)
                        .where(ai_plan_invocations.c.request_key == request_key)
                        .values(state="unknown", completed_at=time.time())
                    )
                return await self._finish(request_key, role, "unavailable")
            if self.evaluation_prefix is not None:
                self.evaluation_raw_proposal = completion
            proposal = validate_proposal(
                completion,
                catalog=catalog,
                text=text,
                local_date=local_date,
                role=role,
            )
            valid = proposal is not None
            if not valid:
                metrics.failure_category = "schema"
            outcome = AiOutcome(
                request_key=request_key,
                role=role,
                proposal=proposal if valid else None,
                status=(
                    "ready" if proposal is not None and proposal.intent == "meal" else "clarify"
                )
                if valid
                else "unavailable",
            )
            async with self.store.write() as connection:
                await connection.execute(
                    sa.update(ai_plan_invocations)
                    .where(ai_plan_invocations.c.request_key == request_key)
                    .values(state="done", completed_at=time.time())
                )
                await save_outcome(connection, outcome, time.time())
            return outcome

    async def _finish(self, request_key: str, role: AiRole, status: str) -> AiOutcome:
        if status == "clarify":
            # No catalog is a safe manual-entry fallback, not a model proposal.
            status = "unavailable"
        outcome = AiOutcome.model_validate(
            {"request_key": request_key, "role": role, "status": status}
        )
        async with self.store.write() as connection:
            await save_outcome(connection, outcome, time.time())
        return outcome


async def create_ai_draft(
    connection: AsyncConnection,
    outcome: AiOutcome,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    owner_id: int | None = None,
    remote_enabled: bool = False,
) -> MealReply:
    """Root's authorized action creates an ordinary revision-bound reviewed draft."""
    from nutrition_bot.domain.ai import quantity_basis, uncertainty_message

    source_text = message.caption or message.text or ""
    source_catalog = (
        {
            value.food_version_id: value.aliases
            for value in await ai_food_context(connection, source_text)
        }
        if outcome.proposal is not None
        else {}
    )

    label = (
        {
            "breakfast": "Breakfast",
            "lunch": "Lunch",
            "dinner": "Dinner",
            "snack": "Snack",
            "meal": "Meal",
        }.get(outcome.proposal.label.strip().casefold(), "Meal")
        if outcome.proposal is not None
        else "Meal"
    )
    if outcome.status == "clarify" and outcome.proposal is not None:
        proposal = outcome.proposal
        await consume_ai_outcome(connection, outcome.request_key)
        if proposal.discovery_items and owner_id is not None:
            from nutrition_bot.application.food_discovery import start_ai_discovery

            pending = []
            for discovery_item in proposal.discovery_items:
                food = (
                    await get_food_version(connection, discovery_item.food_version_id)
                    if discovery_item.food_version_id is not None
                    else None
                )
                basis = quantity_basis(discovery_item, outcome.role)
                pending.append(
                    {
                        "food_id": food.version_id if food else None,
                        "name": food.record.name if food else discovery_item.query,
                        "query": discovery_item.preparation.replace("_", " ")
                        + " "
                        + discovery_item.query,
                        "preparation": discovery_item.preparation,
                        "quantity": (
                            ("about " if basis else "") + discovery_item.grams + "g"
                            if discovery_item.grams is not None
                            else None
                        ),
                        "ai_basis": basis,
                        "preparation_details": (
                            details.model_dump(mode="json")
                            if (
                                details := _reported_preparation(
                                    message.caption or message.text or "",
                                    discovery_item.identity_evidence,
                                    food.record.name if food else discovery_item.query,
                                    source_catalog.get(food.version_id, ()) if food else (),
                                )
                            )
                            is not None
                            else None
                        ),
                    }
                )
            return await start_ai_discovery(
                connection,
                pending,
                message,
                action_key=action_key,
                reference=reference,
                owner_id=owner_id,
                remote_enabled=remote_enabled,
                local_date=proposal.local_date,
                label=label,
                request_key=outcome.request_key,
            )
        date_unclear = proposal.unresolved == (_INVALID_DATE_NOTE,)
        return MealReply(
            "AI proposal needs clarification · "
            + ("date unresolved" if date_unclear else str(proposal.local_date))
            + f" · {label}\n"
            + (
                _INVALID_DATE_NOTE
                if date_unclear
                else uncertainty_message(proposal.clarification_reason)
            )
            + "\nSend a complete description with those details, or enter the meal manually. "
            "Nothing has been saved.",
            "ai_status",
            ui_buttons=(("Enter meal manually", "log"),),
        )
    if outcome.status != "ready" or outcome.proposal is None:
        await consume_ai_outcome(connection, outcome.request_key)
        return MealReply(
            outcome.manual_message,
            "ai_status",
            ui_buttons=(("Enter meal manually", "log"),),
        )
    proposal = outcome.proposal
    planned = []
    for item in proposal.items:
        food = await get_food_version(connection, item.food_version_id)
        if food.record.preparation == "unspecified":
            raise AiUnavailable("Choose a reviewed food with a known preparation.")
        mass = grams_to_milligrams(exact_decimal(item.grams)) if item.grams is not None else None
        planned.append(
            PlannedItem(
                food_version_id=food.version_id,
                edible_milligrams=mass,
                original_quantity=item.grams,
                original_unit="g" if item.grams is not None else None,
                # Only fixed local provenance text survives model interpretation.
                estimate_basis=quantity_basis(item, outcome.role),
                preparation_details=_reported_preparation(
                    message.caption or message.text or "",
                    item.identity_evidence,
                    food.record.name,
                    source_catalog.get(food.version_id, ()),
                ),
            )
        )
    content = DraftContent(
        label=label,
        local_date=proposal.local_date,
        timezone=str(reference.tzinfo),
        consumed_at=_date_timestamp(proposal.local_date, reference),
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        items=tuple(planned),
        review_required=True,
    )
    draft = await create_draft(connection, content, action_key=action_key, now=time.time())
    from nutrition_bot.application.ai_metrics import link_draft

    await link_draft(connection, outcome.request_key, draft.id, draft.revision)
    await consume_ai_outcome(connection, outcome.request_key)
    return await draft_receipt(connection, draft, "AI proposal — review every item")
