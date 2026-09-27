from aiogram.types import Update


def message(update_id=1, text="/start", *, user=101, chat=101, kind="private", edited=False):
    return Update.model_validate(
        {
            "update_id": update_id,
            "edited_message" if edited else "message": {
                "message_id": update_id,
                "date": 1700000000,
                "from": {"id": user, "is_bot": False, "first_name": "Synthetic"},
                "chat": {"id": chat, "type": kind},
                "text": text,
            },
        }
    )


def callback(token, *, update_id=2, callback_id="synthetic-callback", message_id=77, user=101):
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "chat_instance": "synthetic-instance",
                "data": f"status:{token}",
                "from": {"id": user, "is_bot": False, "first_name": "Synthetic"},
                "message": {
                    "message_id": message_id,
                    "date": 1700000000,
                    "from": {"id": 123456, "is_bot": True, "first_name": "SyntheticBot"},
                    "chat": {"id": 101, "type": "private"},
                    "text": "Status",
                },
            },
        }
    )


class FakeGateway:
    def __init__(self):
        self.messages = []
        self.answers = []
        self.failure = None
        self.batches = []
        self.offsets = []
        self.keyboards = []

    async def preflight(self):
        pass

    async def poll(self, offset):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    async def send_message(self, chat_id, text, button_token, buttons=None):
        if self.failure:
            raise self.failure
        self.messages.append((chat_id, text, button_token))
        self.keyboards.append(buttons)
        return 77

    async def answer_callback(self, callback_id, text):
        if self.failure:
            raise self.failure
        self.answers.append((callback_id, text))
