"""An unavailable optional provider never stops the synthetic manual diary."""

import pytest

from nutrition_bot.cli import configured_ai_service


@pytest.mark.parametrize("provider", ["chatgpt", "openrouter"])
@pytest.mark.parametrize("flags", [(True, False), (False, True), (True, True)])
async def test_missing_credentials_leave_both_routes_disabled(store, settings, provider, flags):
    selected = settings.model_copy(
        update={
            "llm_provider": provider,
            "ai_text_enabled": flags[0],
            "ai_photo_enabled": flags[1],
        }
    )
    optional = configured_ai_service(store, selected)
    try:
        assert not optional.enabled_for("meal_text")
        assert not optional.enabled_for("meal_photo")
    finally:
        await optional.close()


@pytest.mark.parametrize("provider", ["chatgpt", "openrouter"])
async def test_invalid_private_policy_disables_route_without_echoing_values(
    store,
    settings,
    tmp_path,
    caplog,
    provider,
):
    invalid = tmp_path / "policy.json"
    invalid.write_text('{"private": "synthetic-sensitive-sentinel"}')
    selected = settings.model_copy(
        update={
            "llm_provider": provider,
            "ai_text_enabled": True,
            "chatgpt_credentials_path": tmp_path / "credentials.json",
            "chatgpt_policy_path": invalid,
            "ai_endpoint_manifest": invalid,
            "openrouter_api_key": "synthetic-offline-key",
        }
    )
    optional = configured_ai_service(store, selected)
    try:
        assert not optional.enabled_for("meal_text")
        assert "ai_configuration_unavailable" in caplog.text
        assert "synthetic-sensitive-sentinel" not in caplog.text
        assert str(invalid) not in caplog.text
    finally:
        await optional.close()


async def test_disabled_flags_need_no_credential_files_or_provider_client(store, settings):
    optional = configured_ai_service(store, settings.model_copy(update={"llm_provider": "chatgpt"}))
    try:
        assert optional.plan_adapter is None and optional.adapter is None
        assert not optional.enabled_for("meal_text") and not optional.enabled_for("meal_photo")
    finally:
        await optional.close()
