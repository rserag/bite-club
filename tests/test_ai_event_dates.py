"""Delayed synthetic Telegram updates preserve the same date in both meal paths."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.drafts import get_draft
from nutrition_bot.adapters.database.meals import get_meal
from nutrition_bot.adapters.database.schema import inbox
from nutrition_bot.application.ai_service import AiService
from nutrition_bot.domain.ai import AiMealIntent, AiOutcome
from tests.helpers import FakeGateway, message
from tests.test_telegram_meals import catalog as catalog


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Yerevan"])
@pytest.mark.parametrize("photo", [False, True])
async def test_delayed_ai_meal_uses_original_message_day_like_measured_parser(
    service, store, catalog, timezone, photo
):
    event = datetime(2023, 11, 13, 23, 59, tzinfo=ZoneInfo(timezone))
    accepted = event + timedelta(minutes=6)
    calls = []
    version_id = catalog["rice"].version_id

    class CapturingAi(AiService):
        def enabled_for(self, role):
            return True

        async def interpret(self, *, request_key, text, local_date, photo=None):
            assert not store.writer_lock.locked()
            calls.append((local_date, photo))
            return AiOutcome(
                request_key=request_key,
                role="meal_photo" if photo is not None else "meal_text",
                status="ready",
                proposal=AiMealIntent(
                    intent="meal",
                    label="Meal",
                    local_date=local_date,
                    items=(
                        {
                            "food_version_id": version_id,
                            "grams": "150",
                            "basis": "Synthetic offline date fixture",
                        },
                    ),
                    full_input_accounted_for=True,
                ),
            )

    class PhotoGateway(FakeGateway):
        async def download_photo(self, received):
            assert not store.writer_lock.locked()
            return b"synthetic in-memory transport fixture"

    service.settings = service.settings.model_copy(update={"app_timezone": timezone})
    service.ai_service = CapturingAi(store)
    service.gateway = PhotoGateway()
    update = message(1, "Lunch included a bowl of rice.")
    received = update.message.model_copy(update={"date": event})
    if photo:
        from aiogram.types import PhotoSize

        received = received.model_copy(
            update={
                "text": None,
                "photo": [
                    PhotoSize(
                        file_id="synthetic",
                        file_unique_id="synthetic",
                        width=1,
                        height=1,
                        file_size=1,
                    )
                ],
            }
        )
    update = update.model_copy(update={"message": received})
    measured = message(2, "150g rice")
    measured = measured.model_copy(
        update={"message": measured.message.model_copy(update={"date": event})}
    )
    await service.accept([update, measured])
    async with store.write() as connection:
        await connection.execute(sa.update(inbox).values(received_at=accepted.timestamp()))
    assert await service.process_one()
    assert await service.process_one()
    assert not await service.process_one()
    assert len(calls) == 1 and calls[0][0] == event.date()
    assert (calls[0][1] is not None) is photo
    async with store.engine.connect() as connection:
        draft = await get_draft(connection, 1)
        saved = await get_meal(connection, 1)
        assert draft.content.local_date == saved.local_date == event.date()
        assert draft.content.timezone == saved.timezone == timezone
        assert draft.content.consumed_at == saved.consumed_at == event.timestamp()
    # The result remains a draft, so the second process only saved the measured meal.
    assert draft.state == "open" and draft.content.review_required
