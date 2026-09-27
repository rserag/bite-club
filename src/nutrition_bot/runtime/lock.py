import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def database_lock(database: Path) -> Iterator[None]:
    """Exclude another worker or migration, including across container processes."""
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(database) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another worker or migration owns this database") from exc
        yield
    finally:
        os.close(descriptor)
