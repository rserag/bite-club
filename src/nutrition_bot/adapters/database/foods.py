"""Local food catalog. Callers own transactions; this module never commits."""

import hashlib
import json
import time
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import (
    food_nutrients,
    food_portions,
    food_versions,
    foods,
    nutrients,
)
from nutrition_bot.domain.food import (
    FrozenModel,
    NutrientDefinition,
    ReviewedFoodInput,
    Unit,
    convert_unit,
    grams_to_milligrams,
    milligrams_to_grams,
    per_100g_scaled,
    scale_nutrient,
)
from nutrition_bot.domain.food_source import SourceProvenance

CALCULATION_VERSION = "food-per100g-micro-v1"


class StoredNutrient(FrozenModel):
    code: str
    unit: Unit
    amount_scaled: int | None
    source_amount: str | None
    source_unit: Unit
    quality: str
    note: str | None


class StoredPortion(FrozenModel):
    label: str
    edible_milligrams: int
    original_measure: str
    source: str
    is_estimate: bool


class FoodSnapshot(FrozenModel):
    food_id: int
    version_id: int
    version_number: int
    record: ReviewedFoodInput
    nutrients: tuple[StoredNutrient, ...]
    portions: tuple[StoredPortion, ...]
    content_sha256: str
    source_kind: str
    reviewed_at: float
    calculation_version: str
    provenance: SourceProvenance | None = None

    def amount_for(self, code: str, edible_milligrams: int) -> Decimal | None:
        value = next((item.amount_scaled for item in self.nutrients if item.code == code), None)
        return scale_nutrient(value, edible_milligrams)


async def register_nutrient(connection: AsyncConnection, definition: NutrientDefinition) -> None:
    """Explicitly extend the registry; an existing code's meaning cannot be overwritten."""
    existing = (
        (await connection.execute(sa.select(nutrients).where(nutrients.c.code == definition.code)))
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if dict(existing) != definition.model_dump():
            raise ValueError("Existing nutrient definition differs")
        return
    await connection.execute(sa.insert(nutrients).values(**definition.model_dump()))


async def normalize_food(
    connection: AsyncConnection, record: ReviewedFoodInput, *, quality: str = "manual_reviewed"
) -> tuple[StoredNutrient, ...]:
    registry = {
        row.code: row.unit for row in (await connection.execute(sa.select(nutrients))).all()
    }
    result = []
    for item in sorted(record.nutrients, key=lambda item: item.code):
        if item.code not in registry:
            raise ValueError("Unknown nutrient code; register its definition first")
        unit = registry[item.code]
        # Validate dimensions even for explicit unknown values.
        convert_unit(Decimal(0), item.unit, unit)
        result.append(
            StoredNutrient(
                code=item.code,
                unit=unit,
                amount_scaled=(
                    None
                    if item.amount is None
                    else per_100g_scaled(item.amount, item.unit, unit, record.basis_grams)
                ),
                source_amount=None if item.amount is None else format(item.amount, "f"),
                source_unit=item.unit,
                quality=quality,
                note=item.note,
            )
        )
    return tuple(result)


async def publish_reviewed_food(
    connection: AsyncConnection,
    record: ReviewedFoodInput,
    *,
    food_id: int | None = None,
    provenance: SourceProvenance | None = None,
) -> FoodSnapshot:
    """Publish a reviewed record atomically inside a Store.write() transaction.

    This is a trusted application boundary, not an AI tool. Telegram review/approval
    belongs in application actions. A catalog portion never authorizes a meal estimate.
    """
    normalized = await normalize_food(
        connection, record, quality="source_reported" if provenance else "manual_reviewed"
    )
    canonical = record.model_dump(mode="json")
    canonical["nutrients"] = [item.model_dump(mode="json") for item in normalized]
    canonical["portions"] = sorted(canonical["portions"], key=lambda item: item["label"])
    if provenance:
        canonical["provenance"] = provenance.model_dump(mode="json", exclude={"fetched_at"})
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    now = time.time()
    version_number = 1
    if food_id is None:
        food_id = (
            await connection.execute(
                sa.insert(foods)
                .values(preparation=record.preparation, created_at=now)
                .returning(foods.c.id)
            )
        ).scalar_one()
    else:
        preparation = await connection.scalar(
            sa.select(foods.c.preparation).where(foods.c.id == food_id)
        )
        if preparation is None:
            raise ValueError("Food does not exist")
        if preparation != record.preparation:
            raise ValueError("A different preparation requires a separate food")
        latest = (
            (
                await connection.execute(
                    sa.select(food_versions)
                    .where(food_versions.c.food_id == food_id, food_versions.c.sealed.is_(True))
                    .order_by(food_versions.c.version_number.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if latest:
            if latest["content_sha256"] == digest:
                return await get_food_version(connection, latest["id"])
            version_number = latest["version_number"] + 1
    assert food_id is not None
    version_id = (
        await connection.execute(
            sa.insert(food_versions)
            .values(
                food_id=food_id,
                version_number=version_number,
                name=record.name,
                brand=record.brand,
                source_kind=provenance.source_kind if provenance else "manual_reviewed",
                source_external_id=provenance.external_id if provenance else None,
                source_fetched_at=provenance.fetched_at if provenance else None,
                source_published_date=provenance.published_date if provenance else None,
                source_adapter_version=provenance.adapter_version if provenance else None,
                source_metadata=(
                    {"data_type": provenance.data_type, "warnings": list(provenance.warnings)}
                    if provenance
                    else None
                ),
                source_reference=record.source_reference,
                source_url=record.source_url,
                source_license=record.source_license,
                source_basis_milligrams=grams_to_milligrams(record.basis_grams),
                reviewed_at=now,
                calculation_version=CALCULATION_VERSION,
                content_sha256=digest,
                sealed=False,
            )
            .returning(food_versions.c.id)
        )
    ).scalar_one()
    for item in normalized:
        values = item.model_dump(exclude={"code", "unit"})
        await connection.execute(
            sa.insert(food_nutrients).values(
                food_version_id=version_id, nutrient_code=item.code, **values
            )
        )
    for portion in record.portions:
        await connection.execute(
            sa.insert(food_portions).values(
                food_version_id=version_id,
                edible_milligrams=grams_to_milligrams(portion.grams),
                **portion.model_dump(exclude={"grams"}),
            )
        )
    await connection.execute(
        sa.update(food_versions).where(food_versions.c.id == version_id).values(sealed=True)
    )
    return await get_food_version(connection, version_id)


async def get_food_version(connection: AsyncConnection, version_id: int) -> FoodSnapshot:
    version = (
        (
            await connection.execute(
                sa.select(food_versions, foods.c.preparation)
                .join(foods)
                .where(food_versions.c.id == version_id, food_versions.c.sealed.is_(True))
            )
        )
        .mappings()
        .one_or_none()
    )
    if version is None:
        raise ValueError("Food version does not exist")
    values = (
        (
            await connection.execute(
                sa.select(food_nutrients, nutrients.c.unit)
                .join(nutrients)
                .where(food_nutrients.c.food_version_id == version_id)
                .order_by(food_nutrients.c.nutrient_code)
            )
        )
        .mappings()
        .all()
    )
    portions = (
        (
            await connection.execute(
                sa.select(food_portions)
                .where(food_portions.c.food_version_id == version_id)
                .order_by(food_portions.c.label)
            )
        )
        .mappings()
        .all()
    )
    record = ReviewedFoodInput.model_validate(
        {
            "name": version["name"],
            "brand": version["brand"],
            "preparation": version["preparation"],
            "source_reference": version["source_reference"],
            "source_url": version["source_url"],
            "source_license": version["source_license"],
            "basis_grams": milligrams_to_grams(version["source_basis_milligrams"]),
            "nutrients": [
                {
                    "code": row["nutrient_code"],
                    "amount": row["source_amount"],
                    "unit": row["source_unit"],
                    "note": row["note"],
                }
                for row in values
            ],
            "portions": [
                {
                    "label": row["label"],
                    "grams": milligrams_to_grams(row["edible_milligrams"]),
                    "original_measure": row["original_measure"],
                    "source": row["source"],
                    "is_estimate": row["is_estimate"],
                }
                for row in portions
            ],
        }
    )
    return FoodSnapshot(
        food_id=version["food_id"],
        version_id=version_id,
        version_number=version["version_number"],
        record=record,
        content_sha256=version["content_sha256"],
        source_kind=version["source_kind"],
        reviewed_at=version["reviewed_at"],
        calculation_version=version["calculation_version"],
        provenance=(
            SourceProvenance(
                source_kind=version["source_kind"],
                external_id=version["source_external_id"],
                fetched_at=version["source_fetched_at"],
                published_date=version["source_published_date"],
                adapter_version=version["source_adapter_version"],
                data_type=version["source_metadata"]["data_type"],
                warnings=tuple(version["source_metadata"]["warnings"]),
            )
            if version["source_external_id"] is not None
            else None
        ),
        nutrients=tuple(
            StoredNutrient.model_validate(
                {**dict(row), "code": row["nutrient_code"]},
                extra="ignore",
            )
            for row in values
        ),
        portions=tuple(StoredPortion.model_validate(dict(row), extra="ignore") for row in portions),
    )
