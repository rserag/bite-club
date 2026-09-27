import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intake_components,
    supplement_intake_revisions,
    supplement_intakes,
    supplement_product_aliases,
    supplement_product_components,
    supplement_product_versions,
    supplement_products,
    supplement_substances,
)


class SupplementError(ValueError):
    pass


@dataclass(frozen=True)
class SupplementProductSnapshot:
    id: int
    version_id: int
    name: str
    serving_description: str
    creatine_amount_scaled: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class SupplementIntakeSnapshot:
    id: int
    revision_id: int
    revision_number: int
    local_date: date
    consumed_at: float
    timezone: str
    substance_code: str | None
    amount_scaled: int | None
    product_version_id: int | None
    product_name: str | None
    serving_description: str | None
    servings_scaled: int | None
    component_amount_scaled: int
    deleted: bool
    operation: str


def normalize_supplement_alias(value: str) -> str:
    normalized = " ".join(value.casefold().strip().split())
    if not normalized or len(normalized) > 100 or not re.search(r"[a-z0-9]", normalized):
        raise SupplementError("Use a short supplement name containing letters or numbers.")
    return normalized


async def ensure_creatine_substance(connection: AsyncConnection) -> None:
    await connection.execute(
        insert(supplement_substances)
        .values(
            code="creatine_monohydrate",
            name="Creatine monohydrate",
            category="performance_compound",
            canonical_unit="mg",
            nutrient_code=None,
            definition="Creatine monohydrate tracked as a performance compound, not a nutrient.",
            source_reference=(
                "https://ods.od.nih.gov/factsheets/"
                "ExerciseAndAthleticPerformance-HealthProfessional/"
            ),
            reviewed_at=1790035200.0,
        )
        .on_conflict_do_nothing(index_elements=["code"])
    )


async def _require_action(connection: AsyncConnection, action_key: str) -> None:
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise SupplementError("This action is unavailable; send the supplement request again.")


async def create_reviewed_creatine_product(
    connection: AsyncConnection,
    *,
    action_key: str,
    name: str,
    serving_description: str,
    amount_scaled: int,
    aliases: tuple[str, ...],
) -> SupplementProductSnapshot:
    """Publish one exact, manually reviewed label snapshot.

    The structured Telegram command is the review action. No OCR or inferred label
    value reaches this function.
    """
    await _require_action(connection, action_key)
    name = name.strip()
    serving_description = serving_description.strip()
    if not 1 <= len(name) <= 200:
        raise SupplementError("Product name must contain 1–200 characters.")
    if not 1 <= len(serving_description) <= 200:
        raise SupplementError("Serving description must contain 1–200 characters.")
    if amount_scaled <= 0:
        raise SupplementError("Label amount must be positive.")
    normalized_aliases = {
        normalize_supplement_alias(value): value.strip() for value in (name, *aliases)
    }
    collision = await connection.scalar(
        sa.select(supplement_product_aliases.c.normalized_name).where(
            supplement_product_aliases.c.normalized_name.in_(normalized_aliases)
        )
    )
    if collision:
        raise SupplementError(f"The supplement name '{collision}' is already in use.")
    await ensure_creatine_substance(connection)
    now = time.time()
    product_id = (
        await connection.execute(
            sa.insert(supplement_products)
            .values(current_version_id=None, created_at=now)
            .returning(supplement_products.c.id)
        )
    ).scalar_one()
    label = {
        "amount_scaled": amount_scaled,
        "component": "creatine_monohydrate",
        "name": name,
        "serving_description": serving_description,
    }
    content_hash = hashlib.sha256(
        json.dumps(label, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    version_id = (
        await connection.execute(
            sa.insert(supplement_product_versions)
            .values(
                product_id=product_id,
                version_number=1,
                previous_version_id=None,
                review_action_key=action_key,
                name=name,
                brand=None,
                form="other",
                jurisdiction=None,
                serving_description=serving_description,
                source_kind="manual_label",
                source_external_id=None,
                source_reference="User-reviewed Telegram label entry",
                source_url=None,
                source_license=None,
                reviewed_at=now,
                content_sha256=content_hash,
                sealed=False,
            )
            .returning(supplement_product_versions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(supplement_product_components).values(
            product_version_id=version_id,
            component_index=1,
            substance_code="creatine_monohydrate",
            printed_name="Creatine monohydrate",
            chemical_form="monohydrate",
            comparison="exact",
            source_amount=str(safer_decimal_amount(amount_scaled)),
            source_unit="mg",
            amount_scaled=amount_scaled,
            conversion_version="mass-v1",
            daily_value_percent=None,
            note=None,
        )
    )
    await connection.execute(
        sa.insert(supplement_product_aliases),
        [
            {
                "normalized_name": normalized,
                "display_name": display,
                "product_id": product_id,
                "created_at": now,
            }
            for normalized, display in sorted(normalized_aliases.items())
        ],
    )
    await connection.execute(
        sa.update(supplement_product_versions)
        .where(supplement_product_versions.c.id == version_id)
        .values(sealed=True)
    )
    await connection.execute(
        sa.update(supplement_products)
        .where(supplement_products.c.id == product_id)
        .values(current_version_id=version_id)
    )
    return await get_supplement_product(connection, product_id)


def safer_decimal_amount(amount_scaled: int) -> str:
    """Return the exact canonical-unit amount printed into the reviewed snapshot."""
    from decimal import Decimal

    return str(Decimal(amount_scaled) / 1_000_000)


async def get_supplement_product(
    connection: AsyncConnection, product_id: int
) -> SupplementProductSnapshot:
    row = (
        (
            await connection.execute(
                sa.select(
                    supplement_products.c.id,
                    supplement_product_versions.c.id.label("version_id"),
                    supplement_product_versions.c.name,
                    supplement_product_versions.c.serving_description,
                    supplement_product_components.c.amount_scaled,
                )
                .join(
                    supplement_product_versions,
                    supplement_product_versions.c.id == supplement_products.c.current_version_id,
                )
                .join(
                    supplement_product_components,
                    supplement_product_components.c.product_version_id
                    == supplement_product_versions.c.id,
                )
                .where(supplement_products.c.id == product_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["amount_scaled"] is None:
        raise SupplementError("That supplement product is unavailable.")
    aliases = tuple(
        (
            await connection.execute(
                sa.select(supplement_product_aliases.c.display_name)
                .where(supplement_product_aliases.c.product_id == product_id)
                .order_by(supplement_product_aliases.c.display_name)
            )
        )
        .scalars()
        .all()
    )
    return SupplementProductSnapshot(
        id=row["id"],
        version_id=row["version_id"],
        name=row["name"],
        serving_description=row["serving_description"],
        creatine_amount_scaled=row["amount_scaled"],
        aliases=aliases,
    )


async def list_supplement_products(
    connection: AsyncConnection,
) -> list[SupplementProductSnapshot]:
    ids = (await connection.execute(sa.select(supplement_products.c.id))).scalars().all()
    return [await get_supplement_product(connection, product_id) for product_id in ids]


async def resolve_supplement_product(
    connection: AsyncConnection, alias: str
) -> SupplementProductSnapshot:
    product_id = await connection.scalar(
        sa.select(supplement_product_aliases.c.product_id).where(
            supplement_product_aliases.c.normalized_name == normalize_supplement_alias(alias)
        )
    )
    if product_id is None:
        raise SupplementError("That supplement name is not saved. Add it with /supplement product.")
    return await get_supplement_product(connection, product_id)


def _snapshot(row: sa.RowMapping) -> SupplementIntakeSnapshot:
    if row["status"] != "taken" or row["component_amount_scaled"] is None:
        raise SupplementError("That supplement entry is unavailable.")
    return SupplementIntakeSnapshot(
        id=row["intake_id"],
        revision_id=row["id"],
        revision_number=row["revision_number"],
        local_date=row["local_date"],
        consumed_at=row["consumed_at"],
        timezone=row["timezone"],
        substance_code=row["substance_code"],
        amount_scaled=row["amount_scaled"],
        product_version_id=row["product_version_id"],
        product_name=row["product_name"],
        serving_description=row["serving_description"],
        servings_scaled=row["servings_scaled"],
        component_amount_scaled=row["component_amount_scaled"],
        deleted=row["deleted"],
        operation=row["operation"],
    )


async def get_supplement_intake(
    connection: AsyncConnection, intake_id: int
) -> SupplementIntakeSnapshot:
    row = (
        (
            await connection.execute(
                sa.select(
                    *supplement_intake_revisions.c,
                    supplement_product_versions.c.name.label("product_name"),
                    supplement_product_versions.c.serving_description,
                    supplement_intake_components.c.amount_scaled.label("component_amount_scaled"),
                )
                .join(
                    supplement_intakes,
                    supplement_intakes.c.current_revision_id == supplement_intake_revisions.c.id,
                )
                .outerjoin(
                    supplement_product_versions,
                    supplement_product_versions.c.id
                    == supplement_intake_revisions.c.product_version_id,
                )
                .join(
                    supplement_intake_components,
                    supplement_intake_components.c.intake_revision_id
                    == supplement_intake_revisions.c.id,
                )
                .where(supplement_intakes.c.id == intake_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise SupplementError("That supplement entry is unavailable.")
    return _snapshot(row)


async def _validate_action(connection: AsyncConnection, action_key: str) -> None:
    await _require_action(connection, action_key)
    if await connection.scalar(
        sa.select(supplement_intake_revisions.c.id).where(
            supplement_intake_revisions.c.action_key == action_key
        )
    ):
        raise SupplementError("This action was already applied; open the latest receipt.")


async def _publish(
    connection: AsyncConnection,
    *,
    intake_id: int,
    current: SupplementIntakeSnapshot | None,
    action_key: str,
    local_date: date,
    consumed_at: float,
    timezone: str,
    substance_code: str | None,
    amount_scaled: int | None,
    product_version_id: int | None = None,
    product_servings_scaled: int | None = None,
    component_amount_scaled: int | None = None,
    deleted: bool,
    operation: str,
) -> SupplementIntakeSnapshot:
    revision_id = (
        await connection.execute(
            sa.insert(supplement_intake_revisions)
            .values(
                intake_id=intake_id,
                revision_number=current.revision_number + 1 if current else 1,
                previous_revision_id=current.revision_id if current else None,
                action_key=action_key,
                local_date=local_date,
                consumed_at=consumed_at,
                timezone=timezone,
                status="taken",
                product_version_id=product_version_id,
                servings_scaled=product_servings_scaled,
                substance_code=substance_code,
                amount_scaled=amount_scaled,
                regimen_revision_id=None,
                phase_index=None,
                slot_index=None,
                sealed=False,
                deleted=deleted,
                operation=operation,
                created_at=time.time(),
            )
            .returning(supplement_intake_revisions.c.id)
        )
    ).scalar_one()
    actual_component = amount_scaled if component_amount_scaled is None else component_amount_scaled
    if actual_component is None:
        raise SupplementError("Supplement component amount is unavailable.")
    await connection.execute(
        sa.insert(supplement_intake_components).values(
            intake_revision_id=revision_id,
            component_index=1,
            substance_code="creatine_monohydrate",
            amount_scaled=actual_component,
            value_origin="label_scaled" if product_version_id else "direct_reported",
        )
    )
    await connection.execute(
        sa.update(supplement_intake_revisions)
        .where(supplement_intake_revisions.c.id == revision_id)
        .values(sealed=True)
    )
    await connection.execute(
        sa.update(supplement_intakes)
        .where(supplement_intakes.c.id == intake_id)
        .values(current_revision_id=revision_id)
    )
    return await get_supplement_intake(connection, intake_id)


async def create_direct_supplement_intake(
    connection: AsyncConnection,
    *,
    action_key: str,
    source_chat_id: int,
    source_message_id: int,
    local_date: date,
    consumed_at: float,
    timezone: str,
    substance_code: str,
    amount_scaled: int,
) -> SupplementIntakeSnapshot:
    await _validate_action(connection, action_key)
    if amount_scaled <= 0:
        raise SupplementError("Supplement amount must be positive.")
    if await connection.scalar(
        sa.select(supplement_intakes.c.id).where(
            supplement_intakes.c.source_chat_id == source_chat_id,
            supplement_intakes.c.source_message_id == source_message_id,
        )
    ):
        raise SupplementError("That message already has a supplement entry.")
    await ensure_creatine_substance(connection)
    intake_id = (
        await connection.execute(
            sa.insert(supplement_intakes)
            .values(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                created_at=time.time(),
            )
            .returning(supplement_intakes.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection,
        intake_id=intake_id,
        current=None,
        action_key=action_key,
        local_date=local_date,
        consumed_at=consumed_at,
        timezone=timezone,
        substance_code=substance_code,
        amount_scaled=amount_scaled,
        deleted=False,
        operation="create",
    )


async def create_product_supplement_intake(
    connection: AsyncConnection,
    *,
    action_key: str,
    source_chat_id: int,
    source_message_id: int,
    local_date: date,
    consumed_at: float,
    timezone: str,
    product: SupplementProductSnapshot,
    product_servings_scaled: int,
) -> SupplementIntakeSnapshot:
    await _validate_action(connection, action_key)
    if not 1 <= product_servings_scaled <= 100_000_000:
        raise SupplementError("Servings must be greater than 0 and no more than 100.")
    if await connection.scalar(
        sa.select(supplement_intakes.c.id).where(
            supplement_intakes.c.source_chat_id == source_chat_id,
            supplement_intakes.c.source_message_id == source_message_id,
        )
    ):
        raise SupplementError("That message already has a supplement entry.")
    intake_id = (
        await connection.execute(
            sa.insert(supplement_intakes)
            .values(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                created_at=time.time(),
            )
            .returning(supplement_intakes.c.id)
        )
    ).scalar_one()
    total = product.creatine_amount_scaled * product_servings_scaled // 1_000_000
    return await _publish(
        connection,
        intake_id=intake_id,
        current=None,
        action_key=action_key,
        local_date=local_date,
        consumed_at=consumed_at,
        timezone=timezone,
        substance_code=None,
        amount_scaled=None,
        product_version_id=product.version_id,
        product_servings_scaled=product_servings_scaled,
        component_amount_scaled=total,
        deleted=False,
        operation="create",
    )


async def revise_supplement_intake(
    connection: AsyncConnection,
    intake_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
    amount_scaled: int | None = None,
    product_servings_scaled: int | None = None,
    delete: bool = False,
) -> SupplementIntakeSnapshot:
    current = await get_supplement_intake(connection, intake_id)
    if current.revision_id != expected_revision_id:
        raise SupplementError("That receipt is old. Open /supplement history for the latest one.")
    await _validate_action(connection, action_key)
    if delete and current.deleted:
        raise SupplementError("That entry is already deleted; use Undo.")
    if amount_scaled is not None and amount_scaled <= 0:
        raise SupplementError("Supplement amount must be positive.")
    if current.product_version_id is not None:
        if amount_scaled is not None:
            raise SupplementError("Edit this product intake with servings, such as 1 serving.")
        next_servings = (
            current.servings_scaled if product_servings_scaled is None else product_servings_scaled
        )
        if (
            next_servings is None
            or not 1 <= next_servings <= 100_000_000
            or current.servings_scaled is None
        ):
            raise SupplementError("Servings must be greater than 0 and no more than 100.")
        component = current.component_amount_scaled * next_servings // current.servings_scaled
        next_amount = None
    else:
        if product_servings_scaled is not None:
            raise SupplementError("Edit this direct dose with an amount such as 5 g.")
        next_servings = None
        next_amount = current.amount_scaled if amount_scaled is None else amount_scaled
        if next_amount is None:
            raise SupplementError("That direct dose is unavailable.")
        component = next_amount
    return await _publish(
        connection,
        intake_id=intake_id,
        current=current,
        action_key=action_key,
        local_date=current.local_date,
        consumed_at=current.consumed_at,
        timezone=current.timezone,
        substance_code=current.substance_code,
        amount_scaled=next_amount,
        product_version_id=current.product_version_id,
        product_servings_scaled=next_servings,
        component_amount_scaled=component,
        deleted=True if delete else current.deleted,
        operation="delete" if delete else "edit",
    )


async def undo_supplement_intake(
    connection: AsyncConnection,
    intake_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
) -> SupplementIntakeSnapshot:
    current = await get_supplement_intake(connection, intake_id)
    if current.revision_id != expected_revision_id:
        raise SupplementError("That receipt is old. Open /supplement history for the latest one.")
    await _validate_action(connection, action_key)
    previous_id = await connection.scalar(
        sa.select(supplement_intake_revisions.c.previous_revision_id).where(
            supplement_intake_revisions.c.id == current.revision_id
        )
    )
    previous = None
    if previous_id is not None:
        previous = (
            (
                await connection.execute(
                    sa.select(
                        *supplement_intake_revisions.c,
                        supplement_intake_components.c.amount_scaled.label(
                            "component_amount_scaled"
                        ),
                    )
                    .join(
                        supplement_intake_components,
                        supplement_intake_components.c.intake_revision_id
                        == supplement_intake_revisions.c.id,
                    )
                    .where(supplement_intake_revisions.c.id == previous_id)
                )
            )
            .mappings()
            .one()
        )
    return await _publish(
        connection,
        intake_id=intake_id,
        current=current,
        action_key=action_key,
        local_date=current.local_date,
        consumed_at=current.consumed_at,
        timezone=current.timezone,
        substance_code=current.substance_code if previous is None else previous["substance_code"],
        amount_scaled=current.amount_scaled if previous is None else previous["amount_scaled"],
        product_version_id=(
            current.product_version_id if previous is None else previous["product_version_id"]
        ),
        product_servings_scaled=(
            current.servings_scaled if previous is None else previous["servings_scaled"]
        ),
        component_amount_scaled=(
            current.component_amount_scaled
            if previous is None
            else previous["component_amount_scaled"]
        ),
        deleted=True if previous is None else previous["deleted"],
        operation="undo",
    )


async def recent_supplement_intakes(
    connection: AsyncConnection, limit: int = 10
) -> list[SupplementIntakeSnapshot]:
    ids = (
        (
            await connection.execute(
                sa.select(supplement_intakes.c.id)
                .order_by(supplement_intakes.c.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [await get_supplement_intake(connection, value) for value in ids]
