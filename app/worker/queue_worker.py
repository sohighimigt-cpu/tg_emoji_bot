from __future__ import annotations

import asyncio
import os
import shutil
import socket
from pathlib import Path

from aiogram import Bot

from app.core.config import ensure_runtime_dirs, load_settings
from app.core.logging_config import setup_logging
from app.db.repository import (
    claim_next_queued_job,
    mark_job_done,
    mark_job_failed,
)
from app.services.converter import convert_job_to_tiles, ConversionError
from app.services.storage import remove_job_input_dir
from app.services.telegram_publisher import create_custom_emoji_pack

logger = setup_logging()

POLL_INTERVAL_SECONDS = 3


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def remove_job_output_dir(base_output_dir: Path, public_id: str) -> None:
    job_dir = (base_output_dir / public_id).resolve()

    if not job_dir.exists():
        return

    expected_root = base_output_dir.resolve()
    if job_dir.parent != expected_root:
        raise RuntimeError("Unsafe output cleanup path detected.")

    shutil.rmtree(job_dir)


async def notify_pack_ready(bot: Bot, chat_id: int, pack_url: str) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=f"Пак готов.\n\n{pack_url}",
    )


async def process_job(job, bot: Bot) -> str:
    settings = load_settings()

    conversion = convert_job_to_tiles(job, settings.output_dir)
    pack_url = await create_custom_emoji_pack(job, conversion)

    await notify_pack_ready(bot, job.chat_id, pack_url)

    remove_job_input_dir(settings.input_dir, job.public_id)
    remove_job_output_dir(settings.output_dir, job.public_id)

    return pack_url


async def main() -> None:
    settings = load_settings()
    ensure_runtime_dirs(settings)

    bot = Bot(token=settings.bot_token)
    current_worker = worker_id()
    logger.info(f"Queue worker started: {current_worker}")

    try:
        while True:
            try:
                job = claim_next_queued_job()

                if job is None:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                logger.info(
                    f"Claimed job public_id={job.public_id} "
                    f"source_type={job.source_type} "
                    f"title={job.title} short_name={job.short_name}"
                )

                try:
                    pack_url = await process_job(job, bot)
                    mark_job_done(job.public_id, pack_url)
                    logger.info(f"Job completed public_id={job.public_id} pack_url={pack_url}")
                except ConversionError as e:
                    mark_job_failed(job.public_id, str(e))
                    logger.warning(f"Job failed (conversion) public_id={job.public_id}: {e}")
                    try:
                        remove_job_input_dir(settings.input_dir, job.public_id)
                        remove_job_output_dir(settings.output_dir, job.public_id)
                    except Exception:
                        logger.exception(f"Cleanup failed public_id={job.public_id}")                
                except Exception:
                    mark_job_failed(job.public_id, "Внутренняя ошибка обработки. Попробуйте позже.")
                    try:
                        remove_job_input_dir(settings.input_dir, job.public_id)
                        remove_job_output_dir(settings.output_dir, job.public_id)
                    except Exception:
                        logger.exception(f"Cleanup failed public_id={job.public_id}")                    
                    logger.exception(f"Job failed public_id={job.public_id}")

            except Exception as outer_error:
                logger.exception(f"Worker loop error: {outer_error}")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())