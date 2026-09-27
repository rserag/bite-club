from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database import supplement_plans
from nutrition_bot.adapters.database.schema_supplements import (
    nutrient_reference_sets as sets,
)
from nutrition_bot.adapters.database.schema_supplements import (
    nutrient_reference_values as refs,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intake_components as components,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intake_revisions as revisions,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_intakes as intakes,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_product_components as labels,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_product_versions as versions,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_products as products,
)
from nutrition_bot.adapters.database.schema_supplements import (
    supplement_substances as substances,
)
from nutrition_bot.adapters.database.supplement_reports import adherence, exposures
from nutrition_bot.application.supplement_report import Request, pages, parse, render
from nutrition_bot.domain.supplement_plans import CreatinePlan, protocol_version
from nutrition_bot.domain.supplement_reports import (
    Amount,
    Exposure,
    UpperLimit,
    check_limit,
    combine,
    converted,
    supplement_sum,
)
from tests.helpers import message
from tests.test_supplement_foundation import add_action
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import process

DAY = date(2023, 11, 14)
NOW = datetime(2023, 11, 14, 12, tzinfo=UTC)
M = 1_000_000


def exposure(amount=100 * M, **kwargs):
    return replace(
        Exposure(DAY, 1, 1, "magnesium_supp", "Magnesium", "magnesium", "mg", amount, "oxide"),
        **kwargs,
    )


@pytest.mark.parametrize(
    "source,target,expected",
    [("g", "mg", 1000 * M), ("mg", "ug", 1000 * M), ("IU", "ug", None), ("mg", "g", 1000)],
)
def test_only_exact_mass_conversions(source, target, expected):
    assert converted(M, source, target) == expected
    assert converted(None, source, target) is None


def test_unknowns_qualifiers_and_large_sums():
    result = supplement_sum([exposure(), exposure(None), exposure(comparison="less_than")], "mg")
    assert result == Amount(100 * M, 1, 3)
    assert result.partial
    assert supplement_sum([exposure(2**63 - 1), exposure(2**63 - 1)], "mg").known == 2 * (2**63 - 1)
    assert combine(Amount(None, 0, 0), Amount(0, 0, 0)).known is None
    assert combine(Amount(None, 0, 1), Amount(100 * M, 1, 1)).partial


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("supplement", "below_recorded"),
        ("food", "exceeds"),
        ("total", "exceeds"),
        ("fortified_and_supplement", "indeterminate"),
    ],
)
def test_upper_limit_scope_excludes_inapplicable_food(scope, expected):
    limit = UpperLimit("magnesium", "mg", 200 * M, scope)
    assert (
        check_limit(limit, Amount(300 * M, 1, 1), [exposure()], food_complete=True).status
        == expected
    )


def test_form_unknown_and_lower_bound_cannot_produce_clearance():
    limit = UpperLimit("magnesium", "mg", 200 * M, "supplement", "oxide")
    assert (
        check_limit(limit, Amount(0, 0, 0), [exposure(form=None)], food_complete=True).status
        == "indeterminate"
    )
    assert (
        check_limit(limit, Amount(0, 0, 0), [exposure(form="citrate")], food_complete=True).known
        == 0
    )
    assert (
        check_limit(
            limit, Amount(0, 0, 0), [exposure(250 * M, comparison="at_least")], food_complete=True
        ).status
        == "exceeds"
    )
    assert (
        check_limit(
            limit, Amount(0, 0, 0), [exposure(250 * M, comparison="less_than")], food_complete=True
        ).status
        == "indeterminate"
    )
    assert (
        check_limit(limit, Amount(0, 0, 0), [exposure(200 * M)], food_complete=True).status
        == "at_limit"
    )
    total = replace(limit, scope="total")
    assert (
        check_limit(total, Amount(300 * M, 1, 1), [exposure()], food_complete=True).status
        == "indeterminate"
    )


@pytest.mark.parametrize(
    "text",
    [
        "/supplements today tomorrow",
        "/supplements week 0001-01-01",
        "/supplements today page 0",
        "/supplements week page 1 page 2",
        "/supplements today reference 1",
        "/supplements today 9999-12-31",
        "/supplements references reference 1 adult",
    ],
)
def test_invalid_report_requests_rejected(text):
    with pytest.raises(ValueError):
        parse(text, DAY)


def test_parser_and_unicode_paging():
    assert parse('/supplement week 2023-11-13 reference 1 "adult group" page 2', DAY) == Request(
        date(2023, 11, 13), 7, 2, 1, "adult group"
    )
    assert parse("/supplement take 1 serving | alias", DAY) is None
    rows = [f"{i} " + "🧪" * 300 for i in range(30)]
    full = [pages(rows, "Report", "/supplements today", page) for page in range(1, 9)]
    assert all(len(text.encode("utf-16-le")) // 2 < 4096 for text in full)
    assert all(any(row in text for text in full) for row in rows)


async def report_text(service, store, number, command="/supplements today"):
    first = await process(service, store, message(number, command))
    text = first["payload"]["text"]
    while " More: " in text.splitlines()[-1]:
        command = text.splitlines()[-1].split(" More: ", 1)[1]
        number += 1
        result = await process(service, store, message(number, command))
        text += "\n" + result["payload"]["text"]
    return text


async def test_creatine_reports_actual_only_and_corrections(service, store, catalog):
    await process(service, store, message(1, "100g rice"))
    await process(service, store, message(2, "creatine 5 g"))
    text = await report_text(service, store, 10)
    assert "Creatine monohydrate: 5000 mg" in text
    assert "F 100 kcal" in text and "S none logged" in text
    assert "C 100 kcal" in text
    assert "Upper limits not assessed" in text
    assert "Food log complete: 0/1" in text
    await process(service, store, message(20, "/supplement edit S1r1 3 g"))
    text = await report_text(service, store, 30, "/supplements week")
    assert "Creatine monohydrate: 3000 mg" in text
    assert "Food log complete: 0/7" in text
    await process(service, store, message(40, "/supplement delete S1r2"))
    assert "No active-ingredient doses" in await report_text(service, store, 50)
    await process(service, store, message(60, "/supplement undo S1r3"))
    assert "Creatine monohydrate: 3000 mg" in await report_text(service, store, 70)


async def test_empty_reference_and_no_doses_do_not_make_food_zero(service, store):
    text = await report_text(service, store, 1)
    assert "F unknown" in text and "C unknown" in text
    assert "S none logged" in text
    text = await report_text(service, store, 10, "/supplements references")
    assert "No reviewed nutrient reference" in text
    text = await report_text(service, store, 20, "/supplements today reference 1 adult")
    assert "unavailable" in text


async def seed_product(connection, n, *, day=DAY, amount=100 * M, form="oxide", comparison="exact"):
    """Synthetic nutrient label fixture, no real dietary reference values."""
    await connection.execute(
        sa.dialects.sqlite.insert(substances)
        .values(
            code="magnesium_supp",
            name="Magnesium supplement",
            category="nutrient",
            canonical_unit="mg",
            nutrient_code="magnesium",
            definition="Synthetic",
            source_reference="Synthetic",
            reviewed_at=1.0,
        )
        .on_conflict_do_nothing()
    )
    pid = (
        await connection.execute(
            sa.insert(products).values(created_at=1.0).returning(products.c.id)
        )
    ).scalar_one()
    vid = (
        await connection.execute(
            sa.insert(versions)
            .values(
                product_id=pid,
                version_number=1,
                review_action_key=await add_action(connection, n),
                name=f"Synthetic {n}",
                form="tablet",
                serving_description="one tablet",
                source_kind="manual_label",
                source_reference="Synthetic",
                reviewed_at=1.0,
                content_sha256="1" * 64,
                sealed=False,
            )
            .returning(versions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(labels).values(
            product_version_id=vid,
            component_index=1,
            substance_code="magnesium_supp",
            printed_name="Magnesium",
            chemical_form=form,
            comparison=comparison,
            source_amount=None if comparison == "unknown" else "100",
            source_unit=None if comparison == "unknown" else "mg",
            amount_scaled=amount,
            conversion_version="mass-v1" if amount is not None else None,
        )
    )
    await connection.execute(sa.update(versions).where(versions.c.id == vid).values(sealed=True))
    await connection.execute(
        sa.update(products).where(products.c.id == pid).values(current_version_id=vid)
    )
    iid = (
        await connection.execute(
            sa.insert(intakes)
            .values(source_chat_id=101, source_message_id=n + 1, created_at=1.0)
            .returning(intakes.c.id)
        )
    ).scalar_one()
    rid = (
        await connection.execute(
            sa.insert(revisions)
            .values(
                intake_id=iid,
                revision_number=1,
                action_key=await add_action(connection, n + 1),
                local_date=day,
                consumed_at=1.0,
                timezone="UTC",
                status="taken",
                product_version_id=vid,
                servings_scaled=M,
                sealed=False,
                deleted=False,
                operation="create",
                created_at=1.0,
            )
            .returning(revisions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(components).values(
            intake_revision_id=rid,
            component_index=1,
            substance_code="magnesium_supp",
            amount_scaled=amount,
            value_origin="label_scaled" if amount is not None else "unknown",
        )
    )
    await connection.execute(sa.update(revisions).where(revisions.c.id == rid).values(sealed=True))
    await connection.execute(
        sa.update(intakes).where(intakes.c.id == iid).values(current_revision_id=rid)
    )
    return pid


async def seed_reference(connection, *, scope="supplement", form="", kind="UL"):
    rid = (
        await connection.execute(
            sa.insert(sets)
            .values(
                framework="US_DRI",
                version="synthetic-v1",
                name="Synthetic ONLY",
                source_url="https://example.test/synthetic",
                reviewed_at=1.0,
                content_sha256="0" * 64,
                sealed=False,
            )
            .returning(sets.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(refs).values(
            reference_set_id=rid,
            reference_group="synthetic-adult",
            nutrient_code="magnesium",
            kind=kind,
            applicability=scope,
            chemical_form=form,
            amount_scaled=150 * M,
            unit="mg",
            note="Synthetic fixture, not a recommendation",
        )
    )
    await connection.execute(sa.update(sets).where(sets.c.id == rid).values(sealed=True))
    return rid


async def collect_render(connection, request, reference=NOW):
    result = await render(connection, request, reference)
    import re

    count = int(re.search(r"Page 1/(\d+)", result)[1])
    return "\n".join(
        [result]
        + [
            await render(connection, replace(request, page=p), reference)
            for p in range(2, count + 1)
        ]
    )


async def test_snapshot_sources_duplicates_and_daily_ul_not_weekly_average(store):
    async with store.write() as c:
        await seed_product(c, 100)
        await seed_product(c, 200)
        await seed_product(
            c, 300, day=DAY - timedelta(days=1), amount=None, comparison="unknown", form=None
        )
        rid = await seed_reference(c)
        rows = await exposures(c, DAY - timedelta(days=6), DAY)
        assert len(rows) == 3
        text = await collect_render(c, Request(DAY, 7, reference_id=rid, group="synthetic-adult"))
    assert "S 200 mg known (2/3)" in text
    assert "C 200 mg known" in text
    assert "occurs in 2 logged products" in text
    assert "200 / 150 mg; recorded amount exceeds UL" in text
    assert "comparison indeterminate" in text
    assert "synthetic-v1" in text
    assert "Weekly averaging never hides a daily exceedance" in text


@pytest.mark.parametrize("kind", ["RDA", "AI", "limit"])
async def test_reference_types_do_not_become_upper_limits(store, kind):
    async with store.write() as c:
        await seed_product(c, 100, amount=500 * M)
        rid = await seed_reference(c, kind=kind)
        text = await collect_render(c, Request(DAY, 1, reference_id=rid, group="synthetic-adult"))
    assert "No UL values" in text
    assert "exceeds UL" not in text


async def test_plan_history_and_linked_dose_deletion(store, monkeypatch):
    # Freeze mutation times to verify past pauses do not erase earlier active days.
    clock = NOW.timestamp()
    monkeypatch.setattr("time.time", lambda: clock)
    plan = CreatinePlan(
        start=DAY,
        timezone="UTC",
        option="steady",
        maintenance_mg=5000,
        protocol_version=protocol_version(),
    )
    async with store.write() as c:
        pid = await supplement_plans.propose(c, plan, await add_action(c, 100))
        await supplement_plans.resolve(c, pid, "approve", await add_action(c, 101), DAY)
        empty = await adherence(c, DAY, DAY, NOW)
        assert "0 taken" in empty[0] and "1 unconfirmed" in empty[0]
        await supplement_plans.mark_dose(
            c, 1, 1, DAY, 1, "taken", await add_action(c, 102), NOW, 101, 102
        )
        assert "1 taken (1 at planned amount)" in (await adherence(c, DAY, DAY, NOW))[0]
        from nutrition_bot.adapters.database.supplements import (
            get_supplement_intake,
            revise_supplement_intake,
        )

        intake = await get_supplement_intake(c, 1)
        await revise_supplement_intake(
            c, 1, intake.revision_id, action_key=await add_action(c, 103), amount_scaled=3000 * M
        )
        assert "1 taken (0 at planned amount)" in (await adherence(c, DAY, DAY, NOW))[0]
        intake = await get_supplement_intake(c, 1)
        await revise_supplement_intake(
            c, 1, intake.revision_id, action_key=await add_action(c, 104), delete=True
        )
        assert "1 unconfirmed" in (await adherence(c, DAY, DAY, NOW))[0]
        clock += timedelta(days=1).total_seconds()
        await supplement_plans.change_state(
            c, 1, 1, "pause", await add_action(c, 105), DAY + timedelta(days=1)
        )
        rows = await adherence(c, DAY, DAY + timedelta(days=2), NOW + timedelta(days=2))
        assert "2 scheduled" in rows[0]  # Start day and part-active pause day, not next day.


async def test_duplicate_product_identity_not_dose_count_and_frozen_label(store):
    async with store.write() as c:
        pid = await seed_product(c, 100)
        # A newer product version must not change the form or amount of prior intake.
        old = (
            (await c.execute(sa.select(versions).where(versions.c.product_id == pid)))
            .mappings()
            .one()
        )
        values = dict(old)
        values.pop("id")
        values.update(
            version_number=2,
            previous_version_id=old["id"],
            sealed=False,
            review_action_key=await add_action(c, 200),
            content_sha256="2" * 64,
        )
        vid = (
            await c.execute(sa.insert(versions).values(**values).returning(versions.c.id))
        ).scalar_one()
        await c.execute(
            sa.insert(labels).values(
                product_version_id=vid,
                component_index=1,
                substance_code="magnesium_supp",
                printed_name="Magnesium",
                chemical_form="citrate",
                comparison="exact",
                source_amount="999",
                source_unit="mg",
                amount_scaled=999 * M,
                conversion_version="mass-v1",
            )
        )
        await c.execute(sa.update(versions).where(versions.c.id == vid).values(sealed=True))
        await c.execute(
            sa.update(products).where(products.c.id == pid).values(current_version_id=vid)
        )
        row = (await exposures(c, DAY, DAY))[0]
        assert row.amount == 100 * M and row.form == "oxide"
        text = await collect_render(c, Request(DAY, 1))
        assert "S 100 mg" in text and "occurs in" not in text


async def test_complete_food_empty_day_is_separate_from_unlogged_days(service, store):
    await process(service, store, message(1, "/today complete"))
    text = await report_text(service, store, 10)
    assert "F 0 kcal" in text and "C 0 kcal" in text
    weekly = await report_text(service, store, 20, "/supplements week")
    assert "F 0 kcal known" in weekly
    assert "Food log complete: 1/7" in weekly


async def test_amount_unknown_and_form_unknown_stay_indeterminate_in_rendered_limits(store):
    async with store.write() as c:
        await seed_product(c, 100, amount=500 * M, form=None)
        rid = await seed_reference(c, form="oxide")
        text = await collect_render(c, Request(DAY, 1, reference_id=rid, group="synthetic-adult"))
    assert "S 500 mg" in text  # Elemental total is known, form-specific eligibility is not.
    assert "comparison indeterminate" in text and "exceeds UL" not in text


async def test_future_plan_and_plan_timezone_boundaries(store, monkeypatch):
    # Observation is still the previous calendar day in Los Angeles.
    observed = datetime(2023, 11, 15, 1, tzinfo=UTC)
    monkeypatch.setattr("time.time", lambda: NOW.timestamp())
    plan = CreatinePlan(
        start=date(2023, 11, 15),
        timezone="America/Los_Angeles",
        option="steady",
        maintenance_mg=5000,
        protocol_version=protocol_version(),
    )
    async with store.write() as c:
        pid = await supplement_plans.propose(c, plan, await add_action(c, 100))
        await supplement_plans.resolve(c, pid, "approve", await add_action(c, 101), DAY)
        assert await adherence(c, DAY, date(2023, 11, 15), observed) == []
        after_midnight = observed + timedelta(hours=8)
        result = await adherence(c, DAY, date(2023, 11, 15), after_midnight)
        assert "1 scheduled; 0 taken" in result[0]
