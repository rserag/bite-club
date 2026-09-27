import json
import logging
import sys
from datetime import UTC, datetime


class SafeFormatter(logging.Formatter):
    """Render only event codes supplied by our code, never exception payloads."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "event": record.msg,
                "error_type": getattr(record, "error_type", None),
            }
        )


def configure_logging(level: str) -> None:
    # Third-party HTTP/Telegram exception messages may contain tokens or user content.
    root = logging.getLogger()
    root.handlers = [logging.NullHandler()]
    logger = logging.getLogger("nutrition_bot")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
