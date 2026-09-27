import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.bjj_plans import (
    BJJTemplate,
    ScheduleRule,
    add_plan_event,
    clear_template,
    current_schedule,
    current_template,
    day_plan,
    replace_schedule,
    save_template,
)
from nutrition_bot.adapters.database.training import (
    BJJDetails,
    GymExercise,
    TrainingError,
    TrainingSnapshot,
    WorkoutSet,
    create_training,
    get_training,
    inferred_rpe,
    recent_training,
    revise_bjj_details,
    revise_gym_details,
    revise_training,
    undo_training,
)
from nutrition_bot.application.meal_conversation import MealReply

REFERENCE = r"T([1-9][0-9]*)(?:r([1-9][0-9]*))?"
DURATION = re.compile(r"\b([0-9]{1,3})\s*(?:min|mins|minutes)\b", re.IGNORECASE)
RPE = re.compile(r"\b(?:rpe|effort)\s*=?\s*([0-9]+(?:\.[0-9])?)\b", re.IGNORECASE)
SET_RPE = re.compile(r"@\s*([0-9]+(?:\.[0-9])?)\b", re.IGNORECASE)


def _rpe(value: str) -> int:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise TrainingError("Session RPE must be a number from 0 to 10.") from None
    tenths = number * 10
    if tenths != tenths.to_integral_value() or not 0 <= tenths <= 100:
        raise TrainingError("Session RPE must be 0–10 with at most one decimal place.")
    return int(tenths)


def _parse(kind: str, body: str) -> tuple[int, str | None, str | None, int | None]:
    duration_match = DURATION.search(body)
    if duration_match is None:
        raise TrainingError("Include duration, for example 60 min.")
    duration = int(duration_match[1])
    if not 1 <= duration <= 600:
        raise TrainingError("Training duration must be between 1 and 600 minutes.")
    rpe_match = RPE.search(body)
    rpe = _rpe(rpe_match[1]) if rpe_match else None
    cleaned = DURATION.sub(" ", body)
    cleaned = RPE.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:for|at|intensity)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.replace(",", " ").split()).strip(" -/")
    if kind == "gym":
        if not cleaned:
            raise TrainingError("Include the gym focus, for example chest and triceps.")
        if len(cleaned) > 120:
            raise TrainingError("Keep the gym focus under 120 characters.")
        return duration, cleaned, None, rpe
    intensity_matches = re.findall(r"\b(easy|medium|hard)\b", cleaned, re.IGNORECASE)
    if len(intensity_matches) > 1:
        raise TrainingError("Choose one BJJ intensity: easy, medium or hard.")
    intensity = intensity_matches[0].casefold() if intensity_matches else None
    remaining = re.sub(r"\b(?:easy|medium|hard)\b", " ", cleaned, flags=re.IGNORECASE)
    if remaining.strip():
        raise TrainingError("Use duration, optional easy/medium/hard, and optional session RPE.")
    return duration, None, intensity, rpe


def _display_rpe(snapshot: TrainingSnapshot) -> str:
    value = Decimal(snapshot.session_rpe_tenths) / 10
    shown = str(value.normalize())
    labels = {
        "reported": "reported",
        "personal_history": "estimated from at least 3 comparable reported sessions",
        "intensity_map": f"estimated from {snapshot.intensity} intensity",
        "system_default": "estimated from the visible default",
    }
    return f"Session RPE: {shown}/10 · {labels[snapshot.rpe_source]}"


def _load_grams(value: str) -> int:
    try:
        grams = Decimal(value) * 1000
    except InvalidOperation:
        raise TrainingError("Set load must be a number in kg.") from None
    if grams != grams.to_integral_value() or not 1 <= grams <= 1_000_000:
        raise TrainingError("Set load must be 0.001–1000 kg with at most three decimals.")
    return int(grams)


def _parse_set(value: str, set_index: int) -> WorkoutSet:
    body = " ".join(value.casefold().split())
    set_types = [name for name in ("warmup", "working") if re.search(rf"\b{name}\b", body)]
    if len(set_types) > 1:
        raise TrainingError("A set cannot be both warmup and working.")
    set_type = set_types[0] if set_types else None
    body = re.sub(r"\b(?:warmup|working)\b", " ", body)
    rpe_match = SET_RPE.search(body)
    set_rpe = _rpe(rpe_match[1]) if rpe_match else None
    body = SET_RPE.sub(" ", body)
    body = " ".join(body.split())
    reps: int | None = None
    load: int | None = None
    convention: str | None = None
    matched = re.fullmatch(r"([0-9]{1,4})\s*x\s*([0-9]+(?:\.[0-9]+)?)\s*kg", body)
    if matched:
        reps, load, convention = int(matched[1]), _load_grams(matched[2]), "external_kg"
    else:
        bodyweight = re.fullmatch(r"(?:(\d{1,4})\s*x\s*)?(?:bodyweight|bw)", body)
        reps_only = re.fullmatch(r"([0-9]{1,4})(?:\s*reps?)?", body)
        load_only = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*kg", body)
        if bodyweight:
            reps = int(bodyweight[1]) if bodyweight[1] else None
            convention = "bodyweight"
        elif reps_only:
            reps = int(reps_only[1])
        elif load_only:
            load, convention = _load_grams(load_only[1]), "external_kg"
        elif body:
            raise TrainingError(
                "Use sets like '10x60kg @8 warmup', '8 reps', '60kg', '10xbodyweight', or '@8'."
            )
    if reps is not None and not 1 <= reps <= 1000:
        raise TrainingError("Set reps must be between 1 and 1000.")
    if reps is None and convention is None and set_rpe is None:
        raise TrainingError("Each set needs reps, load/bodyweight, or set RPE.")
    return WorkoutSet(set_index, reps, load, convention, set_rpe, set_type)


def _parse_details(body: str) -> tuple[GymExercise, ...]:
    if body.strip().casefold() == "clear":
        return ()
    parts = [part.strip() for part in body.split(";")]
    if not parts or any(not part for part in parts) or len(parts) > 10:
        raise TrainingError("Provide 1–10 exercise blocks separated by semicolons.")
    exercises: list[GymExercise] = []
    for occurrence_index, part in enumerate(parts, 1):
        if part.count(":") != 1:
            raise TrainingError("Use 'exercise: set, set' for every exercise block.")
        name, set_text = (item.strip() for item in part.split(":", 1))
        if not 1 <= len(name) <= 120:
            raise TrainingError("Exercise names must contain 1–120 characters.")
        sets = [item.strip() for item in set_text.split(",")]
        if not sets or any(not item for item in sets) or len(sets) > 20:
            raise TrainingError("Provide 1–20 sets per exercise, separated by commas.")
        exercises.append(
            GymExercise(
                occurrence_index,
                " ".join(name.split()),
                tuple(_parse_set(item, index) for index, item in enumerate(sets, 1)),
            )
        )
    return tuple(exercises)


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in body.split():
        if token.count("=") != 1:
            raise TrainingError("Use named values such as warmup=10 or sparring=3x5.")
        key, value = token.casefold().split("=", 1)
        if key in fields:
            raise TrainingError(f"Provide {key} once.")
        fields[key] = value
    return fields


def _positive(value: str, label: str, maximum: int = 600) -> int:
    if not value.isdigit() or not 1 <= int(value) <= maximum:
        raise TrainingError(f"{label} must be a whole number from 1 to {maximum}.")
    return int(value)


def _rounds(value: str, label: str) -> tuple[int, int]:
    matched = re.fullmatch(r"([0-9]{1,3})x([0-9]{1,2})", value)
    if matched is None:
        raise TrainingError(f"{label} must look like 3x5: rounds × minutes.")
    return _positive(matched[1], f"{label} rounds", 100), _positive(
        matched[2], f"{label} round minutes", 60
    )


def _range(value: str, label: str, maximum: int) -> tuple[int, int]:
    matched = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", value)
    if matched is None:
        raise TrainingError(f"{label} must be a number or range such as 10-15.")
    low = _positive(matched[1], label, maximum)
    high = _positive(matched[2] or matched[1], label, maximum)
    if low > high:
        raise TrainingError(f"{label} range must go from lower to higher.")
    return low, high


def _parse_bjj_details(body: str) -> BJJDetails:
    if body.strip().casefold() == "clear":
        return BJJDetails()
    fields = _fields(body)
    allowed = {"warmup", "technical", "positional", "sparring"}
    if not fields or set(fields) - allowed:
        raise TrainingError("Use warmup, technical, positional and/or sparring named values.")
    positional = (
        _rounds(fields["positional"], "positional") if "positional" in fields else (None, None)
    )
    sparring = _rounds(fields["sparring"], "sparring") if "sparring" in fields else (None, None)
    return BJJDetails(
        warmup_minutes=_positive(fields["warmup"], "warmup") if "warmup" in fields else None,
        technical_minutes=_positive(fields["technical"], "technical")
        if "technical" in fields
        else None,
        positional_rounds=positional[0],
        positional_round_minutes=positional[1],
        sparring_rounds=sparring[0],
        sparring_round_minutes=sparring[1],
    )


def _parse_template(body: str) -> BJJTemplate:
    fields = _fields(body)
    allowed = {"duration", "warmup", "technical", "positional", "sparring"}
    if "duration" not in fields or set(fields) - allowed:
        raise TrainingError(
            "Set duration and optional warmup, technical, positional and sparring ranges."
        )
    warmup = _range(fields["warmup"], "warmup", 600) if "warmup" in fields else (None, None)
    technical = (
        _range(fields["technical"], "technical", 600) if "technical" in fields else (None, None)
    )
    positional_range: tuple[int | None, int | None] = (None, None)
    positional_minutes = None
    if "positional" in fields:
        rounds, separator, minutes = fields["positional"].partition("x")
        if not separator:
            raise TrainingError("positional must look like 1-3x2.")
        positional_range = _range(rounds, "positional rounds", 100)
        positional_minutes = _positive(minutes, "positional round minutes", 60)
    sparring_range: tuple[int | None, int | None] = (None, None)
    sparring_minutes = None
    if "sparring" in fields:
        rounds, separator, minutes = fields["sparring"].partition("x")
        if not separator:
            raise TrainingError("sparring must look like 1-3x5.")
        sparring_range = _range(rounds, "sparring rounds", 100)
        sparring_minutes = _positive(minutes, "sparring round minutes", 60)
    return BJJTemplate(
        0,
        _positive(fields["duration"], "duration"),
        warmup[0],
        warmup[1],
        technical[0],
        technical[1],
        positional_range[0],
        positional_range[1],
        positional_minutes,
        sparring_range[0],
        sparring_range[1],
        sparring_minutes,
    )


def _template_text(template: BJJTemplate) -> str:
    def span(low: int | None, high: int | None, unit: str) -> str:
        return "unknown" if low is None else f"{low}{f'–{high}' if high != low else ''} {unit}"

    return "\n".join(
        (
            f"Usual BJJ template v{template.revision_number} · estimates only",
            f"Typical total: {template.typical_duration_minutes} min",
            f"Warm-up: {span(template.warmup_min_minutes, template.warmup_max_minutes, 'min')}",
            "Technical: "
            + span(template.technical_min_minutes, template.technical_max_minutes, "min"),
            "Positional: "
            + (
                "unknown"
                if template.positional_min_rounds is None
                else span(
                    template.positional_min_rounds,
                    template.positional_max_rounds,
                    "rounds",
                )
                + f" × {template.positional_round_minutes} min"
            ),
            "Sparring: "
            + (
                "unknown"
                if template.sparring_min_rounds is None
                else span(
                    template.sparring_min_rounds,
                    template.sparring_max_rounds,
                    "rounds",
                )
                + f" × {template.sparring_round_minutes} min"
            ),
            "These ranges are context only. They never fill a completed session.",
        )
    )


def _set_text(item: WorkoutSet) -> str:
    parts: list[str] = []
    if item.reps is not None and item.load_convention == "external_kg":
        parts.append(f"{item.reps}×{Decimal(item.load_grams or 0) / 1000:g} kg")
    elif item.reps is not None and item.load_convention == "bodyweight":
        parts.append(f"{item.reps}×bodyweight")
    elif item.reps is not None:
        parts.append(f"{item.reps} reps")
    elif item.load_convention == "external_kg":
        parts.append(f"{Decimal(item.load_grams or 0) / 1000:g} kg")
    elif item.load_convention == "bodyweight":
        parts.append("bodyweight")
    if item.set_rpe_tenths is not None:
        parts.append(f"set RPE {Decimal(item.set_rpe_tenths) / 10:g}")
    if item.set_type is not None:
        parts.append(item.set_type)
    return " · ".join(parts)


def receipt(snapshot: TrainingSnapshot, lead: str = "Saved training") -> MealReply:
    title = "Gym" if snapshot.kind == "gym" else "BJJ"
    summary_detail = (
        snapshot.focus if snapshot.kind == "gym" else snapshot.intensity or "intensity unknown"
    )
    lines = [
        f"{lead} T{snapshot.id}r{snapshot.revision_number} · {snapshot.local_date} · {title}",
        f"{snapshot.duration_minutes} min · {summary_detail}",
        _display_rpe(snapshot),
        "Approximate session load: "
        f"{snapshot.duration_minutes * snapshot.session_rpe_tenths // 10} AU.",
    ]
    if snapshot.deleted:
        lines.append("Deleted from current training history; revisions are retained.")
    else:
        if snapshot.exercises:
            lines.append("Reported gym details (set RPE is separate from session RPE):")
            lines.extend(
                f"{exercise.occurrence_index}. {exercise.name}: "
                + ", ".join(_set_text(item) for item in exercise.sets)
                for exercise in snapshot.exercises
            )
        elif snapshot.bjj_details is not None and any(
            value is not None for value in snapshot.bjj_details.__dict__.values()
        ):
            reported = snapshot.bjj_details
            lines.append("Reported BJJ stages (included within total session time):")
            if reported.warmup_minutes is not None:
                lines.append(f"Warm-up: {reported.warmup_minutes} min")
            if reported.technical_minutes is not None:
                lines.append(f"Technical: {reported.technical_minutes} min")
            if reported.positional_rounds is not None:
                lines.append(
                    f"Positional: {reported.positional_rounds} × "
                    f"{reported.positional_round_minutes} min"
                )
            if reported.sparring_rounds is not None:
                lines.append(
                    f"Sparring: {reported.sparring_rounds} × {reported.sparring_round_minutes} min"
                )
            lines.append("Stage minutes are not added to the reported total.")
        else:
            lines.append("No exercises, sets or BJJ rounds were inferred.")
        lines.append(
            f"Correct with /{snapshot.kind} edit T{snapshot.id}r{snapshot.revision_number} "
            + (
                "chest and triceps 40 min rpe 7"
                if snapshot.kind == "gym"
                else "75 min medium rpe 6"
            )
        )
    return MealReply(
        "\n".join(lines),
        "training_receipt",
        buttons=("edit", "delete", "undo") if not snapshot.deleted else ("undo",),
        training_session_id=snapshot.id,
        training_revision_id=snapshot.revision_id,
    )


WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def _clock(value: str | None) -> int | None:
    if value is None:
        return None
    matched = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", value)
    if matched is None:
        raise TrainingError("Start time must use 24-hour HH:MM, for example 18:30.")
    return int(matched[1]) * 60 + int(matched[2])


def _clock_text(value: int | None) -> str:
    return "time unknown" if value is None else f"{value // 60:02d}:{value % 60:02d}"


async def _handle_bjj_template(
    connection: AsyncConnection, stripped: str, *, action_key: str
) -> MealReply | None:
    matched = re.fullmatch(r"/bjj\s+template(?:\s+(.*))?", stripped, re.IGNORECASE)
    if matched is None:
        return None
    body = (matched[1] or "").strip()
    if not body:
        template = await current_template(connection)
        return MealReply(
            _template_text(template)
            if template
            else "No usual BJJ template is set. Use /bjj template set duration=60 …",
            "bjj_template",
        )
    if body.casefold() == "clear":
        await clear_template(connection, action_key=action_key)
        return MealReply(
            "Cleared the usual BJJ template. Existing sessions are unchanged.", "bjj_template"
        )
    if not body.casefold().startswith("set "):
        raise TrainingError("Use /bjj template, /bjj template set …, or /bjj template clear.")
    saved = await save_template(connection, _parse_template(body[4:]), action_key=action_key)
    return MealReply(_template_text(saved), "bjj_template")


async def _handle_plan(
    connection: AsyncConnection,
    stripped: str,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply | None:
    if not stripped.casefold().startswith("/plan"):
        return None
    if stripped.casefold() in {"/plan", "/plan week"}:
        rows = [
            await day_plan(connection, reference.date() + timedelta(days=index))
            for index in range(7)
        ]
        lines = ["Training plan · plans and completed logs are separate:"]
        for row in rows:
            planned = row.state
            if row.activities:
                planned += " " + " / ".join(
                    f"{activity.kind.upper()} {activity.duration_minutes} min at "
                    f"{_clock_text(activity.start_minute)}"
                    for activity in row.activities
                    if activity.kind is not None
                )
            completed = ", ".join(kind.upper() for kind in row.completed) or "none"
            lines.append(f"{row.local_date} · {planned} ({row.source}) · completed: {completed}")
        lines.append("Unknown means no plan was recorded; it does not mean rest.")
        return MealReply("\n".join(lines), "training_plan")
    weekly = re.fullmatch(
        r"/plan\s+weekly\s+(gym|bjj)\s+(.+?)\s+([0-9]{1,3})\s*min(?:\s+at\s+(\S+))?",
        stripped,
        re.IGNORECASE,
    )
    if weekly:
        kind = weekly[1].casefold()
        weekday_tokens = re.split(r"[\s,]+", weekly[2].strip().casefold())
        if not weekday_tokens or any(token not in WEEKDAYS for token in weekday_tokens):
            raise TrainingError("Use weekday names such as tue thu.")
        duration = _positive(weekly[3], "duration")
        start = _clock(weekly[4])
        existing = tuple(rule for rule in await current_schedule(connection) if rule.kind != kind)
        rules = existing + tuple(
            ScheduleRule(day, kind, duration, start)
            for day in sorted({WEEKDAYS[token] for token in weekday_tokens})
        )
        await replace_schedule(
            connection, rules, timezone=str(reference.tzinfo), action_key=action_key
        )
        return MealReply(
            f"Saved weekly {kind.upper()} plan for {', '.join(weekday_tokens)} · "
            f"{duration} min · {_clock_text(start)}.\n"
            "This schedules prompts/context only; it never records attendance.",
            "training_plan",
        )
    dated = re.fullmatch(
        r"/plan\s+(\d{4}-\d{2}-\d{2})\s+(rest|cancel|clear|gym|bjj)(?:\s+([0-9]{1,3})\s*min)?(?:\s+at\s+(\S+))?",
        stripped,
        re.IGNORECASE,
    )
    if dated is None:
        raise TrainingError(
            "Use /plan week, /plan weekly bjj tue thu 60 min, or /plan 2026-10-01 rest."
        )
    try:
        day = date.fromisoformat(dated[1])
    except ValueError:
        raise TrainingError("Use a real date in YYYY-MM-DD format.") from None
    choice = dated[2].casefold()
    event_kind: str | None
    event_duration: int | None
    event_start: int | None
    if choice in {"gym", "bjj"}:
        if dated[3] is None:
            raise TrainingError("A planned session needs a duration, for example 60 min.")
        state, event_kind, event_duration, event_start = (
            "planned",
            choice,
            _positive(dated[3], "duration"),
            _clock(dated[4]),
        )
    else:
        if dated[3] is not None or dated[4] is not None:
            raise TrainingError("Rest, cancel and clear do not take duration or start time.")
        state = "cancelled" if choice == "cancel" else choice
        event_kind, event_duration, event_start = None, None, None
    await add_plan_event(
        connection,
        action_key=action_key,
        local_date=day,
        state=state,
        timezone=str(reference.tzinfo),
        kind=event_kind,
        duration_minutes=event_duration,
        start_minute=event_start,
    )
    return MealReply(
        f"Saved {day} as {state}"
        + (
            f" {event_kind.upper()} {event_duration} min at {_clock_text(event_start)}"
            if event_kind
            else ""
        )
        + ". No completed session was created.",
        "training_plan",
    )


async def _handle_training_message(
    connection: AsyncConnection,
    text: str,
    *,
    action_key: str,
    reference: datetime,
    source_chat_id: int,
    source_message_id: int,
) -> MealReply | None:
    stripped = text.strip()
    lowered = stripped.casefold()
    template_reply = await _handle_bjj_template(connection, stripped, action_key=action_key)
    if template_reply is not None:
        return template_reply
    plan_reply = await _handle_plan(
        connection, stripped, action_key=action_key, reference=reference
    )
    if plan_reply is not None:
        return plan_reply
    if lowered in {"/training", "/training help", "/gym", "/bjj"}:
        return MealReply(
            "Quick logs:\n"
            "gym chest and triceps 40 min\n"
            "BJJ 75 min medium intensity\n"
            "Add 'rpe 7' to report session effort. Without it, the receipt labels the estimate.\n"
            "Add optional sets later with /gym details T1r1 exercise: 10x60kg @8.\n"
            "Add reported BJJ stages with /bjj details T2r1 warmup=10 sparring=3x5.\n"
            "Use /bjj template for usual estimates and /plan week for planned/completed state.\n"
            "Use /load or /load short for the latest completed week's workload.\n"
            "Use /training history to reopen recent sessions.",
            "training_help",
        )
    if lowered == "/training history":
        rows = await recent_training(connection)
        if not rows:
            return MealReply("No training sessions yet.", "training_history")
        return MealReply(
            "Recent training:\n"
            + "\n".join(
                f"T{row.id}r{row.revision_number} · {row.local_date} · {row.kind.upper()} · "
                f"{row.duration_minutes} min" + (" · deleted" if row.deleted else "")
                for row in rows
            ),
            "training_history",
        )
    details = re.fullmatch(rf"/gym\s+details\s+{REFERENCE}\s+(.+)", stripped, re.IGNORECASE)
    if details:
        current = await get_training(connection, int(details[1]))
        if details[2] is None or current.revision_number != int(details[2]):
            raise TrainingError("Use the current T…r… reference from /training history.")
        return receipt(
            await revise_gym_details(
                connection,
                current.id,
                current.revision_id,
                _parse_details(details[3]),
                action_key=action_key,
            ),
            "Updated gym details",
        )
    bjj_details = re.fullmatch(rf"/bjj\s+details\s+{REFERENCE}\s+(.+)", stripped, re.IGNORECASE)
    if bjj_details:
        current = await get_training(connection, int(bjj_details[1]))
        if bjj_details[2] is None or current.revision_number != int(bjj_details[2]):
            raise TrainingError("Use the current T…r… reference from /training history.")
        return receipt(
            await revise_bjj_details(
                connection,
                current.id,
                current.revision_id,
                _parse_bjj_details(bjj_details[3]),
                action_key=action_key,
            ),
            "Updated BJJ details",
        )
    mutation = re.fullmatch(rf"/training\s+(delete|undo)\s+{REFERENCE}", stripped, re.IGNORECASE)
    if mutation:
        current = await get_training(connection, int(mutation[2]))
        if mutation[3] is None or current.revision_number != int(mutation[3]):
            raise TrainingError("Use the current T…r… reference from /training history.")
        updated = (
            await revise_training(
                connection,
                current.id,
                current.revision_id,
                action_key=action_key,
                delete=True,
            )
            if mutation[1].casefold() == "delete"
            else await undo_training(
                connection, current.id, current.revision_id, action_key=action_key
            )
        )
        return receipt(updated, "Updated training")
    edit = re.fullmatch(rf"/(gym|bjj)\s+edit\s+{REFERENCE}\s+(.+)", stripped, re.IGNORECASE)
    if edit:
        kind = edit[1].casefold()
        current = await get_training(connection, int(edit[2]))
        if edit[3] is None or current.revision_number != int(edit[3]):
            raise TrainingError("Use the current T…r… reference from /training history.")
        if current.kind != kind:
            raise TrainingError("Use the session's original /gym or /bjj edit command.")
        duration, focus, intensity, rpe = _parse(kind, edit[4])
        source = None
        if rpe is None:
            rpe, source = await inferred_rpe(
                connection, kind=kind, focus=focus, intensity=intensity
            )
        return receipt(
            await revise_training(
                connection,
                current.id,
                current.revision_id,
                action_key=action_key,
                duration_minutes=duration,
                focus=focus,
                intensity=intensity,
                session_rpe_tenths=rpe,
                rpe_source=source,
            ),
            "Updated training",
        )
    matched = re.match(r"^/?(gym|bjj)\b\s*(.*)$", stripped, re.IGNORECASE)
    if matched is None:
        return None
    kind, body = matched[1].casefold(), matched[2]
    try:
        duration, focus, intensity, rpe = _parse(kind, body)
        return receipt(
            await create_training(
                connection,
                action_key=action_key,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                kind=kind,
                local_date=reference.date(),
                occurred_at=reference.timestamp(),
                timezone=str(reference.tzinfo),
                duration_minutes=duration,
                focus=focus,
                intensity=intensity,
                session_rpe_tenths=rpe,
            )
        )
    except TrainingError as exc:
        return MealReply(f"{exc}\nNo training session changed.", "training_rejected")


async def handle_training_message(
    connection: AsyncConnection,
    text: str,
    *,
    action_key: str,
    reference: datetime,
    source_chat_id: int,
    source_message_id: int,
) -> MealReply | None:
    try:
        return await _handle_training_message(
            connection,
            text,
            action_key=action_key,
            reference=reference,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
    except TrainingError as exc:
        return MealReply(f"{exc}\nNo training session changed.", "training_rejected")


async def handle_training_callback(
    connection: AsyncConnection,
    action: str,
    session_id: int,
    revision_id: int,
    *,
    action_key: str,
) -> MealReply:
    try:
        current = await get_training(connection, session_id)
        if current.revision_id != revision_id:
            raise TrainingError("That is an older receipt. Open /training history.")
        if action == "edit":
            return MealReply(
                f"Reply with /{current.kind} edit T{current.id}r{current.revision_number} "
                + (
                    "chest and triceps 40 min rpe 7"
                    if current.kind == "gym"
                    else "75 min medium rpe 6"
                ),
                "training_edit_help",
            )
        updated = (
            await revise_training(
                connection, session_id, revision_id, action_key=action_key, delete=True
            )
            if action == "delete"
            else await undo_training(connection, session_id, revision_id, action_key=action_key)
            if action == "undo"
            else None
        )
        if updated is None:
            raise TrainingError("Choose Edit, Delete or Undo.")
        return receipt(updated, "Updated training")
    except TrainingError as exc:
        return MealReply(f"{exc}\nNo training session changed.", "training_rejected")
