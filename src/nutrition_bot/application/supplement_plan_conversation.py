import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database import supplement_plans as plans
from nutrition_bot.adapters.database.schema_supplements import supplement_regimens
from nutrition_bot.adapters.database.supplements import SupplementError
from nutrition_bot.application.meal_conversation import MealReply
from nutrition_bot.domain.supplement_plans import CreatinePlan, protocol_version

HELP = (
    "Creatine plan setup (grams refer to creatine monohydrate):\n"
    "/supplement plan steady YYYY-MM-DD 5\n"
    "/supplement plan loading YYYY-MM-DD 7 5\n"
    "/supplement plan weight YYYY-MM-DD 7 5 80\n"
    "Loading fields: start date, 5–7 days, maintenance grams; weight adds starting kg. "
    "Steady daily intake is the simpler default; loading is optional.\n"
    "Each command creates a preview only. Approve it to activate.\n"
    "/supplement plans — current references and doses\n"
    "/supplement dose R1r1 1 taken (or skipped / unconfirmed)\n"
    "/supplement pause R1r1; /supplement resume R1r2; /supplement stop R1r3. "
    "Resume retains the original dates; stop is final. "
    "Mark Taken only for a dose you consumed and have not already logged separately."
)
GATE = (
    "\nBefore approving: this adult template does not establish personal suitability. "
    "If under 18, pregnant/breastfeeding, kidney/liver disease, medication or supplement "
    "interaction concerns, planned surgery, or clinician restrictions apply, choose Cancel "
    "and review with your clinician/pharmacist. Approval confirms none of these concerns apply. "
    "Creatine can affect blood creatinine results; tell the clinician interpreting those tests."
)


async def handle_plan_message(
    connection: AsyncConnection,
    text: str,
    *,
    action_key: str,
    reference: datetime,
    source_chat_id: int,
    source_message_id: int,
) -> MealReply | None:
    normalized = " ".join(text.split())
    if not re.match(r"^/supplements? (plan|plans|dose|pause|resume|stop)(?: |$)", normalized, re.I):
        return None
    try:
        if normalized.casefold() in {"/supplement plan", "/supplements plan"}:
            return MealReply(HELP, "supplement_plan_help")
        if normalized.casefold() in {"/supplement plans", "/supplements plans"}:
            ids: Sequence[int] = (
                (
                    await connection.execute(
                        sa.select(supplement_regimens.c.id)
                        .order_by(supplement_regimens.c.id.desc())
                        .limit(3)
                    )
                )
                .scalars()
                .all()
            )
            descriptions = [await plans.describe(connection, rid, reference) for rid in ids]
            return MealReply("\n\n".join(descriptions) if ids else HELP, "supplement_plans")
        match = re.fullmatch(
            r"/supplements? plan (steady|loading|weight) (\d{4}-\d{2}-\d{2}) "
            r"([0-9.]+)(?: ([0-9.]+))?(?: ([0-9.]+))?",
            normalized,
            re.I,
        )
        if match:
            mode = match[1].lower()
            if (
                (mode == "steady" and match[4] is not None)
                or (mode == "loading" and (match[4] is None or match[5] is not None))
                or (mode == "weight" and match[5] is None)
            ):
                raise SupplementError("Incorrect fields. Open /supplement plan.")
            grams = Decimal(match[3] if mode == "steady" else match[4]) * 1000
            weight = None if mode != "weight" else Decimal(match[5]) * 1000
            if grams != grams.to_integral_value() or (
                weight is not None and weight != weight.to_integral_value()
            ):
                raise SupplementError("Use at most three decimal places for grams or kilograms.")
            plan = CreatinePlan(
                start=date.fromisoformat(match[2]),
                timezone=str(reference.tzinfo),
                option={"steady": "steady", "loading": "fixed_loading", "weight": "weight_loading"}[
                    mode
                ],
                maintenance_mg=int(grams),
                loading_days=0 if mode == "steady" else int(match[3]),
                weight_grams=None if weight is None else int(weight),
                protocol_version=protocol_version(),
            )
            if plan.start < reference.date():
                raise SupplementError("Choose today or a future start date.")
            pid = await plans.propose(connection, plan, action_key)
            return MealReply(
                f"Review plan P{pid}\n" + plan.preview() + GATE,
                "supplement_plan_preview",
                buttons=("approve", "cancel"),
                supplement_plan_proposal_id=pid,
            )
        state = re.fullmatch(
            r"/supplements? (pause|resume|stop) R([1-9][0-9]*)r([1-9][0-9]*)", normalized, re.I
        )
        if state:
            await plans.change_state(
                connection,
                int(state[2]),
                int(state[3]),
                state[1].lower(),
                action_key,
                reference.date(),
            )
            return MealReply(
                await plans.describe(connection, int(state[2]), reference),
                "supplement_plan_state",
            )
        dose = re.fullmatch(
            r"/supplements? dose R([1-9][0-9]*)r([1-9][0-9]*) "
            r"([1-9][0-9]*) (taken|skipped|unconfirmed)",
            normalized,
            re.I,
        )
        if dose:
            await plans.mark_dose(
                connection,
                int(dose[1]),
                int(dose[2]),
                reference.date(),
                int(dose[3]),
                dose[4].lower(),
                action_key,
                reference,
                source_chat_id,
                source_message_id,
            )
            return MealReply(
                await plans.describe(connection, int(dose[1]), reference),
                "supplement_plan_dose",
            )
        return MealReply(HELP, "supplement_plan_help")
    except (ValueError, ValidationError, InvalidOperation) as exc:
        text = (
            str(exc)
            if isinstance(exc, SupplementError)
            else "Invalid plan fields. Open /supplement plan."
        )
        return MealReply(text + "\nNo supplement data changed.", "supplement_rejected")


async def handle_plan_callback(
    connection: AsyncConnection, action: str, pid: int, *, action_key: str, reference: datetime
) -> MealReply:
    try:
        plan = await plans.proposal(connection, pid)
        today = reference.astimezone(ZoneInfo(plan.timezone)).date()
        rid = await plans.resolve(connection, pid, action, action_key, today)
        return MealReply(
            "Plan cancelled."
            if rid is None
            else "Plan approved. No dose was logged.\n"
            + await plans.describe(connection, rid, today),
            "supplement_plan_result",
        )
    except SupplementError as exc:
        return MealReply(str(exc) + "\nNo supplement data changed.", "supplement_rejected")
