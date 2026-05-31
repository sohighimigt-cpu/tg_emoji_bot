from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient, functions, types
from telethon.errors import RPCError
from telethon.tl.types import DocumentAttributeFilename

from app.core.config import load_settings
from app.core.logging_config import setup_logging
from app.db.repository import (
    JobRecord,
    set_job_title_and_short_name,
    short_name_exists,
)
from app.domain.pack_naming import build_unique_short_name
from app.services.converter import ConversionResult

logger = setup_logging()

DEFAULT_EMOJI_ALT = "😎"
DEFAULT_SOFTWARE_TAG = "telegram_emoji_bot"
MAX_SHORT_NAME_ATTEMPTS = 5


async def _build_client() -> TelegramClient:
    settings = load_settings()

    client = TelegramClient(
        str(settings.telethon_session_path),
        settings.api_id,
        settings.api_hash,
    )
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telethon session is not authorized")

    return client


async def _upload_tile_as_document(
    client: TelegramClient,
    file_path: Path,
) -> types.InputDocument:
    suffix = file_path.suffix.lower()
    input_file = await client.upload_file(str(file_path))

    if suffix == ".webm":
        media = types.InputMediaUploadedDocument(
            file=input_file,
            mime_type="video/webm",
            attributes=[
                DocumentAttributeFilename(file_name=file_path.name),
            ],
            nosound_video=True,
            force_file=True,
        )
    elif suffix in {".png", ".webp"}:
        media = types.InputMediaUploadedDocument(
            file=input_file,
            mime_type="image/png" if suffix == ".png" else "image/webp",
            attributes=[
                DocumentAttributeFilename(file_name=file_path.name),
            ],
            force_file=True,
        )
    else:
        raise RuntimeError(f"Unsupported tile extension: {suffix}")

    uploaded = await client(
        functions.messages.UploadMediaRequest(
            peer="me",
            media=media,
        )
    )

    document = getattr(uploaded, "document", None)
    if document is None:
        raise RuntimeError(f"UploadMedia did not return document for {file_path.name}")

    logger.info(f"Uploaded tile {file_path.name} as document id={document.id}")

    return types.InputDocument(
        id=document.id,
        access_hash=document.access_hash,
        file_reference=document.file_reference,
    )


async def add_tiles_to_existing_pack(
    job: JobRecord,
    conversion: ConversionResult,
) -> str:
    if not job.target_short_name:
        raise RuntimeError("job.target_short_name is empty")
    if not conversion.successful_tiles:
        raise RuntimeError("No successful tiles to publish")

    client = await _build_client()
    try:
        stickerset = types.InputStickerSetShortName(short_name=job.target_short_name)

        for tile in sorted(conversion.successful_tiles, key=lambda x: x.index):
            input_document = await _upload_tile_as_document(client, tile.path)
            await client(
                functions.stickers.AddStickerToSetRequest(
                    stickerset=stickerset,
                    sticker=types.InputStickerSetItem(
                        document=input_document,
                        emoji=DEFAULT_EMOJI_ALT,
                        keywords="",
                    ),
                )
            )
            logger.info(f"Added tile {tile.path.name} to set {job.target_short_name}")

        return "https://t.me/addemoji/" + job.target_short_name
    finally:
        await client.disconnect()


async def create_custom_emoji_pack(
    job: JobRecord,
    conversion: ConversionResult,
) -> str:
    if not job.title:
        raise RuntimeError("job.title is empty")
    if not job.short_name:
        raise RuntimeError("job.short_name is empty")
    if not conversion.successful_tiles:
        raise RuntimeError("No successful tiles to publish")

    settings = load_settings()
    client = await _build_client()

    try:
        me = await client.get_me()
        if me is None:
            raise RuntimeError("Could not fetch current Telethon user")

        # Плитки загружаем ОДИН раз — при ретрае имени переиспользуем их.
        sticker_items: list[types.InputStickerSetItem] = []
        for tile in sorted(conversion.successful_tiles, key=lambda x: x.index):
            logger.info(
                f"Publishing tile {tile.path.name} size={tile.size_bytes} bytes "
                f"fps={tile.fps} crf={tile.crf}"
            )
            input_document = await _upload_tile_as_document(client, tile.path)
            sticker_items.append(
                types.InputStickerSetItem(
                    document=input_document,
                    emoji=DEFAULT_EMOJI_ALT,
                    keywords="",
                )
            )

        short_name = job.short_name
        last_error: Exception | None = None

        for attempt in range(MAX_SHORT_NAME_ATTEMPTS):
            try:
                await client(
                    functions.stickers.CreateStickerSetRequest(
                        user_id=me,
                        title=job.title,
                        short_name=short_name,
                        stickers=sticker_items,
                        emojis=True,
                        software=DEFAULT_SOFTWARE_TAG,
                    )
                )
                # Имя поменялось из-за коллизии — сохраняем актуальное в БД.
                if short_name != job.short_name:
                    set_job_title_and_short_name(job.public_id, job.title, short_name)
                    job.short_name = short_name
                return "https://t.me/addemoji/" + short_name
            except RPCError as e:
                if "OCCUPIED" not in str(e).upper():
                    raise
                last_error = e
                short_name = build_unique_short_name(
                    job.title, settings.bot_username, exists=short_name_exists
                )
                logger.warning(
                    f"short_name occupied; retry {attempt + 1} with '{short_name}'"
                )

        raise RuntimeError(
            f"Не удалось создать пак: имя занято после {MAX_SHORT_NAME_ATTEMPTS} попыток ({last_error})."
        )
    finally:
        await client.disconnect()