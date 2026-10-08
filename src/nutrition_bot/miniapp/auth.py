"""Validate Telegram's signed, short-lived Mini App launch data."""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    """A fixed error deliberately contains no launch data or credentials."""

    def __init__(self) -> None:
        super().__init__("Open Bite Club again from your private Telegram chat.")


def authenticate(
    init_data: str,
    *,
    bot_token: str,
    owner_id: int,
    now: float | None = None,
    max_age: int = 900,
) -> None:
    """Bot-token HMAC algorithm from https://core.telegram.org/bots/webapps.

    Launch data is a bearer credential for its brief lifetime. It is accepted
    only in a header, never a URL; client-supplied initDataUnsafe is never used.
    """
    try:
        if not init_data or len(init_data.encode()) > 8192:
            raise ValueError
        if re.search(r"%(?![0-9A-Fa-f]{2})", init_data):
            raise ValueError
        pairs = parse_qsl(init_data, strict_parsing=True, max_num_fields=32)
        values = dict(pairs)
        if len(values) != len(pairs) or not {"hash", "auth_date", "user"} <= values.keys():
            raise ValueError
        if any(not re.fullmatch(r"[a-z_]+", key) for key in values):
            raise ValueError
        signature = values.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError
        check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        timestamp = int(values["auth_date"])
        reference = time.time() if now is None else now
        if not 0 <= reference - timestamp <= max_age:
            raise ValueError
        user = json.loads(values["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int:
            raise ValueError
        if user["id"] != owner_id or user.get("is_bot", False) is not False:
            raise ValueError
        # Direct-link launches can include a chat type; this personal UI cannot
        # be used from a group/attachment chat even by the owner.
        if values.get("chat_type", "private") not in {"private", "sender"}:
            raise ValueError
        if "chat" in values:
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise AuthenticationError from None
