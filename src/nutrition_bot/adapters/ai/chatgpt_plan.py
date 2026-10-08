"""Optional public Responses API with an app-owned ChatGPT plan OAuth grant."""

import asyncio
import base64
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from nutrition_bot.adapters.ai.chatgpt_auth import access_credentials, load_credentials
from nutrition_bot.adapters.ai.openrouter import SYSTEM_PROMPT, _strict_schema
from nutrition_bot.adapters.ai.photos import image_media_type
from nutrition_bot.domain.ai import (
    MAX_AI_RESPONSE_BYTES,
    MAX_AI_TEXT_BYTES,
    AiCatalogItem,
    AiMealIntent,
    AiRole,
    AiUnavailable,
)
from nutrition_bot.domain.food import FrozenModel


class ChatGPTPlanPolicy(FrozenModel):
    """Private explicit activation; subscription usage is distinct from API dollar usage."""

    enabled: bool = Field(default=False, strict=True)
    account_training_opt_out_confirmed: bool = Field(default=False, strict=True)
    reviewed_on: date
    expires_on: date
    policy_url: str = Field(
        pattern=r"^https://(?:developers\.openai\.com|help\.openai\.com|openai\.com)/[^\s]{1,500}$"
    )
    retention_note: str = Field(min_length=1, max_length=500)
    meal_text_model: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,100}$")
    meal_photo_model: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,100}$")
    image_capability_reviewed: bool = Field(default=False, strict=True)
    daily_invocation_limit: int = Field(default=100, strict=True, ge=1, le=100)

    def check(self, role: AiRole) -> str:
        today = datetime.now(UTC).date()
        if (
            not self.enabled
            or not self.account_training_opt_out_confirmed
            or not self.reviewed_on <= today <= self.expires_on
            or not 0 <= (self.expires_on - self.reviewed_on).days <= 30
        ):
            raise AiUnavailable("ChatGPT plan usage needs an active privacy review and sign-in.")
        if role == "meal_photo":
            if not self.meal_photo_model or not self.image_capability_reviewed:
                raise AiUnavailable("The selected ChatGPT model has not been reviewed for photos.")
            return self.meal_photo_model
        return self.meal_text_model


class ChatGPTPlanAdapter:
    def __init__(
        self,
        credentials_path: Path,
        policy: ChatGPTPlanPolicy,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.credentials_path = credentials_path
        self.policy = policy
        self._client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()

    def enabled_for(self, role: AiRole) -> bool:
        try:
            self.policy.check(role)
            return load_credentials(self.credentials_path).inference_permitted()
        except AiUnavailable:
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _models(self, token: str) -> list[dict[str, Any]]:
        try:
            async with self._client.stream(
                "GET",
                "https://api.openai.com/v1/models",
                headers={"Authorization": "Bearer " + token},
                timeout=10,
            ) as response:
                if response.status_code != 200:
                    raise ValueError
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ValueError
            result = json.loads(data)
            models = result["models"]
            if (
                not isinstance(models, list)
                or len(models) > 500
                or not all(isinstance(item, dict) for item in models)
            ):
                raise ValueError
            return [
                {key: item.get(key) for key in ("slug", "visibility", "input_modalities")}
                for item in models
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise AiUnavailable("ChatGPT account model access could not be verified.") from None

    def request_body(
        self,
        *,
        role: AiRole,
        text: str,
        catalog: tuple[AiCatalogItem, ...],
        local_date: date,
        photo: bytes | None = None,
    ) -> dict[str, Any]:
        model = self.policy.check(role)
        if len(text.encode()) > MAX_AI_TEXT_BYTES or (role == "meal_photo") != (photo is not None):
            raise AiUnavailable("This meal input is outside the bounded ChatGPT request limits.")
        user = json.dumps(
            {
                "local_date": local_date.isoformat(),
                "input": text,
                "reviewed_catalog": [item.model_dump() for item in catalog],
            },
            ensure_ascii=False,
        )
        schema = _strict_schema()
        if len((SYSTEM_PROMPT + user + json.dumps(schema)).encode()) > 12_000:
            raise AiUnavailable("This meal description and catalog are too large.")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
        if photo is not None:
            media = image_media_type(photo)
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{media};base64," + base64.b64encode(photo).decode("ascii"),
                }
            )
        # SIWC preview rejects temperature and max_output_tokens. A byte/time
        # receiving bound is not represented as a guaranteed model-output cap.
        return {
            "model": model,
            "instructions": SYSTEM_PROMPT,
            "input": [{"role": "user", "content": content}],
            "store": False,
            "stream": True,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "meal_proposal",
                    "schema": schema,
                    "strict": True,
                }
            },
            "tools": [],
        }

    async def complete(self, *, role: AiRole, body: dict[str, Any]) -> AiMealIntent:
        async with self._lock:
            credentials = await access_credentials(self._client, self.credentials_path)
            token = credentials.access_token.get_secret_value()
            model = self.policy.check(role)
            models = await self._models(token)
            if not any(
                item.get("slug") == model and item.get("visibility") == "list" for item in models
            ):
                raise AiUnavailable("The selected model is not available to this ChatGPT account.")
            try:
                async with self._client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=30,
                ) as response:
                    if response.status_code != 200:
                        raise ValueError
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_AI_RESPONSE_BYTES:
                            raise ValueError
                return parse_completed_stream(bytes(data), expected_model=model)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise AiUnavailable(
                    "ChatGPT could not complete a validated meal draft. Enter it manually."
                ) from None


def parse_completed_stream(data: bytes, *, expected_model: str) -> AiMealIntent:
    if len(data) > MAX_AI_RESPONSE_BYTES:
        raise AiUnavailable("ChatGPT returned an oversized response.")
    completed: dict[str, Any] | None = None
    completed_items: dict[int, dict[str, Any]] = {}
    item_ids: set[str] = set()
    try:
        for block in data.decode().replace("\r\n", "\n").split("\n\n"):
            payload = "\n".join(
                line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
            )
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError
            kind = event.get("type", "")
            if completed is not None:
                # No output/error/completion events may alter a completed stream.
                raise ValueError
            if kind in {"error", "response.failed", "response.incomplete", "response.refusal.done"}:
                raise ValueError
            if kind in {"response.output_item.added", "response.output_item.done"}:
                if event.get("item", {}).get("type") not in {"message", "reasoning"}:
                    raise ValueError
            if kind.startswith("response.function_call_arguments."):
                raise ValueError
            if kind == "response.output_item.done":
                item = event["item"]
                if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}:
                    raise ValueError
                item_id = item.get("id")
                if not isinstance(item_id, str) or not 1 <= len(item_id) <= 160:
                    raise ValueError
                index = event.get("output_index")
                if type(index) is not int or not 0 <= index < 20:
                    raise ValueError
                if index in completed_items or item_id in item_ids:
                    raise ValueError
                completed_items[index] = item
                item_ids.add(item_id)
            if kind == "response.completed":
                if completed is not None:
                    raise ValueError
                completed = event["response"]
        if (
            completed is None
            or completed.get("status") != "completed"
            or completed.get("model") != expected_model
        ):
            raise ValueError
        output = completed["output"]
        if not isinstance(output, list) or len(output) > 20:
            raise ValueError
        # SIWC can omit final output items from the terminal event. Require the
        # explicit output_item.done snapshots rather than accepting partial deltas.
        if completed_items:
            if sorted(completed_items) != list(range(len(completed_items))):
                raise ValueError
            assembled = [completed_items[index] for index in range(len(completed_items))]
            if output and output != assembled:
                raise ValueError
            output = assembled
        texts: list[str] = []
        for item in output:
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
            ):
                raise ValueError
            for part in item["content"]:
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise ValueError
                texts.append(part["text"])
        if len(texts) != 1:
            raise ValueError
        return AiMealIntent.model_validate_json(texts[0])
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise AiUnavailable("ChatGPT did not return a complete valid meal proposal.") from None


def load_plan_policy(path: Path) -> ChatGPTPlanPolicy:
    try:
        if path.stat().st_size > 65_536:
            raise ValueError
        return ChatGPTPlanPolicy.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        raise AiUnavailable("The ChatGPT plan policy is unavailable or invalid.") from None
