"""Check daily cloud copies, alert the owner on failure, and keep originals."""
import asyncio
from datetime import datetime, timezone
import logging


async def cloud_backup_loop(bot, store, admin_id: int, *, check_seconds: float = 21600,
                            retry_seconds: float = 1800) -> None:
    if check_seconds <= 0 or retry_seconds <= 0:
        raise ValueError("Интервал резервного копирования должен быть положительным.")
    last_alert_day = None
    while True:
        delay = check_seconds
        try:
            # The bridge durably reuses today's completed copy, including after
            # a restart. It never marks a copy complete before all photos exist.
            result = await asyncio.to_thread(store.create_backup)
            if not isinstance(result, dict) or result.get("complete") is not True:
                raise RuntimeError("Incomplete cloud copy")
        except Exception as error:
            logging.getLogger(__name__).warning("Cloud backup failed (%s)", type(error).__name__)
            delay = retry_seconds
            day = datetime.now(timezone.utc).date()
            if last_alert_day != day:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text="⚠️ Не удалось подтвердить полную резервную копию картотеки. "
                             "Нужна проверка хранилища; исходные данные не удалялись.",
                        protect_content=True,
                    )
                    last_alert_day = day
                except Exception as send_error:
                    logging.getLogger(__name__).warning("Backup alert failed (%s)", type(send_error).__name__)
        await asyncio.sleep(delay)
