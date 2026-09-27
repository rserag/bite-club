import argparse
import json

import pytest
from pydantic import ValidationError

from nutrition_bot.cli import main, source_command
from nutrition_bot.config import NutritionSourceSettings


def source_settings(migrated, **overrides):
    return NutritionSourceSettings(_env_file=None, database_url=migrated.database_url, **overrides)


async def test_no_key_search_returns_manual_offline_choices(migrated, capsys):
    await source_command(
        source_settings(migrated),
        argparse.Namespace(
            command="food-search", query="synthetic food", remote=False, offline=False
        ),
    )
    result = json.loads(capsys.readouterr().out)
    assert result["source_status"] == "not_configured"
    assert result["local"] == result["remote"] == []
    assert "Import a reviewed label manually" in result["fallback_options"]


async def test_offline_skips_provider_even_with_key(migrated, monkeypatch, capsys):
    import httpx

    def unexpected_client(*args, **kwargs):
        pytest.fail("An offline command must not create an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    await source_command(
        source_settings(migrated, usda_api_key="synthetic-secret"),
        argparse.Namespace(command="food-fetch", fdc_id="12345", refresh=False, offline=True),
    )
    assert json.loads(capsys.readouterr().out)["source_status"] == "offline"


def test_blank_key_is_disabled_and_credentials_are_hidden():
    assert NutritionSourceSettings(_env_file=None, usda_api_key=" ").usda_api_key is None
    settings = NutritionSourceSettings(_env_file=None, usda_api_key="synthetic-private-key")
    assert "synthetic-private-key" not in str(settings)
    with pytest.raises(ValidationError) as caught:
        NutritionSourceSettings(_env_file=None, usda_timeout_seconds="synthetic-private-key")
    assert "synthetic-private-key" not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        ["food-select", "--fdc-id", "123"],
        ["food-search", "--query", "synthetic", "--remote", "--offline"],
        ["food-fetch", "--fdc-id", "123", "--offline", "--refresh"],
        ["status", "--query", "synthetic"],
    ],
)
def test_cli_rejects_ambiguous_or_incomplete_options(arguments, monkeypatch):
    monkeypatch.setattr("sys.argv", ["nutrition-bot", *arguments])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
