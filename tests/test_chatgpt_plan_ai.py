"""Synthetic OAuth signatures and Responses events; no account or live network access."""

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from nutrition_bot.adapters.ai.chatgpt_auth import (
    ISSUER,
    JWKS_URL,
    RESOURCE,
    TOKEN_URL,
    ChatGPTCredentials,
    PendingSignIn,
    access_credentials,
    finish_sign_in,
    import_credentials,
    load_credentials,
    save_credentials,
    stable_host_id,
    verify_id_token,
)
from nutrition_bot.adapters.ai.chatgpt_plan import (
    ChatGPTPlanAdapter,
    ChatGPTPlanPolicy,
    parse_completed_stream,
)
from nutrition_bot.adapters.database.schema_ai import ai_attempts, ai_plan_invocations
from nutrition_bot.application.ai_service import AiService
from nutrition_bot.domain.ai import AiCatalogItem, AiUnavailable
from tests.test_ai_drafts import TODAY, proposed
from tests.test_telegram_meals import catalog as catalog

HOST_ID = "urn:uuid:00000000-0000-4000-8000-000000000001"
CLIENT_ID = "oaiapp_synthetic_offline_client"


def credentials(**changes):
    value = dict(
        subject="synthetic-subject",
        client_id=CLIENT_ID,
        ext_agent_host_id=HOST_ID,
        id_token=SecretStr("synthetic-id-token"),
        access_token=SecretStr("synthetic-access-token"),
        refresh_token=SecretStr("synthetic-refresh-token"),
        token_type="Bearer",
        scopes=("openid", "resource.invoke", "chatgpt.tokens.use.direct", "offline_access"),
        expires_at=time.time() + 3600,
        saved_at=time.time(),
    )
    value.update(changes)
    return ChatGPTCredentials(**value)


def policy(**changes):
    value = dict(
        enabled=True,
        account_training_opt_out_confirmed=True,
        reviewed_on=TODAY,
        expires_on=TODAY + timedelta(days=7),
        policy_url="https://developers.openai.com/siwc/token-sharing-open-source",
        retention_note="Synthetic offline policy review, no real account",
        meal_text_model="synthetic-model-v1",
    )
    value.update(changes)
    return ChatGPTPlanPolicy(**value)


def sign_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid="synthetic-kid", alg="RS256", use="sig")
    return private, public


def signed_token(private, expected_nonce, **changes):
    claims = dict(
        iss=ISSUER,
        aud=CLIENT_ID,
        sub="synthetic-subject",
        iat=int(time.time()),
        exp=int(time.time()) + 3600,
        nonce=expected_nonce,
    )
    claims.update(changes)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "synthetic-kid"})


def oauth_result(token, **changes):
    result = dict(
        id_token=token,
        access_token="synthetic-new-access",
        refresh_token="synthetic-new-refresh",
        scope="openid resource.invoke chatgpt.tokens.use.direct offline_access",
        expires_in=3600,
        token_type="Bearer",
    )
    result.update(changes)
    return result


def stream(proposal=None, **changes):
    response = dict(
        status="completed",
        model="synthetic-model-v1",
        output=[
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(proposal or proposed())}],
            }
        ],
    )
    response.update(changes)
    event = {"type": "response.completed", "response": response}
    return ("event: response.completed\ndata: " + json.dumps(event) + "\n\n").encode()


def private_path(tmp_path: Path) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory / "chatgpt.json"


def test_app_registration_uses_pkce_unique_host_state_nonce_and_dynamic_client():
    pending = PendingSignIn.create(HOST_ID, 1455)
    query = parse_qs(urlsplit(pending.authorization_url).query)
    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["agent_name_hint"] == ["Bite Club"]
    assert query["redirect_uri"] == ["http://127.0.0.1:1455/auth/callback"]
    assert query["resource"] == [RESOURCE]
    assert query["ext_agent_host_id"] == [HOST_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0] != pending.verifier
    assert "chatgpt.tokens.use.direct" in query["scope"][0]
    assert pending.state != pending.nonce


def test_returning_account_reuses_issued_client_and_its_id_hint():
    selected = credentials()
    pending = PendingSignIn.create(HOST_ID, 4444, selected)
    query = parse_qs(urlsplit(pending.authorization_url).query)
    assert query["client_id"] == [CLIENT_ID] and "agent_name_hint" not in query
    assert query["id_token_hint"] == [selected.id_token.get_secret_value()]


@pytest.mark.parametrize(
    "path",
    [
        "/auth/callback?code=synthetic&state=other&client_id=" + CLIENT_ID,
        "/auth/callback?code=synthetic&state=state",
        "/callback?code=synthetic&state=state&client_id=" + CLIENT_ID,
        "/auth/callback?code=synthetic&state=state&state=state&client_id=" + CLIENT_ID,
        "/auth/callback?error=access_denied&state=state",
        "/auth/callback?code=synthetic&state=state&client_id=dynamic_agent_client",
    ],
)
def test_unbound_duplicate_declined_or_incomplete_callback_rejected(path):
    pending = PendingSignIn(
        HOST_ID, "http://127.0.0.1:1455/auth/callback", "state", "nonce", "verifier"
    )
    with pytest.raises((AiUnavailable, ValueError)):
        pending.callback(path)


def test_callback_issued_client_cannot_replace_selected_registration():
    pending = PendingSignIn(
        HOST_ID, "http://127.0.0.1:1455/auth/callback", "state", "nonce", "verifier", credentials()
    )
    assert pending.callback("/auth/callback?code=synthetic&state=state") == ("synthetic", CLIENT_ID)
    with pytest.raises(AiUnavailable):
        pending.callback("/auth/callback?code=synthetic&state=state&client_id=oaiapp_other")


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://other.invalid"},
        {"aud": "oaiapp_other"},
        {"nonce": "other"},
        {"exp": int(time.time()) - 1},
        {"sub": ""},
    ],
)
async def test_oidc_identity_signature_issuer_audience_expiry_nonce_checked(change):
    private, public = sign_key()
    token = signed_token(private, "nonce", **change)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [public]}))
    ) as client:
        with pytest.raises(AiUnavailable):
            await verify_id_token(client, token, client_id=CLIENT_ID, nonce="nonce")


async def test_code_exchange_uses_issued_client_exact_uri_and_signed_identity(tmp_path):
    path = private_path(tmp_path)
    private, public = sign_key()
    pending = PendingSignIn.create(HOST_ID, 1455)
    token = signed_token(private, pending.nonce)
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            form = parse_qs(request.content.decode())
            assert form["client_id"] == [CLIENT_ID]
            assert form["code_verifier"] == [pending.verifier]
            assert form["redirect_uri"] == [pending.redirect_uri]
            assert form["resource"] == [RESOURCE]
            assert "client_secret" not in form
            return httpx.Response(200, json=oauth_result(token))
        assert str(request.url) == JWKS_URL
        return httpx.Response(200, json={"keys": [public]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await finish_sign_in(
            client,
            pending,
            f"/auth/callback?code=synthetic&state={pending.state}&client_id={CLIENT_ID}",
            path,
        )
        assert result.inference_permitted() and result.subject == "synthetic-subject"
    assert load_credentials(path) == result
    assert path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-new-access" not in repr(result)
    assert calls == [TOKEN_URL, JWKS_URL]


async def test_identity_switch_never_overwrites_existing_session(tmp_path):
    path = private_path(tmp_path)
    previous = credentials()
    save_credentials(path, previous)
    private, public = sign_key()
    pending = PendingSignIn.create(HOST_ID, 1455, previous)
    token = signed_token(private, pending.nonce, sub="synthetic-other-subject")

    def transport(request):
        return httpx.Response(
            200, json=oauth_result(token) if str(request.url) == TOKEN_URL else {"keys": [public]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AiUnavailable):
            await finish_sign_in(
                client, pending, f"/auth/callback?code=synthetic&state={pending.state}", path
            )
    assert load_credentials(path) == previous


async def test_scope_denial_keeps_identity_but_disables_inference(tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials(scopes=("openid",)))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("No request allowed"))
    ) as client:
        with pytest.raises(AiUnavailable):
            await access_credentials(client, path)


async def test_refresh_rotates_all_credentials_and_keeps_host_identity(tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials(expires_at=time.time() - 1))

    def transport(request):
        assert str(request.url) == TOKEN_URL
        values = parse_qs(request.content.decode())
        assert values["grant_type"] == ["refresh_token"] and values["client_id"] == [CLIENT_ID]
        assert "scope" not in values and values["resource"] == [RESOURCE]
        return httpx.Response(200, json=oauth_result(None))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await access_credentials(client, path)
    assert result.access_token.get_secret_value() == "synthetic-new-access"
    assert result.refresh_token.get_secret_value() == "synthetic-new-refresh"
    assert result.ext_agent_host_id == HOST_ID
    assert load_credentials(path) == result


@pytest.mark.parametrize("persistence_fails", [False, True])
async def test_cancelled_refresh_joins_validation_and_preserves_cancellation(
    tmp_path, monkeypatch, persistence_fails
):
    path = private_path(tmp_path)
    save_credentials(path, credentials(expires_at=time.time() - 1))
    private, public = sign_key()
    entered_jwks = asyncio.Event()
    release_jwks = asyncio.Event()
    if persistence_fails:

        def fail_save(*args):
            raise OSError("Synthetic unavailable credential storage")

        monkeypatch.setattr("nutrition_bot.adapters.ai.chatgpt_auth.save_credentials", fail_save)

    async def transport(request):
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=oauth_result(signed_token(private, None)))
        assert str(request.url) == JWKS_URL
        entered_jwks.set()
        await release_jwks.wait()
        return httpx.Response(200, json={"keys": [public]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        renewal = asyncio.create_task(access_credentials(client, path))
        await entered_jwks.wait()
        for _ in range(3):
            renewal.cancel()
            await asyncio.sleep(0)
        assert not renewal.done()
        assert load_credentials(path).refresh_token.get_secret_value() == "synthetic-refresh-token"
        with pytest.raises(AiUnavailable):
            await access_credentials(client, path)
        release_jwks.set()
        with pytest.raises(asyncio.CancelledError):
            await renewal
    saved = load_credentials(path)
    assert saved.refresh_token.get_secret_value() == (
        "synthetic-refresh-token" if persistence_fails else "synthetic-new-refresh"
    )
    assert saved.access_token.get_secret_value() == (
        "synthetic-access-token" if persistence_fails else "synthetic-new-access"
    )
    assert saved.subject == "synthetic-subject" and saved.ext_agent_host_id == HOST_ID


def test_private_file_rejects_world_permissions_symlink_and_host_replacement(tmp_path):
    source = private_path(tmp_path)
    save_credentials(source, credentials())
    source.chmod(0o644)
    with pytest.raises(AiUnavailable):
        load_credentials(source)
    source.chmod(0o600)
    alias = source.with_name("alias.json")
    alias.symlink_to(source)
    with pytest.raises(AiUnavailable):
        load_credentials(alias)
    host = source.with_name("host-id")
    vm_host = stable_host_id(host)
    assert vm_host != HOST_ID and stable_host_id(host) == vm_host
    target = source.with_name("vm-credentials.json")
    import_credentials(source, target, host)
    assert load_credentials(target).ext_agent_host_id == vm_host


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"account_training_opt_out_confirmed": False},
        {"expires_on": TODAY - timedelta(days=1)},
        {"expires_on": TODAY + timedelta(days=31)},
    ],
)
def test_plan_disabled_without_current_opt_out_policy(change):
    with pytest.raises(AiUnavailable):
        policy(**change).check("meal_text")


def test_plan_request_uses_public_preview_contract_and_no_tools(tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    adapter = ChatGPTPlanAdapter(path, policy())
    body = adapter.request_body(
        role="meal_text",
        text="food",
        catalog=(AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),),
        local_date=TODAY,
    )
    assert body["store"] is False and body["stream"] is True and body["tools"] == []
    assert "instructions" in body and body["input"][0]["role"] == "user"
    assert (
        "max_output_tokens" not in body
        and "temperature" not in body
        and "previous_response_id" not in body
    )
    assert body["text"]["format"]["strict"] is True


@pytest.mark.parametrize(
    "data",
    [
        b"data: [DONE]\n\n",
        b'data: {"type":"response.failed"}\n\n',
        stream(status="incomplete"),
        stream(model="synthetic-other-model"),
        stream(output=[{"type": "function_call", "arguments": "drop table"}]),
        stream(proposed(nutrients={"energy": 123})),
        stream() + stream(),
        b"x" * 65537,
    ],
)
def test_only_completed_valid_typed_response_can_become_proposal(data):
    with pytest.raises(AiUnavailable):
        parse_completed_stream(data, expected_model="synthetic-model-v1")


def test_completed_sse_proposal_validates_exact_fields():
    proposal = parse_completed_stream(stream(), expected_model="synthetic-model-v1")
    assert proposal.items[0].food_version_id == 1 and proposal.items[0].grams == "150"


async def test_plan_adapter_checks_account_catalog_and_public_endpoint(tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    calls = []

    def transport(request):
        calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer synthetic-access-token"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "synthetic-model-v1",
                            "visibility": "list",
                            "display_name": "Synthetic",
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/responses"
        assert json.loads(request.content)["store"] is False
        return httpx.Response(200, content=stream(), headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        body = adapter.request_body(
            role="meal_text",
            text="food",
            catalog=(AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),),
            local_date=TODAY,
        )
        result = await adapter.complete(role="meal_text", body=body)
        assert result.intent == "meal"
    assert calls == ["https://api.openai.com/v1/models", "https://api.openai.com/v1/responses"]


async def test_chatgpt_plan_quota_is_separate_from_dollar_budget(store, catalog, tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    calls = []

    async def transport(request):
        assert not store.writer_lock.locked()
        calls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
            )
        return httpx.Response(200, content=stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = ChatGPTPlanAdapter(path, policy(daily_invocation_limit=1), client=client)
        service = AiService(store, plan_adapter=adapter)
        first = await service.interpret(request_key="plan-1", text="rice", local_date=TODAY)
        replay = await service.interpret(request_key="plan-1", text="rice", local_date=TODAY)
        second = await service.interpret(request_key="plan-2", text="rice", local_date=TODAY)
        assert first.status == "ready" and replay == first and second.status == "quota"
        assert len(calls) == 2
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(ai_plan_invocations))
            == 1
        )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(ai_attempts)) == 0


@pytest.mark.parametrize("token_type", [None, False, 1, [], {}])
async def test_malformed_token_type_fails_safely_without_replacing_credentials(
    tmp_path, token_type
):
    path = private_path(tmp_path)
    previous = credentials(expires_at=time.time() - 1)
    save_credentials(path, previous)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=oauth_result(None, token_type=token_type))
        )
    ) as client:
        with pytest.raises(AiUnavailable):
            await access_credentials(client, path)
    assert load_credentials(path) == previous


@pytest.mark.parametrize("keys", [[None], [1], ["not a jwk"], [[{}]]])
async def test_malformed_jwks_entries_fail_safely(keys):
    private, _ = sign_key()
    token = signed_token(private, "nonce")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": keys}))
    ) as client:
        with pytest.raises(AiUnavailable):
            await verify_id_token(client, token, client_id=CLIENT_ID, nonce="nonce")


async def test_unsigned_or_hmac_id_token_is_not_accepted():
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "synthetic",
            "exp": int(time.time()) + 100,
            "iat": int(time.time()),
        },
        "synthetic-no-authority-secret-32-or-more-bytes",
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Rejected algorithm cannot fetch keys")
        )
    ) as client:
        with pytest.raises(AiUnavailable):
            await verify_id_token(client, token, client_id=CLIENT_ID, nonce="nonce")


def test_terminal_completion_can_assemble_explicit_completed_output_item():
    item = {
        "id": "synthetic-item",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": json.dumps(proposed())}],
    }
    done = (
        "data: "
        + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": item})
        + "\n\n"
    ).encode()
    proposal = parse_completed_stream(done + stream(output=[]), expected_model="synthetic-model-v1")
    assert proposal.items[0].grams == "150"


@pytest.mark.parametrize(
    "item",
    [
        {
            "id": "synthetic-item",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [{"type": "output_text", "text": json.dumps(proposed())}],
        },
        {"id": "synthetic-tool", "type": "function_call", "arguments": "{}"},
        {"type": "message", "role": "assistant", "status": "completed", "content": []},
    ],
)
def test_stream_output_items_require_complete_nontool_snapshots(item):
    done = (
        "data: "
        + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": item})
        + "\n\n"
    ).encode()
    with pytest.raises(AiUnavailable):
        parse_completed_stream(done + stream(output=[]), expected_model="synthetic-model-v1")


def test_partial_deltas_and_done_items_without_terminal_completion_are_rejected():
    delta = (
        "data: "
        + json.dumps({"type": "response.output_text.delta", "delta": json.dumps(proposed())})
        + "\n\n"
    ).encode()
    with pytest.raises(AiUnavailable):
        parse_completed_stream(delta + stream(output=[]), expected_model="synthetic-model-v1")


def done_event(index=0, **changes):
    item = dict(
        id="synthetic-item",
        type="message",
        role="assistant",
        status="completed",
        content=[{"type": "output_text", "text": json.dumps(proposed())}],
    )
    item.update(changes)
    return (
        "data: "
        + json.dumps({"type": "response.output_item.done", "output_index": index, "item": item})
        + "\n\n"
    ).encode()


@pytest.mark.parametrize("index", [None, True, -1, 1, 20, "0", 0.0])
def test_output_done_indexes_must_be_bounded_integers_and_contiguous(index):
    with pytest.raises(AiUnavailable):
        parse_completed_stream(
            done_event(index) + stream(output=[]), expected_model="synthetic-model-v1"
        )


@pytest.mark.parametrize(
    "data",
    [
        done_event() + done_event() + stream(output=[]),
        done_event() + done_event(1) + stream(output=[]),
        done_event() + stream(output=[{"type": "reasoning", "id": "different"}]),
        stream() + done_event(),
        stream() + b'data: {"type":"response.output_text.delta","delta":"late"}\n\n',
        stream() + b'data: {"type":"error"}\n\n',
        done_event(type="function_call") + stream(),
        b'data: {"type":"response.output_item.added","item":{"type":"function_call"}}\n\n'
        + stream(),
        b'data: {"type":"response.function_call_arguments.delta","delta":"{}"}\n\n' + stream(),
        done_event(),
    ],
)
def test_output_done_duplicates_tools_mismatch_and_post_terminal_events_fail_closed(data):
    with pytest.raises(AiUnavailable):
        parse_completed_stream(data, expected_model="synthetic-model-v1")


def test_done_snapshots_and_terminal_output_must_agree_and_can_finish_with_done_marker():
    event = json.loads(done_event().decode().removeprefix("data: "))
    proposal = parse_completed_stream(
        done_event() + stream(output=[event["item"]]) + b"data: [DONE]\n\n",
        expected_model="synthetic-model-v1",
    )
    assert proposal.items[0].grams == "150"


async def test_large_catalog_is_bounded_and_only_model_capabilities_are_retained(tmp_path):
    path = private_path(tmp_path)
    save_credentials(path, credentials())
    entries = {
        "models": [
            {
                "slug": "synthetic-model-v1",
                "visibility": "list",
                "input_modalities": ["text", "image"],
                "base_instructions": "untrusted-ignore-this" * 20000,
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=entries))
    ) as client:
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        items = await adapter._models("synthetic-token")
        assert items == [
            {
                "slug": "synthetic-model-v1",
                "visibility": "list",
                "input_modalities": ["text", "image"],
            }
        ]
