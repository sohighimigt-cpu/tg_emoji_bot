from __future__ import annotations

import asyncio
import re
import unicodedata
import secrets
from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import BotCommand
from app.bot.states import CreatePackStates
from app.core.config import ensure_runtime_dirs, load_settings
from app.db.repository import get_active_job_for_user
from app.core.logging_config import setup_logging
from app.domain.pack_naming import normalize_short_name_base, slugify_title
from app.db.repository import (
    cancel_job,
    create_job,
    get_latest_job_for_user,
    set_job_source,
    set_job_title_and_short_name,
    update_job_selection,
    mark_job_ready,
)
from app.services.storage import build_job_input_path, remove_job_input_dir


logger = setup_logging()


ORIENTATION_OPTIONS = {
    "square": "Квадрат",
    "portrait": "Портрет",
    "landscape": "Горизонтальная",
}

GRID_OPTIONS = {
    "3x3": "3 × 3",
    "4x4": "4 × 4",
    "5x5": "5 × 5",
    "6x6": "6 × 6",
    "7x7": "7 × 7",
    "8x8": "8 × 8",
}

async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="newpack", description="Создать новый emoji pack"),
            BotCommand(command="status", description="Проверить статус последней задачи"),
        ]
    )
    
def make_short_name_candidate(base: str, bot_username: str, suffix_part: str | None = None) -> str:
    ending = f"_by_{bot_username.lower()}"
    if suffix_part:
        raw_base = f"{base}_{suffix_part}"
    else:
        raw_base = base

    max_base_len = 64 - len(ending)
    raw_base = raw_base[:max_base_len].strip("_")
    raw_base = re.sub(r"_+", "_", raw_base)

    if not raw_base:
        raw_base = "emoji"

    if not raw_base[0].isalpha():
        raw_base = f"e_{raw_base}"
        raw_base = raw_base[:max_base_len].strip("_")

    return f"{raw_base}{ending}"


async def is_sticker_set_name_taken(bot: Bot, short_name: str) -> bool:
    try:
        await bot.get_sticker_set(name=short_name)
        return True
    except TelegramBadRequest as e:
        # Bot API отвечает 400 STICKERSET_INVALID, когда набора не существует.
        # Отдельного типа под это в aiogram нет — сверяем текст устойчиво.
        message = (getattr(e, "message", None) or str(e)).upper()
        if "STICKERSET_INVALID" in message:
            return False
        raise


async def build_unique_short_name(title: str, bot_username: str, bot: Bot) -> str:
    base = normalize_short_name_base(title)

    candidates = [
        make_short_name_candidate(base, bot_username),
        make_short_name_candidate(base, bot_username, "2"),
        make_short_name_candidate(base, bot_username, "3"),
        make_short_name_candidate(base, bot_username, secrets.token_hex(2)),
        make_short_name_candidate(base, bot_username, secrets.token_hex(3)),
    ]

    for candidate in candidates:
        if not await is_sticker_set_name_taken(bot, candidate):
            return candidate

    while True:
        candidate = make_short_name_candidate(base, bot_username, secrets.token_hex(4))
        if not await is_sticker_set_name_taken(bot, candidate):
            return candidate

def orientation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Квадрат", callback_data="orientation:square"),
                InlineKeyboardButton(text="Портрет", callback_data="orientation:portrait"),
            ],
            [
                InlineKeyboardButton(text="Горизонтальная", callback_data="orientation:landscape"),
            ],
            [
                InlineKeyboardButton(text="Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


def grid_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 × 3", callback_data="grid:3x3"),
                InlineKeyboardButton(text="4 × 4", callback_data="grid:4x4"),
            ],
            [
                InlineKeyboardButton(text="5 × 5", callback_data="grid:5x5"),
                InlineKeyboardButton(text="6 × 6", callback_data="grid:6x6"),
            ],
            [
                InlineKeyboardButton(text="7 × 7", callback_data="grid:7x7"),
                InlineKeyboardButton(text="8 × 8", callback_data="grid:8x8"),
            ],
            [
                InlineKeyboardButton(text="← Назад", callback_data="nav:back_to_orientation"),
                InlineKeyboardButton(text="Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


def title_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="← Назад", callback_data="nav:back_to_grid"),
                InlineKeyboardButton(text="Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm:start"),
            ],
            [
                InlineKeyboardButton(text="← Назад", callback_data="nav:back_to_title"),
                InlineKeyboardButton(text="Отмена", callback_data="nav:cancel"),
            ],
        ]
    )


async def render_wizard_message(
    bot: Bot,
    state: FSMContext,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    state_data = await state.get_data()
    wizard_chat_id = state_data.get("wizard_chat_id")
    wizard_message_id = state_data.get("wizard_message_id")

    if wizard_chat_id and wizard_message_id:
        await bot.edit_message_text(
            chat_id=wizard_chat_id,
            message_id=wizard_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет. Я бот для сборки emoji pack.\n\n"
        "Отправь /newpack чтобы начать создание нового пака."
    )


async def cmd_newpack(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    active_job = get_active_job_for_user(user.id)
    if active_job:
        if active_job.status in {"queued", "processing"}:
            await message.answer(
                "У тебя уже есть активная задача.\n\n"
                f"ID: <code>{active_job.public_id}</code>\n"
                f"Статус: <code>{active_job.status}</code>\n\n"
                "Дождись завершения текущей обработки или проверь статус через /status.",
                parse_mode=ParseMode.HTML,
            )
            return

        if active_job.status == "draft":
            settings = load_settings()
            remove_job_input_dir(settings.input_dir, active_job.public_id)
            cancel_job(active_job.public_id, error_message="replaced_by_newpack")
            logger.info(f"Replaced stale draft job public_id={active_job.public_id} user_id={user.id}")
    
    await state.clear()

    job = create_job(
        user_id=user.id,
        chat_id=message.chat.id,
        username=user.username,
        source_type="pending",
    )

    prompt = await message.answer(
        "Новая задача создана.\n"
        f"ID задачи: <code>{job.public_id}</code>\n\n"
        "Теперь отправь исходный файл:\n"
        "- фото\n"
        "- GIF\n"
        "- видео\n"
        "- или документ с картинкой/анимацией",
        parse_mode=ParseMode.HTML,
    )

    await state.set_state(CreatePackStates.waiting_for_media)
    await state.update_data(
        public_id=job.public_id,
        newpack_command_message_id=message.message_id,
        upload_prompt_message_id=prompt.message_id,
        upload_prompt_chat_id=prompt.chat.id,
    )

    logger.info(f"Created job public_id={job.public_id} user_id={user.id}")

async def cmd_status(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    job = get_latest_job_for_user(user.id)
    if not job:
        await message.answer("У тебя пока нет задач.")
        return

    text = (
        f"Последняя задача: <code>{job.public_id}</code>\n"
        f"Статус: <code>{job.status}</code>\n"
        f"Source type: <code>{job.source_type or '-'}</code>\n"
        f"Orientation: <code>{job.orientation or '-'}</code>\n"
        f"Grid: <code>{job.grid_code or '-'}</code>\n"
        f"Title: <code>{job.title or '-'}</code>\n"
        f"Short name: <code>{job.short_name or '-'}</code>\n"
        f"Pack URL: <code>{job.pack_url or '-'}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


async def handle_media(message: Message, bot: Bot, state: FSMContext) -> None:
    state_data = await state.get_data()
    public_id = state_data.get("public_id")

    if not public_id:
        await message.answer("Нет активной задачи. Отправь /newpack.")
        return

    settings = load_settings()

    source_type = None
    source_file_id = None
    original_filename = None
    fallback_ext = ".bin"
    downloadable = None

    if message.photo:
        downloadable = message.photo[-1]
        source_type = "photo"
        source_file_id = downloadable.file_id
        original_filename = "photo.jpg"
        fallback_ext = ".jpg"

    elif message.document:
        downloadable = message.document
        source_type = "document"
        source_file_id = downloadable.file_id
        original_filename = message.document.file_name or "document.bin"
        fallback_ext = ".bin"

    elif message.animation:
        downloadable = message.animation
        source_type = "animation"
        source_file_id = downloadable.file_id
        original_filename = message.animation.file_name or "animation.gif"
        fallback_ext = ".gif"

    elif message.video:
        downloadable = message.video
        source_type = "video"
        source_file_id = downloadable.file_id
        original_filename = message.video.file_name or "video.mp4"
        fallback_ext = ".mp4"

    if downloadable is None or source_type is None or source_file_id is None:
        await message.answer("Не удалось распознать медиа. Отправь фото, GIF, видео или документ.")
        return

    destination = build_job_input_path(
        settings.input_dir,
        public_id,
        original_filename,
        fallback_ext,
    )

    await bot.download(downloadable, destination=destination)

    set_job_source(
        public_id=public_id,
        source_type=source_type,
        source_file_id=source_file_id,
        source_file_path=str(destination),
        original_filename=original_filename,
    )

    logger.info(f"Saved media for public_id={public_id}")
    
    command_message_id = state_data.get("newpack_command_message_id")
    upload_prompt_message_id = state_data.get("upload_prompt_message_id")
    upload_prompt_chat_id = state_data.get("upload_prompt_chat_id")

    if command_message_id:
        try:
            await bot.delete_message(message.chat.id, command_message_id)
        except Exception:
            logger.warning("Could not delete /newpack command message")

    if upload_prompt_message_id and upload_prompt_chat_id:
        try:
            await bot.delete_message(upload_prompt_chat_id, upload_prompt_message_id)
        except Exception:
            logger.warning("Could not delete initial upload prompt message")

    wizard_message = await message.answer(
        "Файл успешно сохранён.\n\n"
        "Теперь выбери ориентацию pack:",
        parse_mode=ParseMode.HTML,
        reply_markup=orientation_keyboard(),
    )

    await state.update_data(
        wizard_chat_id=wizard_message.chat.id,
        wizard_message_id=wizard_message.message_id,
    )
    await state.set_state(CreatePackStates.waiting_for_orientation)


async def on_orientation_selected(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if callback.data is None:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    orientation = callback.data.split(":", 1)[1]
    if orientation not in ORIENTATION_OPTIONS:
        await callback.answer("Неизвестная ориентация.", show_alert=True)
        return

    state_data = await state.get_data()
    public_id = state_data.get("public_id")

    if not public_id:
        await callback.answer("Нет активной задачи.", show_alert=True)
        return

    update_job_selection(public_id=public_id, orientation=orientation)
    await state.update_data(orientation=orientation, orientation_label=ORIENTATION_OPTIONS[orientation])
    await state.set_state(CreatePackStates.waiting_for_grid)
    await callback.answer("Ориентация сохранена.")

    await render_wizard_message(
        bot,
        state,
        text=(
            f"Ориентация: <code>{ORIENTATION_OPTIONS[orientation]}</code>\n\n"
            "Теперь выбери сетку:"
        ),
        reply_markup=grid_keyboard(),
    )


async def on_grid_selected(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if callback.data is None:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    grid_code = callback.data.split(":", 1)[1]
    if grid_code not in GRID_OPTIONS:
        await callback.answer("Неизвестная сетка.", show_alert=True)
        return

    state_data = await state.get_data()
    public_id = state_data.get("public_id")

    if not public_id:
        await callback.answer("Нет активной задачи.", show_alert=True)
        return

    update_job_selection(public_id=public_id, grid_code=grid_code)
    await state.update_data(grid_code=grid_code, grid_label=GRID_OPTIONS[grid_code])
    await state.set_state(CreatePackStates.waiting_for_title)
    await callback.answer("Сетка сохранена.")

    await render_wizard_message(
        bot,
        state,
        text=(
            f"Сетка: <code>{GRID_OPTIONS[grid_code]}</code>\n\n"
            "Теперь введи название pack.\n"
            "Например: <code>Created By fastlife</code>"
        ),
        reply_markup=title_keyboard(),
    )


async def handle_title_input(message: Message, bot: Bot, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    title = (message.text or "").strip()
    if not title:
        await message.answer("Title не должен быть пустым.")
        return

    if len(title) > 64:
        await message.answer("Title слишком длинный. Максимум 64 символа.")
        return

    state_data = await state.get_data()
    public_id = state_data.get("public_id")
    if not public_id:
        await message.answer("Нет активной задачи. Отправь /newpack.")
        return

    bot_info = await bot.get_me()
    if not bot_info.username:
        await message.answer("Не удалось получить username бота.")
        return

    short_name = await build_unique_short_name(title, bot_info.username, bot)
    set_job_title_and_short_name(public_id, title, short_name)

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        logger.warning("Could not delete user title message")

    await state.set_state(CreatePackStates.waiting_for_confirmation)

    await render_wizard_message(
        bot,
        state,
        text=(
            "Проверь параметры перед запуском:\n\n"
            f"Title: <code>{title}</code>\n"
            f"Short name: <code>{short_name}</code>\n"
            f"Orientation: <code>{state_data.get('orientation_label', '-')}</code>\n"
            f"Grid: <code>{state_data.get('grid_label', '-')}</code>\n\n"
            "Если всё верно, подтвердите запуск обработки."
        ),
        reply_markup=confirmation_keyboard(),
    )


async def on_back_to_orientation(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(CreatePackStates.waiting_for_orientation)
    await callback.answer("Возвращаемся к ориентации.")

    await render_wizard_message(
        bot,
        state,
        text="Хорошо, выбери ориентацию заново:",
        reply_markup=orientation_keyboard(),
    )


async def on_back_to_grid(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(CreatePackStates.waiting_for_grid)
    await callback.answer("Возвращаемся к сетке.")

    await render_wizard_message(
        bot,
        state,
        text="Хорошо, выбери сетку заново:",
        reply_markup=grid_keyboard(),
    )


async def on_back_to_title(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(CreatePackStates.waiting_for_title)
    await callback.answer("Возвращаемся к title.")

    await render_wizard_message(
        bot,
        state,
        text=(
            "Теперь введи название pack заново.\n"
            "Например: <code>Murad Twitch Pack</code>"
        ),
        reply_markup=title_keyboard(),
    )


async def on_confirm_start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    state_data = await state.get_data()
    public_id = state_data.get("public_id")

    if not public_id:
        await callback.answer("Нет активной задачи.", show_alert=True)
        return

    mark_job_ready(public_id)
    await state.clear()
    await callback.answer("Задача подтверждена и поставлена в очередь.")

    if callback.message:
        await callback.message.edit_text(
            "Задача подтверждена.\n\n"
            "Она поставлена в очередь на обработку.\n"
            "Ожидайте ответа",
            parse_mode=ParseMode.HTML,
        )


async def on_cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    state_data = await state.get_data()
    public_id = state_data.get("public_id")

    try:
        if public_id:
            settings = load_settings()
            remove_job_input_dir(settings.input_dir, public_id)
            cancel_job(public_id, error_message="cancelled_by_user")
            logger.info(f"Job cancelled and cleaned up: public_id={public_id}")
    except Exception as e:
        logger.exception(f"Failed to cleanup cancelled job public_id={public_id}: {e}")

    await state.clear()
    await callback.answer("Создание задачи отменено.")

    if callback.message:
        await callback.message.edit_text(
            "Текущий сценарий отменён.\n\n"
            "Все входные данные этой попытки удалены.\n"
            "Чтобы начать заново, отправь /newpack.",
            parse_mode=ParseMode.HTML,
        )


async def fallback_message(message: Message) -> None:
    await message.answer(
        "Пока доступны команды:\n"
        "/start\n"
        "/newpack\n"
        "/status"
    )


async def main() -> None:
    settings = load_settings()
    ensure_runtime_dirs(settings)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_newpack, Command("newpack"))
    dp.message.register(cmd_status, Command("status"))

    dp.message.register(
        handle_media,
        StateFilter(CreatePackStates.waiting_for_media),
        F.photo | F.document | F.animation | F.video,
    )

    dp.callback_query.register(
        on_orientation_selected,
        StateFilter(CreatePackStates.waiting_for_orientation),
        F.data.startswith("orientation:"),
    )

    dp.callback_query.register(
        on_grid_selected,
        StateFilter(CreatePackStates.waiting_for_grid),
        F.data.startswith("grid:"),
    )

    dp.callback_query.register(
        on_back_to_orientation,
        StateFilter(CreatePackStates.waiting_for_grid),
        F.data == "nav:back_to_orientation",
    )

    dp.message.register(
        handle_title_input,
        StateFilter(CreatePackStates.waiting_for_title),
        F.text,
    )

    dp.callback_query.register(
        on_back_to_grid,
        StateFilter(CreatePackStates.waiting_for_title),
        F.data == "nav:back_to_grid",
    )

    dp.callback_query.register(
        on_back_to_title,
        StateFilter(CreatePackStates.waiting_for_confirmation),
        F.data == "nav:back_to_title",
    )

    dp.callback_query.register(
        on_confirm_start,
        StateFilter(CreatePackStates.waiting_for_confirmation),
        F.data == "confirm:start",
    )

    dp.callback_query.register(
        on_cancel_flow,
        StateFilter(CreatePackStates.waiting_for_orientation),
        F.data == "nav:cancel",
    )

    dp.callback_query.register(
        on_cancel_flow,
        StateFilter(CreatePackStates.waiting_for_grid),
        F.data == "nav:cancel",
    )

    dp.callback_query.register(
        on_cancel_flow,
        StateFilter(CreatePackStates.waiting_for_title),
        F.data == "nav:cancel",
    )

    dp.callback_query.register(
        on_cancel_flow,
        StateFilter(CreatePackStates.waiting_for_confirmation),
        F.data == "nav:cancel",
    )

    dp.message.register(fallback_message, F.text)

    logger.info("Bot polling started")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    asyncio.run(main())