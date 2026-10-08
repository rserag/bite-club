import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from nutrition_bot.miniapp.auth import AuthenticationError, authenticate

TOKEN = "123456:synthetic_token_for_offline_tests_only"


def signed(*, user=101, timestamp=1700000000, **extra):
    values = {
        "auth_date": str(timestamp),
        "query_id": "synthetic_query",
        "user": json.dumps({"id": user, "first_name": "Synthetic"}),
        **extra,
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_official_hmac_accepts_fresh_signed_owner():
    authenticate(signed(), bot_token=TOKEN, owner_id=101, now=1700000100)


@pytest.mark.parametrize(
    "data,now",
    [
        (signed(user=102), 1700000100),
        (signed(), 1700000901),
        (signed(timestamp=1700000101), 1700000100),
        (signed(chat_type="group"), 1700000100),
        (signed(chat=json.dumps({"id": 101, "type": "group"})), 1700000100),
        (signed(user=True), 1700000100),
        (signed() + "&auth_date=1700000000", 1700000100),
        (signed().replace("Synthetic", "Tampered"), 1700000100),
        (signed() + "&unexpected=%GG", 1700000100),
        ("", 1700000100),
        ("a" * 8193, 1700000100),
    ],
)
def test_tampering_expiry_duplicates_and_other_users_rejected_without_data(data, now):
    with pytest.raises(AuthenticationError) as error:
        authenticate(data, bot_token=TOKEN, owner_id=101, now=now)
    assert str(error.value) == "Open Bite Club again from your private Telegram chat."


def test_rejects_bot_and_string_id_even_if_correctly_signed():
    for user in ({"id": "101"}, {"id": 101, "is_bot": True}):
        data = signed(user=json.dumps(user))
        with pytest.raises(AuthenticationError):
            authenticate(data, bot_token=TOKEN, owner_id=101, now=1700000100)
