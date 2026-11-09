"""Complete PostgreSQL backups on the host's separate persistent volume.

One copy is produced at a time, including private photos and recovery totals.
Completed copies are never overwritten or automatically deleted. The backend
currently assembles a full export in memory; its memory use grows with the
registry. This worker does not make another in-memory copy of that export.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from storage import _restrict_permissions, _utc_datetime


def _backup_directory(value: Path | str) -> Path:
    root = Path(value).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
    if os.name != "nt":
        _restrict_permissions(root, 0o700)
    return root


def create_postgres_backup(store, directory: Path | str, *, now: datetime | None = None) -> Path:
    """Publish one complete JSON copy atomically, refusing name collisions.

    store.backup() atomically replaces our own private staging file. A hard
    link in the same directory then publishes the complete inode with an
    atomic create-if-absent operation. Existing completed copies are untouched
    even if two workers happen to choose the same final name.
    """
    if getattr(store, "backend", None) != "postgres":
        raise ValueError("Для этой резервной копии требуется PostgreSQL.")
    stamp = _utc_datetime(now).strftime("%Y%m%dT%H%M%S%fZ")
    root = _backup_directory(directory)
    destination = root / f"guest-registry-{stamp}-{uuid4().hex}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".postgres-backup-staging-", suffix=".json", dir=root)
    os.close(descriptor)
    staging = Path(temporary_name)
    try:
        _restrict_permissions(staging)
        result = store.backup(staging)
        if Path(result).resolve() != staging or staging.stat().st_size == 0:
            raise RuntimeError("Backup was not completed")
        _restrict_permissions(staging)
        os.link(staging, destination)
        # Persist the new directory entry as well as the file's fsynced bytes.
        if os.name != "nt":
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return destination
    finally:
        staging.unlink(missing_ok=True)


def _reserve_daily_alert(directory: Path | str, day: str) -> bool:
    """Persist the attempt before sending, so restarts don't duplicate it."""
    root = _backup_directory(directory)
    marker = root / (".postgres-backup-alert-" + day)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(day + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _restrict_permissions(marker)
    return True


async def postgres_backup_loop(bot, store, admin_id: int, directory: Path | str, *,
                               check_seconds: float = 21600,
                               retry_seconds: float = 1800) -> None:
    """Copy immediately, then every six hours; retry failures after 30 minutes.

    Alert attempts are at most once per UTC day. A marker makes that limit
    survive restarts when the backup volume is writable; an unavailable volume
    still allows one generic alert attempt during the running process.
    """
    if check_seconds <= 0 or retry_seconds <= 0:
        raise ValueError("Интервал резервного копирования должен быть положительным.")
    if getattr(store, "backend", None) != "postgres":
        raise ValueError("Для этой резервной копии требуется PostgreSQL.")
    last_alert_day = None
    logger = logging.getLogger(__name__)
    while True:
        delay = check_seconds
        operation = asyncio.create_task(asyncio.to_thread(create_postgres_backup, store, directory))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # to_thread cannot abort an in-progress filesystem/database write.
            # Wait for atomic publication/cleanup before the bot closes.
            with suppress(Exception):
                await operation
            raise
        except Exception as error:
            logger.warning("PostgreSQL backup failed (%s)", type(error).__name__)
            delay = retry_seconds
            day = datetime.now(timezone.utc).date().isoformat()
            if last_alert_day != day:
                last_alert_day = day
                try:
                    alert = await asyncio.to_thread(_reserve_daily_alert, directory, day)
                except Exception as marker_error:
                    logger.warning("PostgreSQL backup alert marker failed (%s)", type(marker_error).__name__)
                    alert = True
                if alert:
                    try:
                        await bot.send_message(
                            chat_id=admin_id,
                            text="⚠️ Не удалось создать полную резервную копию картотеки. "
                                 "Нужна проверка хранилища; исходные данные сохранены.",
                            protect_content=True,
                        )
                    except Exception as send_error:
                        logger.warning("PostgreSQL backup alert failed (%s)", type(send_error).__name__)
        await asyncio.sleep(delay)
