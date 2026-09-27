import subprocess
import sys


def test_health_command_does_not_load_telegram_client():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import nutrition_bot.cli; "
                "assert 'aiogram' not in sys.modules; "
                "assert 'alembic' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
