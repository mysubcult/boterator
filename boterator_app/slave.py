from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import timedelta
from typing import Any

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramMigrateToChat
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .ai import AiAnalyzer, build_system_prompt
from .db import merge_settings
from .i18n import (
    DEFAULT_START_TEXTS,
    LANGUAGE_LABELS,
    content_label,
    default_text,
    normalize_language,
    translate,
)


LOGGER = logging.getLogger(__name__)

SUBMIT_KINDS = {
    "text": "text",
    "photo": "photo",
    "voice": "voice",
    "video": "video",
    "audio": "audio",
    "document": "document",
    "sticker": "sticker",
    "animation": "gif",
}

CONTENT_LABELS = {
    "text": "📝 Текст",
    "photo": "📷 Фото",
    "voice": "🎙️ Голос",
    "video": "🎬 Видео",
    "audio": "🎧 Аудио",
    "document": "📎 Документы",
    "sticker": "🏷️ Стикеры",
    "gif": "🎞️ GIF",
}

NUMERIC_SETTINGS = {
    "votes": (1, 50),
    "delay": (0, 10080),
    "vote_timeout": (1, 720),
    "text_min": (0, 4000),
    "text_max": (1, 4096),
}

FREQ_LIMIT_DEFAULT_COUNT = 2
FREQ_LIMIT_DEFAULT_HOURS = 24
FREQ_LIMIT_MAX_COUNT = 1000
FREQ_LIMIT_MAX_HOURS = 8760
AI_AUTO_PUBLISH_DEFAULT_SCORE = 85
AI_AUTO_PUBLISH_MIN_SCORE = 50
AI_AUTO_PUBLISH_MAX_SCORE = 100
AI_ANALYSIS_TIMEOUT_SECONDS = 90
AI_PROMPT_MAX_LENGTH = 3200
BAN_REASON_MIN_LENGTH = 5
CONTACT_MESSAGE_MAX_LENGTH = 3900
DUPLICATE_DEFAULT_DAYS = 180
DUPLICATE_MIN_DAYS = 1
DUPLICATE_MAX_DAYS = 3650
DUPLICATE_MIN_CHARS = 24
DUPLICATE_MIN_WORDS = 5
DUPLICATE_EXACT_SCORE = 100
DUPLICATE_JACCARD_THRESHOLD = 88

REJECT_REASONS = {
    "off_topic": "Off-topic",
    "low_quality": "Low quality",
    "ai_like": "AI-like",
    "spam": "Spam",
    "no_caption": "No caption",
    "unsafe": "Unsafe",
}

TOGGLE_LABELS = {
    "selfvote": "Самоголосование",
    "allow_vote_switch": "Смена голоса",
    "tag_polls": "Теги в голосованиях",
    "public_vote": "Публичный счет голосов",
}

PUBLISH_MODE_LABELS = {
    "forward": "🔁 пересылка от отправителя",
    "copy": "📋 копия без автора",
}

AI_PROVIDER_LABELS = {
    "openai": "🤖 OpenAI",
    "gemini": "✨ Gemini",
}

OPENAI_OFFER_MODELS_1M = [
    "gpt-5.5-2026-04-23",
    "gpt-5.4-2026-03-05",
    "gpt-5.2-2025-12-11",
    "gpt-5.1-2025-11-13",
    "gpt-5.1-codex",
    "gpt-5-codex",
    "gpt-5-2025-08-07",
    "gpt-5-chat-latest",
    "gpt-4.1-2025-04-14",
    "gpt-4o-2024-05-13",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "o3-2025-04-16",
    "o1-preview-2024-09-12",
    "o1-2024-12-17",
]

OPENAI_OFFER_MODELS_10M = [
    "gpt-5.4-mini-2026-03-17",
    "gpt-5.4-nano-2026-03-17",
    "gpt-5.1-codex-mini",
    "gpt-5-mini-2025-08-07",
    "gpt-5-nano-2025-08-07",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano-2025-04-14",
    "gpt-4o-mini-2024-07-18",
    "o4-mini-2025-04-16",
    "o1-mini-2024-09-12",
    "codex-mini-latest",
]

GEMINI_MODEL_PRESETS = [
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]


class SlaveManager:
    def __init__(
        self,
        pool: asyncpg.Pool,
        analyzer: AiAnalyzer,
        publish_mode: str,
        data_retention_days: int,
        cleanup_interval_hours: int,
    ):
        self.pool = pool
        self.analyzer = analyzer
        self.publish_mode = publish_mode
        self.data_retention_days = max(1, data_retention_days)
        self.cleanup_interval_seconds = max(3600, cleanup_interval_hours * 3600)
        self.runtimes: dict[int, SlaveRuntime] = {}

    async def start_existing(self) -> None:
        rows = await self.pool.fetch("SELECT * FROM registered_bots WHERE active = TRUE")
        for row in rows:
            await self.start_bot(row)

    async def start_bot_by_id(self, bot_id: int) -> None:
        row = await self.pool.fetchrow("SELECT * FROM registered_bots WHERE id = $1 AND active = TRUE", bot_id)
        if row:
            await self.start_bot(row)

    async def start_bot(self, row: asyncpg.Record) -> None:
        bot_id = int(row["id"])
        if bot_id in self.runtimes:
            await self.runtimes[bot_id].stop()

        runtime = SlaveRuntime(
            self.pool,
            row,
            self.analyzer,
            self.publish_mode,
            self.data_retention_days,
            self.cleanup_interval_seconds,
        )
        self.runtimes[bot_id] = runtime
        runtime.start()
        LOGGER.info("Started slave bot #%s", bot_id)

    async def stop_all(self) -> None:
        await asyncio.gather(*(runtime.stop() for runtime in self.runtimes.values()), return_exceptions=True)


class SlaveRuntime:
    def __init__(
        self,
        pool: asyncpg.Pool,
        row: asyncpg.Record,
        analyzer: AiAnalyzer,
        publish_mode: str,
        data_retention_days: int,
        cleanup_interval_seconds: int,
    ):
        self.pool = pool
        self.bot_id = int(row["id"])
        self.token = row["token"]
        self.owner_id = int(row["owner_id"])
        self.moderator_chat_id = int(row["moderator_chat_id"])
        self.target_channel = row["target_channel"]
        self.settings = merge_settings(row["settings"])
        self.analyzer = analyzer
        self.publish_mode = publish_mode
        self.data_retention_days = data_retention_days
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.bot = Bot(self.token, default=DefaultBotProperties(parse_mode=None))
        self.dp = Dispatcher()
        self.router = Router()
        self.pending: dict[tuple[int, int], Message] = {}
        self.pending_ai_key: dict[int, str] = {}
        self.pending_ai_model: dict[int, str] = {}
        self.pending_ai_prompt: set[int] = set()
        self.pending_start_text: set[int] = set()
        self.pending_contact: dict[int, tuple[int, int]] = {}
        self.pending_ban: dict[int, tuple[int, int, int]] = {}
        self.health_alerts: dict[str, float] = {}
        self.task: asyncio.Task | None = None
        self.publisher_task: asyncio.Task | None = None
        self.timeout_task: asyncio.Task | None = None
        self.cleanup_task: asyncio.Task | None = None
        self.publish_lock = asyncio.Lock()
        self._register_handlers()
        self.dp.include_router(self.router)

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"slave-{self.bot_id}-polling")
        self.publisher_task = asyncio.create_task(self._publisher_loop(), name=f"slave-{self.bot_id}-publisher")
        self.timeout_task = asyncio.create_task(self._timeout_loop(), name=f"slave-{self.bot_id}-timeouts")
        self.cleanup_task = asyncio.create_task(self._cleanup_loop(), name=f"slave-{self.bot_id}-cleanup")

    async def stop(self) -> None:
        for task in (self.task, self.publisher_task, self.timeout_task, self.cleanup_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.task, self.publisher_task, self.timeout_task, self.cleanup_task) if task),
            return_exceptions=True,
        )
        await self.bot.session.close()

    async def _run(self) -> None:
        try:
            await self.bot.delete_webhook(drop_pending_updates=False)
            await self.dp.start_polling(self.bot, allowed_updates=self.dp.resolve_used_update_types())
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Slave bot #%s stopped unexpectedly", self.bot_id)
            await self._notify_health("polling", "⚠️ Slave bot polling stopped unexpectedly. The bot was marked inactive.")
            await self.pool.execute("UPDATE registered_bots SET active = FALSE WHERE id = $1", self.bot_id)

    def _register_handlers(self) -> None:
        @self.router.message(CommandStart())
        async def start(message: Message) -> None:
            if _chat_type(message) == ChatType.PRIVATE.value:
                await self._upsert_user(message)
                await message.answer(self._start_text())

        @self.router.message(Command("settings"))
        async def settings_command(message: Message) -> None:
            if not self._can_manage(message):
                return
            await self._send_settings(message)

        @self.router.message(Command("setmodchat", "setmoderators"))
        async def setmodchat(message: Message) -> None:
            if not self._can_change_moderator_chat(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if _chat_type(message) == ChatType.PRIVATE.value:
                if len(parts) != 2 or not _valid_chat_id(parts[1].strip()):
                    await message.answer("👥 Использование в личке: /setmodchat -1001234567890")
                    return
                chat_id = int(parts[1].strip())
            else:
                chat_id = message.chat.id

            old_chat_id = self.moderator_chat_id
            await self._set_moderator_chat(chat_id)
            await message.answer(
                "✅ Чат модераторов обновлен.\n"
                f"👥 Новый chat_id: {chat_id}\n\n"
                "Новые заявки будут приходить сюда. Старые карточки голосования лучше закрыть до переноса."
            )
            if old_chat_id != chat_id:
                await self._notify_moderator_chat_changed(old_chat_id, chat_id)

        @self.router.message(Command("setvotes"))
        async def setvotes(message: Message) -> None:
            if not self._can_manage(message):
                return
            value = _command_int(message, minimum=1, maximum=50)
            if value is None:
                await message.answer("🗳️ Использование: /setvotes 2")
                return
            await self._update_setting("votes", value)
            await message.answer(f"✅ Теперь для решения нужно голосов: {value}.")

        @self.router.message(Command("setdelay"))
        async def setdelay(message: Message) -> None:
            if not self._can_manage(message):
                return
            value = _command_int(message, minimum=0, maximum=10080)
            if value is None:
                await message.answer("⏱️ Использование: /setdelay 15")
                return
            await self._update_setting("delay", value)
            await message.answer(f"✅ Задержка между публикациями: {value} мин.")

        @self.router.message(Command("settimeout"))
        async def settimeout(message: Message) -> None:
            if not self._can_manage(message):
                return
            value = _command_int(message, minimum=1, maximum=720)
            if value is None:
                await message.answer("⌛ Использование: /settimeout 24")
                return
            await self._update_setting("vote_timeout", value)
            await message.answer(f"✅ Таймаут голосования: {value} ч.")

        @self.router.message(Command("setfreqlimit"))
        async def setfreqlimit(message: Message) -> None:
            if not self._can_manage(message):
                return
            argument = (message.text or "").split(maxsplit=1)
            if len(argument) != 2:
                await message.answer("🚦 Использование: /setfreqlimit 2 24 или /setfreqlimit 0")
                return
            value = argument[1].strip().lower()
            if value in {"0", "off", "disable", "disabled", "none", "выкл", "выключить"}:
                await self._update_setting("msg_freq_limit", None)
                await message.answer("🚫 Лимит отправки выключен.")
                return
            parsed = _parse_freq_limit_value(value)
            if not parsed:
                await message.answer("🚦 Использование: /setfreqlimit 2 24, где 2 - сообщения, 24 - часы.")
                return
            await self._set_freq_limit(*parsed)
            await message.answer(f"✅ Лимит отправки: {_freq_limit_label(parsed)}.")

        @self.router.message(Command("allow"))
        async def allow(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split()
            if len(parts) != 3 or parts[1] not in self.settings["content_status"] or parts[2] not in {"on", "off"}:
                await message.answer("🧩 Использование: /allow photo on")
                return
            content_settings = dict(self.settings["content_status"])
            content_settings[parts[1]] = parts[2] == "on"
            await self._update_setting("content_status", content_settings)
            await message.answer(f"✅ {parts[1]}: {'включено' if content_settings[parts[1]] else 'выключено'}.")

        @self.router.message(Command("setai"))
        async def setai(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split()
            if len(parts) != 2 or parts[1] not in {"on", "off"}:
                await message.answer("🤖 Использование: /setai on или /setai off")
                return
            if parts[1] == "on" and not self._ai_configured():
                await message.answer(
                    "🔑 Сначала добавьте ключ: /setaikey openai <ключ> или /setaikey gemini <ключ>."
                )
                return
            await self._set_ai_enabled(parts[1] == "on")
            await message.answer(f"🤖 AI-анализ: {'включен' if parts[1] == 'on' else 'выключен'}.")

        @self.router.message(Command("setaikey"))
        async def setaikey(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split(maxsplit=2)
            provider = parts[1].lower() if len(parts) >= 2 else None
            if provider not in AI_PROVIDER_LABELS:
                await message.answer("🔑 Использование: /setaikey openai <ключ> или /setaikey gemini <ключ>")
                return
            self.pending_start_text.discard(message.from_user.id)
            self.pending_ai_model.pop(message.from_user.id, None)
            self.pending_ai_prompt.discard(message.from_user.id)
            if len(parts) == 2:
                self.pending_ai_key[message.from_user.id] = provider
                await message.answer(
                    f"🔑 Пришлите следующим сообщением API-ключ для {AI_PROVIDER_LABELS[provider]}. "
                    "🧹 После сохранения я попробую удалить сообщение с ключом."
                )
                return
            await self._store_ai_key(provider, parts[2].strip())
            await self._delete_sensitive_message(message)
            await message.answer(f"✅ Ключ {AI_PROVIDER_LABELS[provider]} сохранен. AI-анализ включен.")

        @self.router.message(Command("setaimodel"))
        async def setaimodel(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 2:
                await message.answer("🧬 Использование: /setaimodel gpt-5-mini-2025-08-07")
                return
            if len(parts) == 3 and parts[1].lower() in AI_PROVIDER_LABELS:
                provider = parts[1].lower()
                model = parts[2].strip()
            else:
                provider = self._ai_provider()
                model = parts[1].strip()
            self.pending_ai_model.pop(message.from_user.id, None)
            self.pending_ai_prompt.discard(message.from_user.id)
            await message.answer(f"🔎 Проверяю модель {model}...")
            ok, response = await self._set_ai_model_checked(provider, model)
            await message.answer(("✅ " if ok else "⚠️ ") + response)

        @self.router.message(Command("setaiprompt"))
        async def setaiprompt(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            self.pending_ai_key.pop(message.from_user.id, None)
            self.pending_ai_model.pop(message.from_user.id, None)
            self.pending_start_text.discard(message.from_user.id)
            if len(parts) == 1 or not parts[1].strip():
                self.pending_ai_prompt.add(message.from_user.id)
                await message.answer(
                    "🧾 Пришлите следующим сообщением полный AI system prompt.\n\n"
                    "Он полностью заменит текущий prompt. Сохраните в нем требуемый формат ответа, "
                    "иначе автопубликация может хуже понимать verdict и процент."
                )
                return
            ok, response = await self._set_ai_prompt(parts[1].strip())
            if ok:
                self.pending_ai_prompt.discard(message.from_user.id)
            await message.answer(("✅ " if ok else "⚠️ ") + response)

        @self.router.message(Command("resetaiprompt"))
        async def resetaiprompt(message: Message) -> None:
            if not self._can_manage(message):
                return
            self.pending_ai_prompt.discard(message.from_user.id)
            await self._reset_ai_prompt()
            await message.answer("🔄 AI system prompt сброшен к дефолтному.")

        @self.router.message(Command("clearaikey"))
        async def clearaikey(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            provider = parts[1].lower() if len(parts) == 2 else None
            if provider is not None and provider not in AI_PROVIDER_LABELS:
                await message.answer("🧹 Использование: /clearaikey или /clearaikey openai|gemini")
                return
            await self._clear_ai_key(provider)
            await message.answer("🧹 AI-ключ удален. AI-анализ выключен." if provider is None else f"🧹 Ключ {AI_PROVIDER_LABELS[provider]} удален.")

        @self.router.message(Command("setstart"))
        async def setstart(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                await message.answer("👋 Использование: /setstart Новый текст приветствия")
                return
            if len(parts[1]) > 4096:
                await message.answer("⚠️ Приветствие слишком длинное. Максимум Telegram - 4096 символов.")
                return
            self.pending_ai_key.pop(message.from_user.id, None)
            self.pending_ai_model.pop(message.from_user.id, None)
            self.pending_ai_prompt.discard(message.from_user.id)
            self.pending_start_text.discard(message.from_user.id)
            await self._set_start_text(parts[1].strip())
            await message.answer("✅ Приветствие /start обновлено.")

        @self.router.message(Command("resetstart"))
        async def resetstart(message: Message) -> None:
            if not self._can_manage(message):
                return
            self.pending_ai_key.pop(message.from_user.id, None)
            self.pending_ai_model.pop(message.from_user.id, None)
            self.pending_ai_prompt.discard(message.from_user.id)
            self.pending_start_text.discard(message.from_user.id)
            await self._reset_start_text()
            await message.answer("🔄 Приветствие /start сброшено к стандартному тексту текущего языка.")

        @self.router.message(Command("banlist"))
        async def banlist(message: Message) -> None:
            if not self._can_manage(message):
                return
            await message.answer(await self._banlist_text())

        @self.router.message(Command("unban"))
        async def unban(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().lstrip("-").isdigit():
                await message.answer("🔓 Использование: /unban 123456789")
                return
            user_id = int(parts[1].strip())
            restored = await self._unban_user(user_id)
            await message.answer("🔓 Пользователь разбанен." if restored else "ℹ️ Пользователь не найден в бане.")

        @self.router.message(Command("setlanguage", "language"))
        async def setlanguage(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split()
            language = parts[1].lower() if len(parts) == 2 else None
            if language not in LANGUAGE_LABELS:
                await message.answer("🌐 Использование: /setlanguage ru или /setlanguage en")
                return
            await self._set_language(language)
            await message.answer(f"🌐 Язык публичного бота и модерации: {LANGUAGE_LABELS[language]}.")

        @self.router.message(Command("setpublish", "setpublishmode"))
        async def setpublish(message: Message) -> None:
            if not self._can_manage(message):
                return
            parts = (message.text or "").split()
            if len(parts) != 2 or parts[1] not in PUBLISH_MODE_LABELS:
                await message.answer("🚀 Использование: /setpublish forward или /setpublish copy")
                return
            await self._update_setting("publish_mode", parts[1])
            await message.answer(f"🚀 Режим отправки: {PUBLISH_MODE_LABELS[parts[1]]}.")

        @self.router.message(Command("power"))
        async def power(message: Message) -> None:
            if not self._can_manage(message):
                return
            await self._update_setting("power", False)
            await message.answer("🔒 Доступ к настройкам теперь только у владельца в личном чате с ботом.")

        @self.router.message(Command("stats"))
        async def stats(message: Message) -> None:
            if self._can_manage(message):
                await message.answer(await self._stats_text())
                return
            if _chat_type(message) != ChatType.PRIVATE.value or not message.from_user:
                return
            await self._upsert_user(message)
            await message.answer(await self._user_stats_text(message.from_user.id))

        @self.router.callback_query(F.data.startswith("confirm|"))
        async def confirm(callback: CallbackQuery) -> None:
            await self._handle_confirmation(callback)

        @self.router.callback_query(F.data.startswith("cancel|"))
        async def cancel(callback: CallbackQuery) -> None:
            key = _parse_message_key(callback.data)
            if key:
                self.pending.pop(key, None)
            if callback.message:
                await callback.message.edit_text(self._t("submission_cancelled"))
            await callback.answer()

        @self.router.callback_query(F.data.startswith("s|"))
        async def settings_callback(callback: CallbackQuery) -> None:
            await self._handle_settings_callback(callback)

        @self.router.callback_query(F.data.startswith("v|"))
        async def vote(callback: CallbackQuery) -> None:
            await self._handle_vote(callback)

        @self.router.callback_query(F.data.startswith("r|"))
        async def reject(callback: CallbackQuery) -> None:
            await self._handle_reject(callback)

        @self.router.callback_query(F.data.startswith("rr|"))
        async def reject_reason(callback: CallbackQuery) -> None:
            await self._handle_reject_reason(callback)

        @self.router.callback_query(F.data.startswith("c|"))
        async def contact(callback: CallbackQuery) -> None:
            await self._handle_contact_request(callback)

        @self.router.callback_query(F.data.startswith("b|"))
        async def ban(callback: CallbackQuery) -> None:
            await self._handle_ban_request(callback)

        @self.router.message()
        async def incoming(message: Message) -> None:
            if await self._handle_pending_moderator_action(message):
                return
            if await self._handle_pending_ai_key(message):
                return
            if await self._handle_pending_ai_model(message):
                return
            if await self._handle_pending_ai_prompt(message):
                return
            if await self._handle_pending_start_text(message):
                return
            await self._handle_incoming_message(message)

    def _settings_entry_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть настройки", callback_data="s|nav|main")]]
        )

    async def _send_settings(self, message: Message, section: str = "main") -> None:
        await message.answer(
            await self._settings_text(section),
            reply_markup=await self._settings_keyboard(section),
        )

    async def _handle_settings_callback(self, callback: CallbackQuery) -> None:
        if not callback.message or not self._can_manage_callback(callback):
            await callback.answer()
            return

        parts = (callback.data or "").split("|")
        if len(parts) < 2:
            await callback.answer()
            return

        action = parts[1]
        section = "main"
        callback_answered = False

        if action == "nav" and len(parts) >= 3:
            section = parts[2]
        elif action == "close":
            await callback.message.edit_text("✅ Меню настроек закрыто.")
            await callback.answer()
            return
        elif action == "adj" and len(parts) >= 5:
            setting, delta, section = parts[2], int(parts[3]), parts[4]
            await self._adjust_numeric_setting(setting, delta)
        elif action == "freq_adj" and len(parts) >= 4:
            await self._adjust_freq_limit(parts[2], int(parts[3]))
            section = "freq"
        elif action == "freq_preset":
            await self._set_freq_limit(FREQ_LIMIT_DEFAULT_COUNT, FREQ_LIMIT_DEFAULT_HOURS)
            section = "freq"
        elif action == "freq_disable":
            await self._update_setting("msg_freq_limit", None)
            section = "freq"
        elif action == "ai_score_adj" and len(parts) >= 3:
            await self._adjust_ai_auto_publish_score(int(parts[2]))
            section = "ai"
        elif action == "duplicate_days_adj" and len(parts) >= 3:
            await self._adjust_duplicate_detection_days(int(parts[2]))
            section = "duplicates"
        elif action == "toggle" and len(parts) >= 4:
            target, section = parts[2], parts[-1]
            if target == "ai":
                if not self._ai_setting_enabled() and not self._ai_configured():
                    await callback.answer("🔑 Сначала добавьте API-ключ.", show_alert=True)
                    await self._edit_settings(callback, "ai")
                    return
                await self._set_ai_enabled(not self._ai_setting_enabled())
            elif target == "ai_auto_publish":
                if not self._ai_enabled():
                    await callback.answer("🤖 Сначала включите AI-анализ и добавьте ключ.", show_alert=True)
                    await self._edit_settings(callback, "ai")
                    return
                await self._set_ai_auto_publish(not self._ai_auto_publish_enabled())
            elif target.startswith("ai_provider:"):
                await self._set_ai_provider(target.split(":", 1)[1])
            elif target.startswith("language:"):
                await self._set_language(target.split(":", 1)[1])
            elif target.startswith("publish:"):
                await self._set_publish_mode(target.split(":", 1)[1])
            elif target.startswith("content:"):
                await self._toggle_content_setting(target.split(":", 1)[1])
            elif target == "duplicate_detection":
                await self._set_duplicate_detection_enabled(not self._duplicate_detection_enabled())
            else:
                await self._toggle_boolean_setting(target)
        elif action == "ai_key" and len(parts) >= 3:
            provider = parts[2]
            if provider not in AI_PROVIDER_LABELS:
                await callback.answer()
                return
            self.pending_start_text.discard(callback.from_user.id)
            self.pending_ai_model.pop(callback.from_user.id, None)
            self.pending_ai_prompt.discard(callback.from_user.id)
            self.pending_ai_key[callback.from_user.id] = provider
            if callback.message:
                await callback.message.answer(
                    f"🔑 Пришлите следующим сообщением API-ключ для {AI_PROVIDER_LABELS[provider]}. "
                    "🧹 После сохранения я попробую удалить сообщение с ключом."
                )
        elif action == "ai_model" and len(parts) >= 3:
            provider = self._ai_provider()
            model = parts[2]
            await callback.answer("🔎 Проверяю модель...")
            callback_answered = True
            ok, message = await self._set_ai_model_checked(provider, model)
            if not ok:
                await callback.message.answer(f"⚠️ {message}")
                section = "ai_model"
            else:
                await callback.message.answer(f"✅ {message}")
                section = "ai_model"
        elif action == "ai_model_custom":
            provider = self._ai_provider()
            self.pending_ai_key.pop(callback.from_user.id, None)
            self.pending_start_text.discard(callback.from_user.id)
            self.pending_ai_prompt.discard(callback.from_user.id)
            self.pending_ai_model[callback.from_user.id] = provider
            section = "ai_model"
            await callback.message.answer(
                f"🧬 Пришлите следующим сообщением ID модели для {AI_PROVIDER_LABELS[provider]}.\n\n"
                "Например: gpt-5-mini-2025-08-07"
            )
        elif action == "ai_prompt_edit":
            self.pending_ai_key.pop(callback.from_user.id, None)
            self.pending_ai_model.pop(callback.from_user.id, None)
            self.pending_start_text.discard(callback.from_user.id)
            self.pending_ai_prompt.add(callback.from_user.id)
            section = "ai_prompt"
            await callback.message.answer(
                "🧾 Пришлите следующим сообщением полный AI system prompt.\n\n"
                "Текущий prompt показан в меню выше: его можно скопировать, изменить и отправить обратно. "
                "Новый текст полностью заменит текущий prompt."
            )
        elif action == "ai_prompt_reset":
            self.pending_ai_prompt.discard(callback.from_user.id)
            await self._reset_ai_prompt()
            section = "ai_prompt"
        elif action == "ai_prompt_rollback" and len(parts) >= 3:
            try:
                history_id = int(parts[2])
            except ValueError:
                await callback.answer()
                return
            ok = await self._rollback_ai_prompt(history_id)
            section = "ai_prompt"
            await callback.answer("✅ Prompt restored" if ok else "⚠️ Prompt not found", show_alert=not ok)
            callback_answered = True
        elif action == "clear_ai":
            await self._clear_ai_key(None)
            section = "ai"
        elif action == "start_edit":
            self.pending_ai_key.pop(callback.from_user.id, None)
            self.pending_ai_model.pop(callback.from_user.id, None)
            self.pending_ai_prompt.discard(callback.from_user.id)
            self.pending_start_text.add(callback.from_user.id)
            section = "start"
            await callback.message.answer(
                "👋 Пришлите следующим сообщением новый текст приветствия для /start.\n\n"
                "Можно использовать несколько строк и эмодзи."
            )
        elif action == "start_reset":
            self.pending_start_text.discard(callback.from_user.id)
            self.pending_ai_prompt.discard(callback.from_user.id)
            await self._reset_start_text()
            section = "start"
        elif action == "modchat_help":
            section = "publish"
            if callback.message:
                me = await self.bot.get_me()
                username = me.username or "bot"
                await callback.message.answer(
                    "👥 Перенос чата модераторов\n\n"
                    "1. Добавьте этого бота в новую группу модераторов.\n"
                    f"2. От аккаунта владельца отправьте там /setmodchat@{username}\n"
                    "3. Новые заявки начнут приходить в новую группу.\n\n"
                    "В одну группу можно добавить несколько Boterator-ботов. "
                    "Каждый бот будет принимать голоса по своим карточкам и публиковать в свой канал."
                )
        else:
            await callback.answer()
            return

        await self._edit_settings(callback, section)
        if not callback_answered:
            if action in {"adj", "toggle", "freq_adj", "freq_preset", "freq_disable", "ai_score_adj", "duplicate_days_adj"}:
                await callback.answer("✅ Сохранено")
            else:
                await callback.answer()

    async def _edit_settings(self, callback: CallbackQuery, section: str) -> None:
        if not callback.message:
            return
        try:
            await callback.message.edit_text(
                await self._settings_text(section),
                reply_markup=await self._settings_keyboard(section),
            )
        except TelegramAPIError:
            LOGGER.debug("Unable to edit settings menu", exc_info=True)

    async def _settings_text(self, section: str) -> str:
        if section == "vote":
            return (
                "🗳️ Голосование\n\n"
                f"✅ Голосов для решения: {self.settings['votes']}\n"
                f"⏱️ Задержка публикации: {self.settings['delay']} мин.\n"
                f"⌛ Таймаут голосования: {self.settings['vote_timeout']} ч.\n"
                f"👁️ Показывать счет голосов: {_enabled_label(self.settings.get('public_vote', True))}\n"
                f"🔄 Разрешить смену голоса: {_enabled_label(self.settings.get('allow_vote_switch', False))}"
            )

        if section == "content":
            lines = ["🧩 Типы контента", ""]
            for key, label in CONTENT_LABELS.items():
                lines.append(f"{label}: {_enabled_label(self.settings['content_status'].get(key, False))}")
            return "\n".join(lines)

        if section == "ai":
            provider = self._ai_provider()
            key_state = "добавлен" if self._ai_configured() else "не добавлен"
            model = self._ai_model(provider)
            auto_state = _enabled_label(self._ai_auto_publish_enabled())
            auto_score = self._ai_auto_publish_min_score()
            prompt_state = self._ai_prompt_state()
            return (
                "🤖 AI-анализ\n\n"
                f"🟢 Статус: {_enabled_label(self._ai_enabled())}\n"
                f"🧠 Провайдер: {AI_PROVIDER_LABELS[provider]}\n"
                f"🔑 Ключ: {key_state}\n"
                f"🧬 Модель: {model}\n"
                f"🧾 System prompt: {prompt_state}\n"
                f"🚀 Автопубликация после таймаута: {auto_state}\n"
                f"🎯 Порог AI-одобрения: {auto_score}%\n\n"
                "🔒 Ключ можно добавить только владельцу в личке с ботом. "
                "Автопубликация сработает только если AI дал verdict publish, "
                "процент не ниже порога, а модераторы не нажимали против. "
                "System prompt влияет на AI-вердикт и автопубликацию. "
                "API-ключ не показывается в меню. Для ввода можно нажать кнопку ниже "
                "или отправить команду /setaikey openai <ключ>."
            )

        if section == "ai_prompt":
            prompt = self._ai_full_prompt()
            history = await self._prompt_history_preview()
            return (
                "🧾 AI system prompt\n\n"
                f"📌 Режим: {self._ai_prompt_state()}\n"
                f"🌐 Язык prompt: {LANGUAGE_LABELS[self._language()]}\n\n"
                "📄 Текущий полный prompt:\n"
                f"{prompt}\n\n"
                "✏️ При редактировании новый текст полностью заменит этот prompt. "
                "Сохраняйте строку verdict/publish chance, если используете AI-автопубликацию.\n\n"
                f"{history}"
            )

        if section == "ai_prompt_history":
            rows = await self._recent_prompt_history()
            if not rows:
                return "🕓 История prompt\n\nПока нет сохраненных версий."
            lines = ["🕓 История prompt", ""]
            for row in rows:
                created = row["created_at"].strftime("%Y-%m-%d %H:%M")
                lines.append(f"#{row['id']} | {created}\n{_preview_text(row['prompt'], 260)}")
                lines.append("")
            return "\n".join(lines).rstrip()

        if section in {"ai_model", "ai_models_1m", "ai_models_10m", "ai_models_gemini"}:
            provider = self._ai_provider()
            model = self._ai_model(provider)
            if section == "ai_models_1m":
                title = "🧠 OpenAI модели: 1M token group"
            elif section == "ai_models_10m":
                title = "⚡ OpenAI модели: 10M token group"
            elif section == "ai_models_gemini":
                title = "✨ Gemini модели"
            else:
                title = "🧬 Модель AI"
            return (
                f"{title}\n\n"
                f"🧠 Провайдер: {AI_PROVIDER_LABELS[provider]}\n"
                f"🧬 Текущая модель: {model}\n\n"
                "🔎 При выборе или ручном вводе бот проверит модель текущим API-ключом. "
                "Если модель недоступна аккаунту или не проходит тестовый запрос, она не сохранится."
            )

        if section == "publish":
            mode = self._publish_mode()
            return (
                "🚀 Публикация и права\n\n"
                f"👥 Чат модераторов: {self.moderator_chat_id}\n"
                f"📣 Канал: {self.target_channel}\n"
                f"🚀 Режим отправки: {PUBLISH_MODE_LABELS[mode]}\n"
                "🔒 Доступ к настройкам: только владелец в личке с ботом\n"
                f"🙋 Самоголосование: {_enabled_label(self.settings.get('selfvote', True))}\n"
                f"🏷️ Теги статуса в опросах: {_enabled_label(self.settings.get('tag_polls', False))}\n\n"
                "👥 Для переноса добавьте бота в новую группу и отправьте там "
                "/setmodchat@username_бота от аккаунта владельца."
            )

        if section == "language":
            language = self._language()
            return (
                "🌐 Язык публичных сообщений\n\n"
                f"🗣️ Текущий язык: {LANGUAGE_LABELS[language]}\n\n"
                "💬 Эта настройка меняет язык /start, подтверждения отправки, "
                "сообщений голосования в чате модераторов и уведомлений авторам. "
                "Меню настроек остается на русском."
            )

        if section == "text":
            return (
                "✍️ Текстовые лимиты\n\n"
                f"⬇️ Минимум символов: {self.settings['text_min']}\n"
                f"⬆️ Максимум символов: {self.settings['text_max']}"
            )

        if section == "freq":
            limit = self._freq_limit()
            if limit:
                status = "🟢 включено"
                label = _freq_limit_label(limit)
                count, hours = limit
            else:
                status = "⚪ выключено"
                label = "без ограничений"
                count, hours = FREQ_LIMIT_DEFAULT_COUNT, FREQ_LIMIT_DEFAULT_HOURS
            return (
                "🚦 Антиспам: лимит отправки\n\n"
                f"📍 Статус: {status}\n"
                f"📨 Сейчас: {label}\n"
                f"🔢 Сообщений: {count}\n"
                f"⏱️ Период: {hours} ч.\n\n"
                "Если пользователь превысит лимит, бот не примет новое сообщение на модерацию."
            )

        if section == "duplicates":
            return (
                "🔁 Проверка дублей\n\n"
                f"📍 Статус: {_enabled_label(self._duplicate_detection_enabled())}\n"
                f"🗓️ Период проверки: {self._duplicate_detection_days()} дн.\n"
                f"🎯 Порог похожести: {DUPLICATE_JACCARD_THRESHOLD}%\n\n"
                "Проверяются только текст и подписи к медиа внутри этого бота. "
                "Это локальная проверка через PostgreSQL, без AI и без расхода токенов. "
                "Найденный дубль не отклоняется автоматически: модераторы видят предупреждение и решают сами."
            )

        if section == "start":
            mode = "стандартное" if self._start_text_is_default() else "свое"
            return (
                "👋 Приветствие /start\n\n"
                f"🧭 Тип: {mode}\n"
                f"🌐 Язык публичных сообщений: {LANGUAGE_LABELS[self._language()]}\n\n"
                "📌 Текущий текст:\n"
                f"{_preview_text(self._start_text())}\n\n"
                "✏️ Нажмите кнопку изменения и пришлите новый текст следующим сообщением."
            )

        if section == "stats":
            return await self._stats_text()

        if section == "health":
            return (
                "🩺 Health и backup\n\n"
                "✅ Уведомления владельцу включены для критичных runtime-ошибок: "
                "падение polling, ошибки AI, недоступность чата модераторов и ошибки публикации.\n"
                "💾 Daily backup PostgreSQL на сервере настроен cron-задачей. "
                "По умолчанию файлы лежат в /opt/boterator/backups и хранятся 14 дней.\n\n"
                "Если backup-скрипт запускается вручную, команда описана в README."
            )

        enabled_content = [
            label
            for key, label in CONTENT_LABELS.items()
            if self.settings["content_status"].get(key, False)
        ]
        ai_state = (
            f"включен ({AI_PROVIDER_LABELS[self._ai_provider()]})"
            if self._ai_enabled()
            else ("ключ добавлен, но выключен" if self._ai_configured() else "ключ не добавлен")
        )
        freq_limit = self._freq_limit()
        return (
            "⚙️ Настройки бота\n\n"
            f"🗳️ Голосов для решения: {self.settings['votes']}\n"
            f"⏱️ Задержка публикации: {self.settings['delay']} мин.\n"
            f"⌛ Таймаут голосования: {self.settings['vote_timeout']} ч.\n"
            f"🌐 Язык публичных сообщений: {LANGUAGE_LABELS[self._language()]}\n"
            f"🤖 AI-анализ: {ai_state}\n"
            f"🚦 Лимит отправки: {_freq_limit_label(freq_limit) if freq_limit else 'выключен'}\n"
            f"🔁 Проверка дублей: {_enabled_label(self._duplicate_detection_enabled())}, {self._duplicate_detection_days()} дн.\n"
            f"🧩 Разрешенный контент: {', '.join(enabled_content) if enabled_content else 'ничего'}"
        )

    async def _settings_keyboard(self, section: str) -> InlineKeyboardMarkup:
        if section == "vote":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    _adjust_row("🗳️ Голоса", "votes", -1, 1, "vote"),
                    _adjust_row("⏱️ Задержка", "delay", -5, 5, "vote"),
                    _adjust_row("⌛ Таймаут", "vote_timeout", -1, 1, "vote"),
                    [_toggle_button("👁️ Счет голосов", "public_vote", self.settings.get("public_vote", True), "vote")],
                    [
                        _toggle_button(
                            "🔄 Смена голоса",
                            "allow_vote_switch",
                            self.settings.get("allow_vote_switch", False),
                            "vote",
                        )
                    ],
                    _back_row(),
                ]
            )

        if section == "content":
            buttons = [
                _toggle_button(label, f"content:{key}", self.settings["content_status"].get(key, False), "content")
                for key, label in CONTENT_LABELS.items()
            ]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append(_back_row())
            return InlineKeyboardMarkup(inline_keyboard=rows)

        if section == "ai":
            provider = self._ai_provider()
            current_model = self._ai_model(provider)
            ai_button_text = "🤖 AI: " + ("вкл" if self._ai_setting_enabled() else "выкл")
            if self._ai_setting_enabled() and not self._ai_configured():
                ai_button_text = "🔑 AI: нужен ключ"
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _ai_provider_button("OpenAI", "openai", provider),
                        _ai_provider_button("Gemini", "gemini", provider),
                    ],
                    [InlineKeyboardButton(text=ai_button_text, callback_data="s|toggle|ai|ai")],
                    [
                        _toggle_button(
                            "🚀 Автопубликация",
                            "ai_auto_publish",
                            self._ai_auto_publish_enabled(),
                            "ai",
                        )
                    ],
                    _ai_score_adjust_row(self._ai_auto_publish_min_score()),
                    [InlineKeyboardButton(text=f"🧬 Модель: {_short_model_label(current_model)}", callback_data="s|nav|ai_model")],
                    [InlineKeyboardButton(text="🧾 System prompt", callback_data="s|nav|ai_prompt")],
                    [InlineKeyboardButton(text="🔑 Добавить/заменить ключ", callback_data=f"s|ai_key|{provider}|ai")],
                    [InlineKeyboardButton(text="🧹 Удалить ключ", callback_data="s|clear_ai|ai")],
                    _back_row(),
                ]
            )

        if section == "ai_prompt":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Редактировать полный prompt", callback_data="s|ai_prompt_edit")],
                    [InlineKeyboardButton(text="🕓 История / rollback", callback_data="s|nav|ai_prompt_history")],
                    [InlineKeyboardButton(text="🔄 Сбросить к дефолтному", callback_data="s|ai_prompt_reset")],
                    [InlineKeyboardButton(text="⬅️ Назад к AI", callback_data="s|nav|ai")],
                ]
            )

        if section == "ai_prompt_history":
            rows = await self._recent_prompt_history()
            buttons = [
                [InlineKeyboardButton(text=f"↩️ Restore #{row['id']}", callback_data=f"s|ai_prompt_rollback|{row['id']}")]
                for row in rows[:5]
            ]
            buttons.append([InlineKeyboardButton(text="⬅️ Назад к prompt", callback_data="s|nav|ai_prompt")])
            return InlineKeyboardMarkup(inline_keyboard=buttons)

        if section == "ai_model":
            provider = self._ai_provider()
            if provider == "openai":
                rows = [
                    [InlineKeyboardButton(text="🧠 1M token group", callback_data="s|nav|ai_models_1m")],
                    [InlineKeyboardButton(text="⚡ 10M token group", callback_data="s|nav|ai_models_10m")],
                    [InlineKeyboardButton(text="✍️ Ввести свою модель", callback_data="s|ai_model_custom")],
                    [InlineKeyboardButton(text="⬅️ Назад к AI", callback_data="s|nav|ai")],
                ]
            else:
                rows = [
                    [InlineKeyboardButton(text=model, callback_data=f"s|ai_model|{model}")]
                    for model in GEMINI_MODEL_PRESETS
                ]
                rows.extend(
                    [
                        [InlineKeyboardButton(text="✍️ Ввести свою модель", callback_data="s|ai_model_custom")],
                        [InlineKeyboardButton(text="⬅️ Назад к AI", callback_data="s|nav|ai")],
                    ]
                )
            return InlineKeyboardMarkup(inline_keyboard=rows)

        if section == "ai_models_1m":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    *[[InlineKeyboardButton(text=model, callback_data=f"s|ai_model|{model}")] for model in OPENAI_OFFER_MODELS_1M],
                    [InlineKeyboardButton(text="⬅️ Назад к моделям", callback_data="s|nav|ai_model")],
                ]
            )

        if section == "ai_models_10m":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    *[[InlineKeyboardButton(text=model, callback_data=f"s|ai_model|{model}")] for model in OPENAI_OFFER_MODELS_10M],
                    [InlineKeyboardButton(text="⬅️ Назад к моделям", callback_data="s|nav|ai_model")],
                ]
            )

        if section == "publish":
            mode = self._publish_mode()
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👥 Как перенести чат", callback_data="s|modchat_help")],
                    [
                        _mode_button("🔁 Пересылать", "forward", mode),
                        _mode_button("📋 Копировать", "copy", mode),
                    ],
                    [_toggle_button("🙋 Самоголосование", "selfvote", self.settings.get("selfvote", True), "publish")],
                    [_toggle_button("🏷️ Теги статуса", "tag_polls", self.settings.get("tag_polls", False), "publish")],
                    _back_row(),
                ]
            )

        if section == "language":
            language = self._language()
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _language_button("🇷🇺 Русский", "ru", language),
                        _language_button("🇬🇧 English", "en", language),
                    ],
                    _back_row(),
                ]
            )

        if section == "text":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    _adjust_row("⬇️ Мин.", "text_min", -10, 10, "text"),
                    _adjust_row("⬆️ Макс.", "text_max", -100, 100, "text"),
                    _back_row(),
                ]
            )

        if section == "freq":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    _freq_adjust_row("📨 Сообщения", "count", -1, 1),
                    _freq_adjust_row("⏱️ 1 час", "hours", -1, 1),
                    _freq_adjust_row("⏱️ 24 часа", "hours", -24, 24),
                    [InlineKeyboardButton(text="✅ 2 сообщения за 24 ч.", callback_data="s|freq_preset")],
                    [InlineKeyboardButton(text="🚫 Выключить лимит", callback_data="s|freq_disable")],
                    _back_row(),
                ]
            )

        if section == "duplicates":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _toggle_button(
                            "🔁 Проверка дублей",
                            "duplicate_detection",
                            self._duplicate_detection_enabled(),
                            "duplicates",
                        )
                    ],
                    _duplicate_days_adjust_row("🗓️ 7 дн.", -7, 7),
                    _duplicate_days_adjust_row("🗓️ 30 дн.", -30, 30),
                    _duplicate_days_adjust_row("🗓️ 180 дн.", -180, 180),
                    _back_row(),
                ]
            )

        if section == "start":
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Изменить приветствие", callback_data="s|start_edit")],
                    [InlineKeyboardButton(text="🔄 Сбросить к стандартному", callback_data="s|start_reset")],
                    _back_row(),
                ]
            )

        if section in {"stats", "health"}:
            return InlineKeyboardMarkup(inline_keyboard=[_back_row()])

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗳️ Голосование", callback_data="s|nav|vote"),
                    InlineKeyboardButton(text="🧩 Контент", callback_data="s|nav|content"),
                ],
                [
                    InlineKeyboardButton(text="🤖 AI-анализ", callback_data="s|nav|ai"),
                    InlineKeyboardButton(text="🚀 Публикация", callback_data="s|nav|publish"),
                ],
                [
                    InlineKeyboardButton(text="🌐 Язык", callback_data="s|nav|language"),
                    InlineKeyboardButton(text="👋 Приветствие", callback_data="s|nav|start"),
                ],
                [
                    InlineKeyboardButton(text="✍️ Лимиты текста", callback_data="s|nav|text"),
                    InlineKeyboardButton(text="🚦 Антиспам", callback_data="s|nav|freq"),
                ],
                [
                    InlineKeyboardButton(text="🔁 Дубли", callback_data="s|nav|duplicates"),
                    InlineKeyboardButton(text="📊 Статистика", callback_data="s|nav|stats"),
                ],
                [
                    InlineKeyboardButton(text="🩺 Backup/health", callback_data="s|nav|health"),
                ],
                [InlineKeyboardButton(text="✅ Закрыть", callback_data="s|close")],
            ]
        )

    async def _stats_text(self) -> str:
        row = await self.pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_published) AS published,
                COUNT(*) FILTER (WHERE is_voting_fail) AS rejected,
                COUNT(*) FILTER (WHERE is_voting_success AND NOT is_published) AS queued,
                COUNT(*) FILTER (
                    WHERE NOT is_voting_fail
                        AND NOT is_voting_success
                        AND NOT is_published
                ) AS review,
                COUNT(*) FILTER (WHERE ai_auto_approved) AS auto_approved,
                COUNT(*) FILTER (WHERE duplicate_score IS NOT NULL) AS duplicates,
                ROUND(AVG(ai_publish_score) FILTER (WHERE ai_publish_score IS NOT NULL)) AS avg_ai_score
            FROM incoming_messages
            WHERE bot_id = $1
            """,
            self.bot_id,
        )
        reason_rows = await self.pool.fetch(
            """
            SELECT COALESCE(reject_reason, 'no_reason') AS reason, COUNT(*) AS count
            FROM incoming_messages
            WHERE bot_id = $1 AND is_voting_fail = TRUE
            GROUP BY COALESCE(reject_reason, 'no_reason')
            ORDER BY count DESC, reason
            """,
            self.bot_id,
        )
        lines = [
            "📊 Статистика",
            "",
            f"📨 Всего: {row['total']}",
            f"🛡️ На модерации: {row['review']}",
            f"⏳ Одобрено, ждет публикации: {row['queued']}",
            f"✅ Опубликовано: {row['published']}",
            f"🤖 Автоодобрено AI: {row['auto_approved']}",
            f"❌ Отклонено: {row['rejected']}",
            f"🔁 Возможные дубли: {row['duplicates']}",
            f"🎯 Средний AI score: {int(row['avg_ai_score']) if row['avg_ai_score'] is not None else 'n/a'}%",
        ]
        if reason_rows:
            lines.extend(["", "🚫 Причины отклонений:"])
            for reason_row in reason_rows:
                reason = reason_row["reason"]
                label = "No reason" if reason == "no_reason" else (_reject_reason_label(reason) or str(reason))
                lines.append(f"- {label}: {reason_row['count']}")
        return "\n".join(lines)

    async def _user_stats_text(self, user_id: int) -> str:
        row = await self.pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_published) AS published,
                COUNT(*) FILTER (WHERE is_voting_fail) AS rejected,
                COUNT(*) FILTER (WHERE is_voting_success AND NOT is_published) AS queued,
                COUNT(*) FILTER (
                    WHERE NOT is_voting_fail
                        AND NOT is_voting_success
                        AND NOT is_published
                ) AS review
            FROM incoming_messages
            WHERE bot_id = $1 AND owner_id = $2
            """,
            self.bot_id,
            user_id,
        )
        return "\n".join(
            [
                self._t("user_stats_title"),
                "",
                self._t("user_stats_total", count=row["total"]),
                self._t("user_stats_review", count=row["review"]),
                self._t("user_stats_queued", count=row["queued"]),
                self._t("user_stats_published", count=row["published"]),
                self._t("user_stats_rejected", count=row["rejected"]),
            ]
        )

    async def _adjust_numeric_setting(self, setting: str, delta: int) -> None:
        if setting not in NUMERIC_SETTINGS:
            return
        minimum, maximum = NUMERIC_SETTINGS[setting]
        value = max(minimum, min(maximum, int(self.settings.get(setting, minimum)) + delta))
        if setting == "text_min":
            value = min(value, int(self.settings.get("text_max", maximum)))
        elif setting == "text_max":
            value = max(value, int(self.settings.get("text_min", minimum)))
        await self._update_setting(setting, value)

    async def _adjust_freq_limit(self, field: str, delta: int) -> None:
        limit = self._freq_limit() or (FREQ_LIMIT_DEFAULT_COUNT, FREQ_LIMIT_DEFAULT_HOURS)
        count, hours = limit
        if field == "count":
            count = max(1, min(FREQ_LIMIT_MAX_COUNT, count + delta))
        elif field == "hours":
            hours = max(1, min(FREQ_LIMIT_MAX_HOURS, hours + delta))
        else:
            return
        await self._set_freq_limit(count, hours)

    async def _set_freq_limit(self, count: int, hours: int) -> None:
        count = max(1, min(FREQ_LIMIT_MAX_COUNT, int(count)))
        hours = max(1, min(FREQ_LIMIT_MAX_HOURS, int(hours)))
        await self._update_setting("msg_freq_limit", {"count": count, "hours": hours})

    async def _set_duplicate_detection_enabled(self, enabled: bool) -> None:
        duplicate_settings = dict(self.settings.get("duplicate_detection", {}))
        duplicate_settings["enabled"] = enabled
        duplicate_settings.setdefault("days", DUPLICATE_DEFAULT_DAYS)
        await self._update_setting("duplicate_detection", duplicate_settings)

    async def _adjust_duplicate_detection_days(self, delta: int) -> None:
        duplicate_settings = dict(self.settings.get("duplicate_detection", {}))
        value = self._duplicate_detection_days() + delta
        duplicate_settings["days"] = max(DUPLICATE_MIN_DAYS, min(DUPLICATE_MAX_DAYS, value))
        duplicate_settings.setdefault("enabled", self._duplicate_detection_enabled())
        await self._update_setting("duplicate_detection", duplicate_settings)

    async def _toggle_content_setting(self, content_key: str) -> None:
        if content_key not in self.settings["content_status"]:
            return
        content_settings = dict(self.settings["content_status"])
        content_settings[content_key] = not content_settings.get(content_key, False)
        await self._update_setting("content_status", content_settings)

    async def _toggle_boolean_setting(self, key: str) -> None:
        if key not in TOGGLE_LABELS:
            return
        await self._update_setting(key, not bool(self.settings.get(key, False)))

    async def _set_ai_enabled(self, enabled: bool) -> None:
        ai_settings = dict(self.settings.get("ai", {}))
        ai_settings["enabled"] = enabled
        if not enabled:
            ai_settings["auto_publish_after_timeout"] = False
        await self._update_setting("ai", ai_settings)

    async def _set_ai_auto_publish(self, enabled: bool) -> None:
        ai_settings = dict(self.settings.get("ai", {}))
        ai_settings["auto_publish_after_timeout"] = enabled
        ai_settings.setdefault("auto_publish_min_score", AI_AUTO_PUBLISH_DEFAULT_SCORE)
        await self._update_setting("ai", ai_settings)

    async def _adjust_ai_auto_publish_score(self, delta: int) -> None:
        ai_settings = dict(self.settings.get("ai", {}))
        value = self._ai_auto_publish_min_score() + delta
        ai_settings["auto_publish_min_score"] = max(
            AI_AUTO_PUBLISH_MIN_SCORE,
            min(AI_AUTO_PUBLISH_MAX_SCORE, value),
        )
        await self._update_setting("ai", ai_settings)

    async def _set_ai_provider(self, provider: str) -> None:
        if provider not in AI_PROVIDER_LABELS:
            return
        ai_settings = dict(self.settings.get("ai", {}))
        ai_settings["provider"] = provider
        if not self.analyzer.is_configured(ai_settings):
            ai_settings["enabled"] = False
        await self._update_setting("ai", ai_settings)

    async def _store_ai_key(self, provider: str, api_key: str) -> None:
        if provider not in AI_PROVIDER_LABELS or not api_key:
            return
        ai_settings = dict(self.settings.get("ai", {}))
        ai_settings["provider"] = provider
        ai_settings["enabled"] = True
        ai_settings[f"{provider}_api_key"] = api_key
        if provider == "openai" and not ai_settings.get("openai_model"):
            ai_settings["openai_model"] = "gpt-5-mini"
        if provider == "gemini" and not ai_settings.get("gemini_model"):
            ai_settings["gemini_model"] = "gemini-3.5-flash"
        await self._update_setting("ai", ai_settings)

    async def _set_ai_model_checked(self, provider: str, model: str) -> tuple[bool, str]:
        provider = provider.lower()
        model = model.strip()
        if provider not in AI_PROVIDER_LABELS:
            return False, "Неизвестный AI-провайдер."
        if not _valid_model_id(model):
            return False, "ID модели должен быть без пробелов, до 96 символов."

        ai_settings = dict(self.settings.get("ai", {}))
        ai_settings["provider"] = provider
        validation = await self.analyzer.validate_model(provider, model, ai_settings)
        if not validation.ok:
            return False, validation.message

        ai_settings[f"{provider}_model"] = model.removeprefix("models/") if provider == "gemini" else model
        await self._update_setting("ai", ai_settings)
        return True, validation.message

    async def _clear_ai_key(self, provider: str | None) -> None:
        ai_settings = dict(self.settings.get("ai", {}))
        providers = AI_PROVIDER_LABELS if provider is None else {provider: AI_PROVIDER_LABELS[provider]}
        for key in providers:
            ai_settings[f"{key}_api_key"] = None
        ai_settings["enabled"] = False
        await self._update_setting("ai", ai_settings)

    async def _set_ai_prompt(self, prompt: str) -> tuple[bool, str]:
        prompt = prompt.strip()
        if not prompt:
            return False, "AI prompt не должен быть пустым."
        if len(prompt) > AI_PROMPT_MAX_LENGTH:
            return False, f"AI prompt слишком длинный. Максимум {AI_PROMPT_MAX_LENGTH} символов."
        ai_settings = dict(self.settings.get("ai", {}))
        await self._save_current_prompt_history(ai_settings)
        ai_settings["system_prompt"] = prompt
        ai_settings["custom_prompt"] = None
        await self._update_setting("ai", ai_settings)
        return True, "Полный AI system prompt сохранен."

    async def _reset_ai_prompt(self) -> None:
        ai_settings = dict(self.settings.get("ai", {}))
        await self._save_current_prompt_history(ai_settings)
        ai_settings["system_prompt"] = None
        ai_settings["custom_prompt"] = None
        await self._update_setting("ai", ai_settings)

    async def _save_current_prompt_history(self, ai_settings: dict[str, Any]) -> None:
        prompt = ai_settings.get("system_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return
        prompt = prompt.strip()
        last = await self.pool.fetchval(
            """
            SELECT prompt
            FROM ai_prompt_history
            WHERE bot_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            self.bot_id,
        )
        if last == prompt:
            return
        await self.pool.execute(
            """
            INSERT INTO ai_prompt_history (bot_id, prompt)
            VALUES ($1, $2)
            """,
            self.bot_id,
            prompt,
        )

    async def _recent_prompt_history(self) -> list[asyncpg.Record]:
        rows = await self.pool.fetch(
            """
            SELECT id, created_at, prompt
            FROM ai_prompt_history
            WHERE bot_id = $1
            ORDER BY created_at DESC
            LIMIT 5
            """,
            self.bot_id,
        )
        return list(rows)

    async def _prompt_history_preview(self) -> str:
        rows = await self._recent_prompt_history()
        if not rows:
            return "🕓 История prompt: пусто"
        latest = rows[0]["created_at"].strftime("%Y-%m-%d %H:%M")
        return f"🕓 История prompt: {len(rows)} последних, свежая версия #{rows[0]['id']} от {latest}"

    async def _rollback_ai_prompt(self, history_id: int) -> bool:
        prompt = await self.pool.fetchval(
            """
            SELECT prompt
            FROM ai_prompt_history
            WHERE bot_id = $1 AND id = $2
            """,
            self.bot_id,
            history_id,
        )
        if not prompt:
            return False
        ai_settings = dict(self.settings.get("ai", {}))
        await self._save_current_prompt_history(ai_settings)
        ai_settings["system_prompt"] = str(prompt)
        ai_settings["custom_prompt"] = None
        await self._update_setting("ai", ai_settings)
        return True

    async def _handle_pending_ai_key(self, message: Message) -> bool:
        if not message.from_user or not self._can_manage(message):
            return False
        provider = self.pending_ai_key.pop(message.from_user.id, None)
        if not provider:
            return False
        api_key = (message.text or "").strip()
        if len(api_key) < 20:
            await message.answer("⚠️ Ключ выглядит слишком коротким. Нажмите кнопку добавления ключа еще раз.")
            return True
        await self._store_ai_key(provider, api_key)
        await self._delete_sensitive_message(message)
        await message.answer(f"✅ Ключ {AI_PROVIDER_LABELS[provider]} сохранен. AI-анализ включен.")
        return True

    async def _handle_pending_ai_model(self, message: Message) -> bool:
        if not message.from_user or not self._can_manage(message):
            return False
        provider = self.pending_ai_model.get(message.from_user.id)
        if not provider:
            return False

        model = (message.text or "").strip()
        if not model or model.startswith("/"):
            await message.answer("⚠️ Пришлите ID модели обычным текстом.")
            return True

        await message.answer(f"🔎 Проверяю модель {model}...")
        ok, response = await self._set_ai_model_checked(provider, model)
        if ok:
            self.pending_ai_model.pop(message.from_user.id, None)
        await message.answer(
            ("✅ " if ok else "⚠️ ") + response,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🧬 Открыть модели", callback_data="s|nav|ai_model")]]
            ),
        )
        return True

    async def _handle_pending_ai_prompt(self, message: Message) -> bool:
        if not message.from_user or not self._can_manage(message):
            return False
        if message.from_user.id not in self.pending_ai_prompt:
            return False

        text = (message.text or message.caption or "").strip()
        if not text:
            await message.answer("⚠️ Пришлите AI system prompt обычным текстовым сообщением.")
            return True

        ok, response = await self._set_ai_prompt(text)
        if ok:
            self.pending_ai_prompt.discard(message.from_user.id)
        await message.answer(
            ("✅ " if ok else "⚠️ ") + response,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🧾 Открыть prompt", callback_data="s|nav|ai_prompt")]]
            ),
        )
        return True

    async def _handle_pending_start_text(self, message: Message) -> bool:
        if not message.from_user or not self._can_manage(message):
            return False
        if message.from_user.id not in self.pending_start_text:
            return False

        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            await message.answer("⚠️ Пришлите приветствие обычным текстовым сообщением.")
            return True
        if len(text) > 4096:
            await message.answer("⚠️ Приветствие слишком длинное. Максимум Telegram - 4096 символов.")
            return True

        self.pending_start_text.discard(message.from_user.id)
        await self._set_start_text(text)
        await message.answer(
            "✅ Приветствие /start обновлено.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="👋 Открыть приветствие", callback_data="s|nav|start")]]
            ),
        )
        return True

    async def _delete_sensitive_message(self, message: Message) -> None:
        try:
            await message.delete()
        except TelegramAPIError:
            LOGGER.debug("Unable to delete sensitive message", exc_info=True)

    def _ai_provider(self) -> str:
        ai_settings = self.settings.get("ai", {})
        provider = self.analyzer.resolve_provider(ai_settings)
        return provider if provider in AI_PROVIDER_LABELS else "openai"

    def _ai_configured(self) -> bool:
        return self.analyzer.is_configured(self.settings.get("ai", {}))

    def _ai_model(self, provider: str) -> str:
        ai_settings = self.settings.get("ai", {})
        if provider == "gemini":
            return str(ai_settings.get("gemini_model") or "gemini-3.5-flash")
        return str(ai_settings.get("openai_model") or "gpt-5-mini")

    def _ai_full_prompt(self) -> str:
        return build_system_prompt(self._language(), self.settings.get("ai", {}))

    def _ai_prompt_state(self) -> str:
        ai_settings = self.settings.get("ai", {})
        if isinstance(ai_settings.get("system_prompt"), str) and ai_settings["system_prompt"].strip():
            return "изменен вручную"
        if isinstance(ai_settings.get("custom_prompt"), str) and ai_settings["custom_prompt"].strip():
            return "дефолтный + старый контекст"
        return "дефолтный"

    def _ai_auto_publish_enabled(self) -> bool:
        return bool(self.settings.get("ai", {}).get("auto_publish_after_timeout", False))

    def _ai_auto_publish_min_score(self) -> int:
        try:
            value = int(self.settings.get("ai", {}).get("auto_publish_min_score", AI_AUTO_PUBLISH_DEFAULT_SCORE))
        except (TypeError, ValueError):
            value = AI_AUTO_PUBLISH_DEFAULT_SCORE
        return max(AI_AUTO_PUBLISH_MIN_SCORE, min(AI_AUTO_PUBLISH_MAX_SCORE, value))

    async def _set_publish_mode(self, mode: str) -> None:
        if mode not in PUBLISH_MODE_LABELS:
            return
        await self._update_setting("publish_mode", mode)

    def _publish_mode(self) -> str:
        mode = str(self.settings.get("publish_mode") or self.publish_mode).lower()
        return mode if mode in PUBLISH_MODE_LABELS else "forward"

    async def _set_language(self, language: str) -> None:
        if language not in LANGUAGE_LABELS:
            return
        await self._update_setting("language", language)

    async def _set_start_text(self, text: str) -> None:
        await self._update_setting("start", text)

    async def _reset_start_text(self) -> None:
        await self._update_setting("start", DEFAULT_START_TEXTS[self._language()])

    def _freq_limit(self) -> tuple[int, int] | None:
        raw = self.settings.get("msg_freq_limit")
        if not raw:
            return None

        count: Any
        hours: Any
        if isinstance(raw, dict):
            count = raw.get("count") or raw.get("messages") or raw.get("message_count")
            hours = raw.get("hours") or raw.get("period_hours")
            if hours is None and raw.get("days") is not None:
                hours = int(raw["days"]) * 24
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            count = raw[0]
            hours = int(raw[1]) * 24
        else:
            return None

        try:
            count_int = int(count)
            hours_int = int(hours)
        except (TypeError, ValueError):
            return None

        if count_int < 1 or hours_int < 1:
            return None
        return (
            min(count_int, FREQ_LIMIT_MAX_COUNT),
            min(hours_int, FREQ_LIMIT_MAX_HOURS),
        )

    def _language(self) -> str:
        return normalize_language(self.settings.get("language"))

    def _t(self, key: str, **kwargs: Any) -> str:
        return translate(self._language(), key, **kwargs)

    def _start_text(self) -> str:
        return default_text(self._language(), self.settings.get("start"), DEFAULT_START_TEXTS)

    def _start_text_is_default(self) -> bool:
        value = self.settings.get("start")
        return not value or value in DEFAULT_START_TEXTS.values()

    def _ai_enabled(self) -> bool:
        return bool(self._ai_setting_enabled() and self._ai_configured())

    def _ai_setting_enabled(self) -> bool:
        return bool(self.settings.get("ai", {}).get("enabled", False))

    async def _rate_limit_exceeded(self, user_id: int) -> bool:
        limit = self._freq_limit()
        if not limit:
            return False
        count, hours = limit
        used = await self.pool.fetchval(
            """
            SELECT COUNT(*)
            FROM incoming_messages
            WHERE bot_id = $1
                AND owner_id = $2
                AND created_at >= NOW() - ($3::int * interval '1 hour')
            """,
            self.bot_id,
            user_id,
            hours,
        )
        return int(used or 0) >= count

    def _rate_limit_message(self) -> str:
        limit = self._freq_limit() or (FREQ_LIMIT_DEFAULT_COUNT, FREQ_LIMIT_DEFAULT_HOURS)
        count, hours = limit
        return self._t("rate_limit_exceeded", count=count, hours=hours)

    def _ai_auto_publish_allowed(self, row: asyncpg.Record, counts: dict[str, int]) -> bool:
        if not self._ai_auto_publish_enabled():
            return False
        if counts.get("no", 0) > 0:
            return False
        if row["ai_recommendation"] != "publish":
            return False
        score = row["ai_publish_score"]
        if score is None:
            return False
        return int(score) >= self._ai_auto_publish_min_score()

    def _duplicate_detection_enabled(self) -> bool:
        settings = self.settings.get("duplicate_detection", {})
        if isinstance(settings, dict):
            return bool(settings.get("enabled", True))
        return True

    def _duplicate_detection_days(self) -> int:
        settings = self.settings.get("duplicate_detection", {})
        if not isinstance(settings, dict):
            return DUPLICATE_DEFAULT_DAYS
        try:
            value = int(settings.get("days", DUPLICATE_DEFAULT_DAYS))
        except (TypeError, ValueError):
            value = DUPLICATE_DEFAULT_DAYS
        return max(DUPLICATE_MIN_DAYS, min(DUPLICATE_MAX_DAYS, value))

    async def _find_duplicate(
        self,
        normalized_text: str | None,
        media_unique_id: str | None,
        original_chat_id: int,
        message_id: int,
    ) -> dict[str, int] | None:
        if not self._duplicate_detection_enabled():
            return None
        if media_unique_id:
            row = await self.pool.fetchrow(
                """
                SELECT original_chat_id, id
                FROM incoming_messages
                WHERE bot_id = $1
                    AND media_unique_id = $2
                    AND created_at >= NOW() - ($3::int * interval '1 day')
                    AND NOT (original_chat_id = $4 AND id = $5)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                self.bot_id,
                media_unique_id,
                self._duplicate_detection_days(),
                original_chat_id,
                message_id,
            )
            if row:
                return {
                    "original_chat_id": int(row["original_chat_id"]),
                    "id": int(row["id"]),
                    "score": DUPLICATE_EXACT_SCORE,
                }

        if not normalized_text:
            return None
        if len(normalized_text) < DUPLICATE_MIN_CHARS:
            return None

        rows = await self.pool.fetch(
            """
            SELECT original_chat_id, id, normalized_text
            FROM incoming_messages
            WHERE bot_id = $1
                AND normalized_text IS NOT NULL
                AND created_at >= NOW() - ($2::int * interval '1 day')
                AND NOT (original_chat_id = $3 AND id = $4)
            ORDER BY created_at DESC
            LIMIT 1000
            """,
            self.bot_id,
            self._duplicate_detection_days(),
            original_chat_id,
            message_id,
        )

        best: dict[str, int] | None = None
        for row in rows:
            candidate = row["normalized_text"] or ""
            score = _text_similarity_score(normalized_text, candidate)
            if score >= DUPLICATE_JACCARD_THRESHOLD and (best is None or score > best["score"]):
                best = {
                    "original_chat_id": int(row["original_chat_id"]),
                    "id": int(row["id"]),
                    "score": score,
                }
                if score == DUPLICATE_EXACT_SCORE:
                    break
        return best

    async def _handle_incoming_message(self, message: Message) -> None:
        if _chat_type(message) != ChatType.PRIVATE.value:
            return
        if not message.from_user:
            return
        if (message.text or "").startswith("/"):
            await message.answer(self._t("unknown_command"))
            return

        allowed = await self._upsert_user(message)
        if not allowed:
            await message.answer(self._t("access_denied"))
            return

        if await self._rate_limit_exceeded(message.from_user.id):
            await message.answer(self._rate_limit_message())
            return

        content_key = _content_key(message)
        if not content_key:
            await message.answer(self._t("unsupported_content"))
            return
        if not self.settings["content_status"].get(content_key, False):
            await message.answer(self._t("content_disabled", content=content_label(self._language(), content_key)))
            return

        text = message.text or message.caption or ""
        if content_key == "text" and not (self.settings["text_min"] <= len(text.strip()) <= self.settings["text_max"]):
            await message.answer(
                self._t("text_limits", min=self.settings["text_min"], max=self.settings["text_max"])
            )
            return

        key = (message.chat.id, message.message_id)
        self.pending[key] = message
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=self._t("button_submit"), callback_data=f"confirm|{key[0]}|{key[1]}"),
                    InlineKeyboardButton(text=self._t("button_cancel"), callback_data=f"cancel|{key[0]}|{key[1]}"),
                ]
            ]
        )
        await message.answer(self._t("confirm_submit"), reply_markup=keyboard, reply_to_message_id=message.message_id)

    async def _handle_confirmation(self, callback: CallbackQuery) -> None:
        key = _parse_message_key(callback.data)
        if not key or not callback.from_user:
            await callback.answer()
            return
        message = self.pending.pop(key, None)
        if not message:
            await callback.answer(self._t("repeat_submission"), show_alert=True)
            return

        if await self._rate_limit_exceeded(callback.from_user.id):
            if callback.message:
                await callback.message.edit_text(self._rate_limit_message())
            await callback.answer()
            return

        if callback.message:
            await callback.message.edit_text(self._t("sending_to_mods"))
        await callback.answer()

        raw_message = _message_dump(message)
        normalized_text = _normalize_submission_text(message)
        media_unique_id = _media_unique_id(message)
        duplicate = await self._find_duplicate(normalized_text, media_unique_id, message.chat.id, message.message_id)
        ai_enabled = self._ai_enabled()
        ai_provider = self._ai_provider() if ai_enabled else None
        ai_model = self._ai_model(ai_provider) if ai_provider else None

        await self.pool.execute(
            """
            INSERT INTO incoming_messages (
                id, original_chat_id, owner_id, bot_id, message, ai_provider, ai_model, ai_analysis,
                ai_recommendation, ai_publish_score, ai_auto_approved, normalized_text, media_unique_id,
                duplicate_of_original_chat_id, duplicate_of_message_id, duplicate_score
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, FALSE, $11, $12, $13, $14, $15)
            ON CONFLICT (bot_id, original_chat_id, id) DO UPDATE SET
                message = EXCLUDED.message,
                ai_provider = EXCLUDED.ai_provider,
                ai_model = EXCLUDED.ai_model,
                ai_analysis = EXCLUDED.ai_analysis,
                ai_recommendation = EXCLUDED.ai_recommendation,
                ai_publish_score = EXCLUDED.ai_publish_score,
                normalized_text = EXCLUDED.normalized_text,
                media_unique_id = EXCLUDED.media_unique_id,
                duplicate_of_original_chat_id = EXCLUDED.duplicate_of_original_chat_id,
                duplicate_of_message_id = EXCLUDED.duplicate_of_message_id,
                duplicate_score = EXCLUDED.duplicate_score,
                ai_auto_approved = FALSE,
                reject_reason = NULL,
                rejected_by = NULL,
                rejected_at = NULL,
                is_voting_fail = FALSE,
                is_voting_success = FALSE,
                is_published = FALSE
            """,
            message.message_id,
            message.chat.id,
            callback.from_user.id,
            self.bot_id,
            raw_message,
            ai_provider,
            ai_model,
            None,
            None,
            None,
            normalized_text,
            media_unique_id,
            duplicate["original_chat_id"] if duplicate else None,
            duplicate["id"] if duplicate else None,
            duplicate["score"] if duplicate else None,
        )

        try:
            moderation_copy = await self._copy_or_forward(self.moderator_chat_id, message.chat.id, message.message_id)
            moderation_fwd_id = moderation_copy.message_id
            status_text, keyboard = await self._verification_message(message.message_id, message.chat.id, False)
            moderation_status = await self._send_moderation_status(status_text, keyboard, moderation_fwd_id)
        except TelegramAPIError:
            LOGGER.exception("Unable to send moderation request for bot #%s", self.bot_id)
            await self._notify_health(
                "moderation_send",
                "⚠️ Could not send a submission to the moderator chat. Check bot permissions and moderator chat.",
            )
            await self.pool.execute(
                "UPDATE incoming_messages SET is_voting_fail = TRUE WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3",
                self.bot_id,
                message.chat.id,
                message.message_id,
            )
            if callback.message:
                await callback.message.edit_text(self._t("send_to_mods_failed"))
            return

        await self.pool.execute(
            """
            UPDATE incoming_messages
            SET moderation_message_id = $1,
                moderation_fwd_message_id = $2
            WHERE bot_id = $3 AND original_chat_id = $4 AND id = $5
            """,
            moderation_status.message_id,
            moderation_fwd_id,
            self.bot_id,
            message.chat.id,
            message.message_id,
        )
        await self.pool.execute(
            "UPDATE registered_bots SET last_moderation_message_at = NOW() WHERE id = $1",
            self.bot_id,
        )
        if callback.message:
            await callback.message.edit_text(self._t("sent_to_moderation"))
        if ai_enabled:
            asyncio.create_task(
                self._run_ai_analysis(message, message.chat.id, message.message_id, ai_provider, ai_model),
                name=f"slave-{self.bot_id}-ai-{message.chat.id}-{message.message_id}",
            )

    async def _run_ai_analysis(
        self,
        message: Message,
        original_chat_id: int,
        message_id: int,
        provider: str | None,
        model: str | None,
    ) -> None:
        if not provider or not model:
            return
        ai_settings = dict(self.settings.get("ai", {}))
        ai_settings["provider"] = provider
        ai_settings[f"{provider}_model"] = model

        try:
            ai_result = await asyncio.wait_for(
                self.analyzer.analyze(self.bot, message, ai_settings, self._language()),
                timeout=AI_ANALYSIS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "AI analysis timed out for bot #%s message #%s/%s",
                self.bot_id,
                original_chat_id,
                message_id,
            )
            ai_result = None
            await self._notify_health("ai_timeout", "⚠️ AI analysis timed out. Moderation still works, but AI result is missing.")
        except Exception:
            LOGGER.exception(
                "AI analysis task failed for bot #%s message #%s/%s",
                self.bot_id,
                original_chat_id,
                message_id,
            )
            ai_result = None
            await self._notify_health("ai_failed", "⚠️ AI analysis failed. Moderation still works, but AI result is missing.")

        analysis_text = ai_result.text if ai_result else self._t("ai_analysis_failed")
        try:
            await self.pool.execute(
                """
                UPDATE incoming_messages
                SET ai_provider = $1,
                    ai_model = $2,
                    ai_analysis = $3,
                    ai_recommendation = $4,
                    ai_publish_score = $5
                WHERE bot_id = $6 AND original_chat_id = $7 AND id = $8
                """,
                provider,
                model,
                analysis_text,
                ai_result.recommendation if ai_result else None,
                ai_result.publish_score if ai_result else None,
                self.bot_id,
                original_chat_id,
                message_id,
            )
            await self._refresh_moderation_status(
                original_chat_id,
                message_id,
                await self._message_finished(original_chat_id, message_id),
            )
        except Exception:
            LOGGER.exception(
                "Unable to store AI analysis for bot #%s message #%s/%s",
                self.bot_id,
                original_chat_id,
                message_id,
            )

    async def _handle_vote(self, callback: CallbackQuery) -> None:
        parsed = _parse_vote_callback(callback.data)
        if not parsed or not callback.message or callback.message.chat.id != self.moderator_chat_id:
            await callback.answer()
            return
        original_chat_id, message_id, vote_yes = parsed
        user_id = callback.from_user.id

        if not self.settings.get("selfvote") and int(original_chat_id) == user_id and user_id != self.moderator_chat_id:
            await callback.answer(self._t("self_vote_denied"), show_alert=True)
            return

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT is_voting_fail, is_voting_success, is_published
                FROM incoming_messages
                WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
                """,
                self.bot_id,
                original_chat_id,
                message_id,
            )
            if not row or row["is_voting_fail"] or row["is_voting_success"] or row["is_published"]:
                await callback.answer(self._t("voting_closed"))
                await self._refresh_moderation_status(original_chat_id, message_id, True)
                return

            previous = await conn.fetchval(
                """
                SELECT vote_yes
                FROM votes_history
                WHERE bot_id = $1 AND user_id = $2 AND message_id = $3 AND original_chat_id = $4
                """,
                self.bot_id,
                user_id,
                message_id,
                original_chat_id,
            )
            if previous is not None and (previous == vote_yes or not self.settings.get("allow_vote_switch")):
                await callback.answer(self._t("vote_already_counted"))
                return

            await conn.execute(
                """
                INSERT INTO votes_history (bot_id, user_id, message_id, original_chat_id, vote_yes)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (bot_id, user_id, message_id, original_chat_id)
                DO UPDATE SET vote_yes = EXCLUDED.vote_yes
                """,
                self.bot_id,
                user_id,
                message_id,
                original_chat_id,
                vote_yes,
            )

            counts = await self._vote_counts(conn, original_chat_id, message_id)
            if counts["yes"] >= self.settings["votes"]:
                await conn.execute(
                    """
                    UPDATE incoming_messages
                    SET is_voting_success = TRUE
                    WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3 AND is_voting_fail = FALSE
                    """,
                    self.bot_id,
                    original_chat_id,
                    message_id,
                )
                await self._notify_author(original_chat_id, message_id, self._t("author_approved"))
                await callback.answer(self._t("vote_counted_approved"))
            elif counts["no"] >= self.settings["votes"]:
                await conn.execute(
                    """
                    UPDATE incoming_messages
                    SET is_voting_fail = TRUE,
                        reject_reason = COALESCE(reject_reason, 'no_vote'),
                        rejected_by = COALESCE(rejected_by, $4),
                        rejected_at = COALESCE(rejected_at, NOW())
                    WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3 AND is_voting_success = FALSE
                    """,
                    self.bot_id,
                    original_chat_id,
                    message_id,
                    user_id,
                )
                await self._notify_rejection_author(original_chat_id, message_id, "author_rejected")
                await callback.answer(self._t("vote_counted_rejected"))
            else:
                await callback.answer(self._t("vote_counted"))

        finished = counts["yes"] >= self.settings["votes"] or counts["no"] >= self.settings["votes"]
        await self._refresh_moderation_status(original_chat_id, message_id, finished)
        if counts["yes"] >= self.settings["votes"]:
            await self.publish_ready_messages()

    async def _handle_reject(self, callback: CallbackQuery) -> None:
        parsed = _parse_reject_callback(callback.data)
        if not parsed or not callback.message or callback.message.chat.id != self.moderator_chat_id:
            await callback.answer()
            return
        original_chat_id, message_id = parsed
        await callback.message.answer(
            "🚫 Select rejection reason:",
            reply_markup=_reject_reason_keyboard(original_chat_id, message_id),
            reply_to_message_id=callback.message.message_id,
        )
        await callback.answer()

    async def _handle_reject_reason(self, callback: CallbackQuery) -> None:
        parsed = _parse_reject_reason_callback(callback.data)
        if not parsed or not callback.message or callback.message.chat.id != self.moderator_chat_id:
            await callback.answer()
            return
        original_chat_id, message_id, reason = parsed
        row = await self.pool.fetchrow(
            """
            UPDATE incoming_messages
            SET is_voting_fail = TRUE,
                reject_reason = $4,
                rejected_by = $5,
                rejected_at = NOW()
            WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
                AND is_published = FALSE AND is_voting_success = FALSE AND is_voting_fail = FALSE
            RETURNING id
            """,
            self.bot_id,
            original_chat_id,
            message_id,
            reason,
            callback.from_user.id,
        )
        if not row:
            await callback.answer("⚠️ Message is already finished.", show_alert=True)
            return
        await self._notify_rejection_author(original_chat_id, message_id, "author_rejected")
        await self._refresh_moderation_status(original_chat_id, message_id, True)
        try:
            await callback.message.edit_text(f"🚫 Rejected: {REJECT_REASONS.get(reason, reason)}")
        except TelegramAPIError:
            LOGGER.debug("Unable to edit reject reason message", exc_info=True)
        await callback.answer(self._t("message_rejected"))

    async def _handle_contact_request(self, callback: CallbackQuery) -> None:
        parsed = _parse_moderation_action_callback(callback.data, "c")
        if not parsed or not callback.message or callback.message.chat.id != self.moderator_chat_id:
            await callback.answer()
            return
        original_chat_id, message_id = parsed
        row = await self._moderation_action_row(original_chat_id, message_id)
        if not row:
            await callback.answer(self._t("moderation_message_not_found"), show_alert=True)
            return

        self.pending_ban.pop(callback.from_user.id, None)
        self.pending_contact[callback.from_user.id] = (original_chat_id, message_id)
        await self.bot.send_message(
            self.moderator_chat_id,
            self._t("contact_prompt"),
            reply_to_message_id=row["moderation_fwd_message_id"] or callback.message.message_id,
            reply_markup=ForceReply(selective=True),
        )
        await callback.answer()

    async def _handle_ban_request(self, callback: CallbackQuery) -> None:
        parsed = _parse_moderation_action_callback(callback.data, "b")
        if not parsed or not callback.message or callback.message.chat.id != self.moderator_chat_id:
            await callback.answer()
            return
        original_chat_id, message_id = parsed
        row = await self._moderation_action_row(original_chat_id, message_id)
        if not row:
            await callback.answer(self._t("moderation_message_not_found"), show_alert=True)
            return

        target_user_id = int(row["owner_id"])
        if target_user_id == callback.from_user.id:
            await callback.answer(self._t("ban_self_denied"), show_alert=True)
            return
        if target_user_id == self.owner_id:
            await callback.answer(self._t("ban_owner_denied"), show_alert=True)
            return
        if await self._user_is_banned(target_user_id):
            await callback.answer(self._t("ban_already"), show_alert=True)
            return

        self.pending_contact.pop(callback.from_user.id, None)
        self.pending_ban[callback.from_user.id] = (target_user_id, original_chat_id, message_id)
        await self.bot.send_message(
            self.moderator_chat_id,
            self._t("ban_prompt"),
            reply_to_message_id=row["moderation_fwd_message_id"] or callback.message.message_id,
            reply_markup=ForceReply(selective=True),
        )
        await callback.answer()

    async def _handle_pending_moderator_action(self, message: Message) -> bool:
        if not message.from_user or message.chat.id != self.moderator_chat_id:
            return False

        moderator_id = message.from_user.id
        contact_target = self.pending_contact.get(moderator_id)
        ban_target = self.pending_ban.get(moderator_id)
        if not contact_target and not ban_target:
            return False

        text = (message.text or message.caption or "").strip()
        if text.startswith("/cancel"):
            self.pending_contact.pop(moderator_id, None)
            self.pending_ban.pop(moderator_id, None)
            await message.answer(self._t("moderator_action_cancelled"), reply_to_message_id=message.message_id)
            return True

        if contact_target:
            if not text:
                await message.answer(
                    self._t("contact_empty"),
                    reply_to_message_id=message.message_id,
                    reply_markup=ForceReply(selective=True),
                )
                return True
            if len(text) > CONTACT_MESSAGE_MAX_LENGTH:
                await message.answer(
                    self._t("contact_too_long", max=CONTACT_MESSAGE_MAX_LENGTH),
                    reply_to_message_id=message.message_id,
                    reply_markup=ForceReply(selective=True),
                )
                return True

            original_chat_id, original_message_id = contact_target
            try:
                await self.bot.send_message(
                    original_chat_id,
                    self._t("contact_to_author", text=text),
                    reply_to_message_id=original_message_id,
                )
            except TelegramAPIError:
                LOGGER.debug("Unable to send moderator contact message", exc_info=True)
                await message.answer(self._t("contact_failed"), reply_to_message_id=message.message_id)
                return True

            self.pending_contact.pop(moderator_id, None)
            await message.answer(self._t("contact_sent"), reply_to_message_id=message.message_id)
            return True

        if ban_target:
            if len(text) < BAN_REASON_MIN_LENGTH:
                await message.answer(
                    self._t("ban_reason_too_short", min=BAN_REASON_MIN_LENGTH),
                    reply_to_message_id=message.message_id,
                    reply_markup=ForceReply(selective=True),
                )
                return True

            target_user_id, _, _ = ban_target
            if target_user_id == moderator_id:
                self.pending_ban.pop(moderator_id, None)
                await message.answer(self._t("ban_self_denied"), reply_to_message_id=message.message_id)
                return True
            if target_user_id == self.owner_id:
                self.pending_ban.pop(moderator_id, None)
                await message.answer(self._t("ban_owner_denied"), reply_to_message_id=message.message_id)
                return True

            try:
                closed_rows = await self._ban_user(target_user_id, text)
            except Exception:
                LOGGER.exception("Unable to ban user #%s for bot #%s", target_user_id, self.bot_id)
                await message.answer(self._t("ban_failed"), reply_to_message_id=message.message_id)
                return True

            self.pending_ban.pop(moderator_id, None)
            for row in closed_rows:
                await self._refresh_moderation_status(row["original_chat_id"], row["id"], True)
            await message.answer(
                self._t("ban_done", count=len(closed_rows)),
                reply_to_message_id=message.message_id,
            )
            return True

        return False

    async def publish_ready_messages(self) -> None:
        async with self.publish_lock:
            delay_minutes = int(self.settings.get("delay", 0) or 0)
            async with self.pool.acquire() as conn:
                if delay_minutes > 0:
                    allowed = await conn.fetchval(
                        """
                        SELECT last_channel_message_at IS NULL
                            OR last_channel_message_at <= NOW() - ($2::int * interval '1 minute')
                        FROM registered_bots
                        WHERE id = $1
                        """,
                        self.bot_id,
                        delay_minutes,
                    )
                    if not allowed:
                        return

                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM incoming_messages
                    WHERE bot_id = $1
                        AND is_voting_success = TRUE
                        AND is_voting_fail = FALSE
                        AND is_published = FALSE
                    ORDER BY created_at
                    LIMIT 1
                    """,
                    self.bot_id,
                )
                if not row:
                    return

            try:
                published = await self._copy_or_forward(self.target_channel, row["original_chat_id"], row["id"])
            except TelegramAPIError:
                LOGGER.exception("Publishing failed for bot #%s message #%s", self.bot_id, row["id"])
                await self._notify_health(
                    "publish_failed",
                    "⚠️ Publishing failed. Check that the bot is an admin in the target channel.",
                )
                return

            await self.pool.execute(
                """
                UPDATE incoming_messages
                SET is_published = TRUE,
                    published_message_id = $1
                WHERE bot_id = $2 AND original_chat_id = $3 AND id = $4
                """,
                published.message_id,
                self.bot_id,
                row["original_chat_id"],
                row["id"],
            )
            await self.pool.execute(
                "UPDATE registered_bots SET last_channel_message_at = NOW() WHERE id = $1",
                self.bot_id,
            )
            await self._refresh_moderation_status(row["original_chat_id"], row["id"], True)

    async def _publisher_loop(self) -> None:
        try:
            while True:
                await self.publish_ready_messages()
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise

    async def _timeout_loop(self) -> None:
        try:
            while True:
                await self._expire_old_votes()
                await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await self._cleanup_old_data()
                await asyncio.sleep(self.cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise

    async def _cleanup_old_data(self) -> None:
        async with self.pool.acquire() as conn:
            deleted_messages = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM incoming_messages
                    WHERE bot_id = $1
                        AND created_at <= NOW() - ($2::int * interval '1 day')
                        AND (
                            is_published = TRUE
                            OR is_voting_fail = TRUE
                        )
                    RETURNING 1
                )
                SELECT COUNT(*) FROM deleted
                """,
                self.bot_id,
                self.data_retention_days,
            )
            deleted_votes = await conn.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM votes_history votes
                    WHERE votes.bot_id = $1
                        AND votes.created_at <= NOW() - ($2::int * interval '1 day')
                        AND NOT EXISTS (
                            SELECT 1
                            FROM incoming_messages messages
                            WHERE messages.bot_id = votes.bot_id
                                AND messages.original_chat_id = votes.original_chat_id
                                AND messages.id = votes.message_id
                        )
                    RETURNING 1
                )
                SELECT COUNT(*) FROM deleted
                """,
                self.bot_id,
                self.data_retention_days,
            )

        if deleted_messages or deleted_votes:
            LOGGER.info(
                "Cleaned old data for bot #%s: messages=%s votes=%s retention_days=%s",
                self.bot_id,
                deleted_messages or 0,
                deleted_votes or 0,
                self.data_retention_days,
            )

    async def _expire_old_votes(self) -> None:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT original_chat_id, id, ai_recommendation, ai_publish_score
                FROM incoming_messages
                WHERE bot_id = $1
                    AND is_voting_success = FALSE
                    AND is_voting_fail = FALSE
                    AND is_published = FALSE
                    AND created_at <= NOW() - ($2::int * interval '1 hour')
                """,
                self.bot_id,
                int(self.settings.get("vote_timeout", 24)),
            )

            auto_approved: list[asyncpg.Record] = []
            expired: list[asyncpg.Record] = []
            for row in rows:
                counts = await self._vote_counts(conn, row["original_chat_id"], row["id"])
                if self._ai_auto_publish_allowed(row, counts):
                    updated = await conn.fetchrow(
                        """
                        UPDATE incoming_messages
                        SET is_voting_success = TRUE,
                            ai_auto_approved = TRUE
                        WHERE bot_id = $1
                            AND original_chat_id = $2
                            AND id = $3
                            AND is_voting_success = FALSE
                            AND is_voting_fail = FALSE
                            AND is_published = FALSE
                        RETURNING original_chat_id, id
                        """,
                        self.bot_id,
                        row["original_chat_id"],
                        row["id"],
                    )
                    if updated:
                        auto_approved.append(updated)
                    continue

                updated = await conn.fetchrow(
                    """
                    UPDATE incoming_messages
                    SET is_voting_fail = TRUE,
                        reject_reason = COALESCE(reject_reason, 'timeout'),
                        rejected_at = COALESCE(rejected_at, NOW())
                    WHERE bot_id = $1
                        AND original_chat_id = $2
                        AND id = $3
                        AND is_voting_success = FALSE
                        AND is_voting_fail = FALSE
                        AND is_published = FALSE
                    RETURNING original_chat_id, id
                    """,
                    self.bot_id,
                    row["original_chat_id"],
                    row["id"],
                )
                if updated:
                    expired.append(updated)

        for row in auto_approved:
            await self._notify_author(row["original_chat_id"], row["id"], self._t("author_approved"))
            await self._refresh_moderation_status(row["original_chat_id"], row["id"], True)
        for row in expired:
            await self._notify_rejection_author(row["original_chat_id"], row["id"], "vote_expired")
            await self._refresh_moderation_status(row["original_chat_id"], row["id"], True)
        if auto_approved:
            await self.publish_ready_messages()

    async def _copy_or_forward(self, chat_id: int | str, from_chat_id: int, message_id: int):
        try:
            return await self._send_by_publish_mode(chat_id, from_chat_id, message_id)
        except TelegramMigrateToChat as exc:
            new_chat_id = int(exc.migrate_to_chat_id)
            await self._remember_migrated_chat(chat_id, new_chat_id)
            return await self._send_by_publish_mode(new_chat_id, from_chat_id, message_id)

    async def _send_by_publish_mode(self, chat_id: int | str, from_chat_id: int, message_id: int):
        if self._publish_mode() == "forward":
            return await self.bot.forward_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)
        return await self.bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)

    async def _remember_migrated_chat(self, old_chat_id: int | str, new_chat_id: int) -> None:
        LOGGER.info("Updating migrated chat for bot #%s: %s -> %s", self.bot_id, old_chat_id, new_chat_id)
        if old_chat_id == self.moderator_chat_id:
            self.moderator_chat_id = new_chat_id
            await self.pool.execute(
                "UPDATE registered_bots SET moderator_chat_id = $1 WHERE id = $2",
                new_chat_id,
                self.bot_id,
            )
            return

        if str(old_chat_id) == str(self.target_channel):
            self.target_channel = str(new_chat_id)
            await self.pool.execute(
                "UPDATE registered_bots SET target_channel = $1 WHERE id = $2",
                str(new_chat_id),
                self.bot_id,
            )

    async def _verification_message(
        self,
        message_id: int,
        original_chat_id: int,
        finished: bool,
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        async with self.pool.acquire() as conn:
            counts = await self._vote_counts(conn, original_chat_id, message_id)
            row = await conn.fetchrow(
                """
                SELECT
                    is_voting_success,
                    is_voting_fail,
                    is_published,
                    ai_provider,
                    ai_model,
                    ai_analysis,
                    ai_recommendation,
                    ai_publish_score,
                    ai_auto_approved,
                    duplicate_of_original_chat_id,
                    duplicate_of_message_id,
                    duplicate_score,
                    reject_reason
                FROM incoming_messages
                WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
                """,
                self.bot_id,
                original_chat_id,
                message_id,
            )

        yes = counts["yes"]
        no = counts["no"]
        total = yes + no
        lines = [
            f"<b>{_html(self._t('moderation_title'))}</b>",
            _html(self._t("moderation_votes", yes=yes, no=no, total=total, needed=self.settings["votes"])),
        ]

        if row and row["ai_provider"] and not row["ai_analysis"]:
            lines.extend(["", f"<i>{_html(self._t('ai_analysis_checking', provider=_ai_label(row)))}</i>"])
        elif row and row["ai_analysis"]:
            lines.extend(
                [
                    "",
                    f"<b>{_html(self._t('ai_analysis_heading', provider=_ai_label(row)))}</b>",
                    f"<blockquote>{_html(row['ai_analysis'])}</blockquote>",
                ]
            )

        if row and row["duplicate_score"]:
            lines.extend(
                [
                    "",
                    "🔁 "
                    + _html(
                        f"Possible duplicate: {row['duplicate_score']}% similar to "
                        f"{row['duplicate_of_original_chat_id']}/{row['duplicate_of_message_id']}"
                    ),
                ]
            )

        if finished and row:
            if row["is_published"]:
                lines.extend(["", _html(self._t("status_published"))])
            elif row["ai_auto_approved"]:
                lines.extend(["", _html(self._t("status_ai_auto_approved"))])
            elif row["is_voting_success"]:
                lines.extend(["", _html(self._t("status_approved_waiting"))])
            elif row["is_voting_fail"]:
                lines.extend(["", _html(self._t("status_rejected"))])
                reason_label = _reject_reason_label(row["reject_reason"])
                if reason_label:
                    lines.append("🚫 " + _html(f"Reason: {reason_label}"))
            else:
                lines.extend(["", _html(self._t("status_closed"))])
            return "\n".join(lines), None

        lines.extend(["", f"<b>{_html(self._t('moderation_question'))}</b>"])
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=self._t("button_yes", count=yes),
                        callback_data=f"v|{original_chat_id}|{message_id}|y",
                    ),
                    InlineKeyboardButton(
                        text=self._t("button_no", count=no),
                        callback_data=f"v|{original_chat_id}|{message_id}|n",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=self._t("button_reject"),
                        callback_data=f"r|{original_chat_id}|{message_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=self._t("button_contact"),
                        callback_data=f"c|{original_chat_id}|{message_id}",
                    ),
                    InlineKeyboardButton(
                        text=self._t("button_ban"),
                        callback_data=f"b|{original_chat_id}|{message_id}",
                    ),
                ],
            ]
        )
        return "\n".join(lines), keyboard

    async def _refresh_moderation_status(self, original_chat_id: int, message_id: int, finished: bool) -> None:
        row = await self.pool.fetchrow(
            """
            SELECT moderation_message_id
            FROM incoming_messages
            WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
            """,
            self.bot_id,
            original_chat_id,
            message_id,
        )
        if not row or not row["moderation_message_id"]:
            return
        text, keyboard = await self._verification_message(message_id, original_chat_id, finished)
        try:
            await self.bot.edit_message_text(
                text,
                chat_id=self.moderator_chat_id,
                message_id=row["moderation_message_id"],
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            try:
                await self.bot.edit_message_text(
                    _strip_html(text),
                    chat_id=self.moderator_chat_id,
                    message_id=row["moderation_message_id"],
                    reply_markup=keyboard,
                )
            except TelegramAPIError:
                LOGGER.debug("Unable to refresh moderation status", exc_info=True)

    async def _send_moderation_status(
        self,
        text: str,
        keyboard: InlineKeyboardMarkup | None,
        reply_to_message_id: int,
    ) -> Message:
        try:
            return await self.bot.send_message(
                self.moderator_chat_id,
                text,
                reply_markup=keyboard,
                reply_to_message_id=reply_to_message_id,
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            LOGGER.debug("Unable to send HTML moderation status, retrying as plain text", exc_info=True)
            return await self.bot.send_message(
                self.moderator_chat_id,
                _strip_html(text),
                reply_markup=keyboard,
                reply_to_message_id=reply_to_message_id,
            )

    async def _message_finished(self, original_chat_id: int, message_id: int) -> bool:
        row = await self.pool.fetchrow(
            """
            SELECT is_voting_success, is_voting_fail, is_published
            FROM incoming_messages
            WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
            """,
            self.bot_id,
            original_chat_id,
            message_id,
        )
        return bool(row and (row["is_voting_success"] or row["is_voting_fail"] or row["is_published"]))

    async def _vote_counts(self, conn: asyncpg.Connection, original_chat_id: int, message_id: int) -> dict[str, int]:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE vote_yes) AS yes,
                COUNT(*) FILTER (WHERE NOT vote_yes) AS no
            FROM votes_history
            WHERE bot_id = $1 AND original_chat_id = $2 AND message_id = $3
            """,
            self.bot_id,
            original_chat_id,
            message_id,
        )
        return {"yes": row["yes"] or 0, "no": row["no"] or 0}

    async def _notify_author(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, text, reply_to_message_id=message_id)
        except TelegramAPIError:
            LOGGER.debug("Unable to notify author", exc_info=True)

    async def _notify_owner(self, text: str) -> None:
        try:
            await self.bot.send_message(self.owner_id, text)
        except TelegramAPIError:
            LOGGER.debug("Unable to notify owner", exc_info=True)

    async def _notify_health(self, key: str, text: str, throttle_seconds: int = 3600) -> None:
        now = time.monotonic()
        last = self.health_alerts.get(key, 0)
        if now - last < throttle_seconds:
            return
        self.health_alerts[key] = now
        await self._notify_owner(f"🩺 Health alert for bot #{self.bot_id}\n\n{text}")

    async def _notify_rejection_author(self, chat_id: int, message_id: int, base_key: str) -> None:
        row = await self.pool.fetchrow(
            """
            SELECT ai_analysis, reject_reason
            FROM incoming_messages
            WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
            """,
            self.bot_id,
            chat_id,
            message_id,
        )
        analysis = row["ai_analysis"] if row else None
        reason = row["reject_reason"] if row else None
        analysis = str(analysis or "").strip()
        reason_label = _reject_reason_label(reason)
        if analysis:
            text = self._t(f"{base_key}_with_ai", analysis=_preview_text(analysis, 1400))
        else:
            text = self._t(base_key)
        if reason_label:
            text = f"{text}\n\n🚫 Reason: {reason_label}"
        await self._notify_author(chat_id, message_id, text)

    async def _moderation_action_row(self, original_chat_id: int, message_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            SELECT owner_id, moderation_fwd_message_id
            FROM incoming_messages
            WHERE bot_id = $1 AND original_chat_id = $2 AND id = $3
            """,
            self.bot_id,
            original_chat_id,
            message_id,
        )

    async def _user_is_banned(self, user_id: int) -> bool:
        value = await self.pool.fetchval(
            """
            SELECT banned_at IS NOT NULL
            FROM users
            WHERE bot_id = $1 AND user_id = $2
            """,
            self.bot_id,
            user_id,
        )
        return bool(value)

    async def _ban_user(self, user_id: int, reason: str) -> list[asyncpg.Record]:
        reason = reason.strip()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO users (bot_id, user_id, first_name, banned_at, ban_reason)
                    VALUES ($1, $2, 'Unknown', NOW(), $3)
                    ON CONFLICT (bot_id, user_id) DO UPDATE SET
                        banned_at = NOW(),
                        ban_reason = EXCLUDED.ban_reason
                    """,
                    self.bot_id,
                    user_id,
                    reason,
                )
                rows = await conn.fetch(
                    """
                    UPDATE incoming_messages
                    SET is_voting_fail = TRUE
                    WHERE bot_id = $1
                        AND owner_id = $2
                        AND is_voting_success = FALSE
                        AND is_voting_fail = FALSE
                        AND is_published = FALSE
                    RETURNING original_chat_id, id
                    """,
                    self.bot_id,
                    user_id,
                )

        try:
            await self.bot.send_message(user_id, self._t("ban_notification", reason=reason))
        except TelegramAPIError:
            LOGGER.debug("Unable to notify banned user", exc_info=True)
        return list(rows)

    async def _unban_user(self, user_id: int) -> bool:
        restored = await self.pool.fetchval(
            """
            UPDATE users
            SET banned_at = NULL,
                ban_reason = NULL
            WHERE bot_id = $1 AND user_id = $2 AND banned_at IS NOT NULL
            RETURNING user_id
            """,
            self.bot_id,
            user_id,
        )
        if not restored:
            return False
        try:
            await self.bot.send_message(user_id, self._t("unban_notification"))
        except TelegramAPIError:
            LOGGER.debug("Unable to notify unbanned user", exc_info=True)
        return True

    async def _banlist_text(self) -> str:
        rows = await self.pool.fetch(
            """
            SELECT user_id, first_name, last_name, username, banned_at, ban_reason
            FROM users
            WHERE bot_id = $1 AND banned_at IS NOT NULL
            ORDER BY banned_at DESC
            LIMIT 50
            """,
            self.bot_id,
        )
        if not rows:
            return "⛔ Банлист пуст."

        lines = ["⛔ Забаненные пользователи", ""]
        for index, row in enumerate(rows, start=1):
            name = _user_label(row)
            reason = _single_line(row["ban_reason"] or "", 90)
            banned_at = row["banned_at"].strftime("%Y-%m-%d") if row["banned_at"] else "unknown"
            lines.append(f"{index}. {name} | id {row['user_id']} | {banned_at} | {reason} | /unban {row['user_id']}")
        return _preview_text("\n".join(lines), 3900)

    async def _upsert_user(self, message: Message) -> bool:
        if not message.from_user:
            return False
        await self.pool.execute(
            """
            INSERT INTO users (bot_id, user_id, first_name, last_name, username)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (bot_id, user_id) DO UPDATE SET
                first_name = EXCLUDED.first_name,
                last_name = COALESCE(EXCLUDED.last_name, users.last_name),
                username = COALESCE(EXCLUDED.username, users.username),
                updated_at = NOW()
            """,
            self.bot_id,
            message.from_user.id,
            message.from_user.first_name,
            message.from_user.last_name,
            message.from_user.username,
        )
        banned = await self.pool.fetchval(
            "SELECT banned_at IS NOT NULL FROM users WHERE bot_id = $1 AND user_id = $2",
            self.bot_id,
            message.from_user.id,
        )
        return not banned

    async def _update_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        await self.pool.execute(
            "UPDATE registered_bots SET settings = $1 WHERE id = $2",
            self.settings,
            self.bot_id,
        )

    async def _set_moderator_chat(self, chat_id: int) -> None:
        self.moderator_chat_id = int(chat_id)
        await self.pool.execute(
            "UPDATE registered_bots SET moderator_chat_id = $1 WHERE id = $2",
            self.moderator_chat_id,
            self.bot_id,
        )

    async def _notify_moderator_chat_changed(self, old_chat_id: int, new_chat_id: int) -> None:
        try:
            await self.bot.send_message(
                old_chat_id,
                "👥 Чат модераторов для этого бота перенесен.\n"
                f"Новый chat_id: {new_chat_id}",
            )
        except TelegramAPIError:
            LOGGER.debug("Unable to notify old moderator chat about migration", exc_info=True)

    def _can_manage(self, message: Message) -> bool:
        if not message.from_user:
            return False
        return message.from_user.id == self.owner_id and _chat_type(message) == ChatType.PRIVATE.value

    def _can_change_moderator_chat(self, message: Message) -> bool:
        if not message.from_user or message.from_user.id != self.owner_id:
            return False
        return _chat_type(message) in {
            ChatType.PRIVATE.value,
            ChatType.GROUP.value,
            ChatType.SUPERGROUP.value,
        }

    def _can_manage_callback(self, callback: CallbackQuery) -> bool:
        if callback.from_user.id != self.owner_id or not callback.message:
            return False
        return callback.message.chat.id == self.owner_id


def _chat_type(message: Message) -> str:
    return message.chat.type.value if hasattr(message.chat.type, "value") else str(message.chat.type)


def _enabled_label(value: bool) -> str:
    return "🟢 включено" if value else "⚪ выключено"


def _preview_text(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."


def _short_model_label(model: str, limit: int = 28) -> str:
    if len(model) <= limit:
        return model
    return model[: limit - 1].rstrip("-_") + "…"


def _valid_model_id(model: str) -> bool:
    return bool(model) and len(model) <= 96 and not any(ch.isspace() for ch in model) and "|" not in model


def _valid_chat_id(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d{5,20}", value))


def _message_dump(message: Message) -> dict[str, Any]:
    return message.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_defaults=True,
    )


def _html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"</?[^>]+>", "", value))


def _ai_label(row: Any) -> str:
    return str(row["ai_provider"] or "AI")


def _user_label(row: Any) -> str:
    username = row["username"]
    if username:
        return f"@{username}"
    first_name = row["first_name"] or ""
    last_name = row["last_name"] or ""
    full_name = f"{first_name} {last_name}".strip()
    return full_name or f"id {row['user_id']}"


def _single_line(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _reject_reason_label(reason: Any) -> str | None:
    if not reason:
        return None
    reason = str(reason)
    if reason == "no_vote":
        return "Moderator no vote"
    if reason == "timeout":
        return "Voting timeout"
    return REJECT_REASONS.get(reason, reason.replace("_", " ").title())


def _reject_reason_keyboard(original_chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"🚫 {label}",
            callback_data=f"rr|{original_chat_id}|{message_id}|{key}",
        )
        for key, label in REJECT_REASONS.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _normalize_submission_text(message: Message) -> str | None:
    text = message.text or message.caption or ""
    normalized = _normalize_text(text)
    return normalized or None


def _media_unique_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_unique_id
    media = (
        message.video
        or message.animation
        or message.document
        or message.audio
        or message.voice
        or message.sticker
    )
    return getattr(media, "file_unique_id", None) if media else None


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", value)
    value = re.sub(r"[^\w\s']", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _text_similarity_score(left: str, right: str) -> int:
    if not left or not right:
        return 0
    if left == right:
        return DUPLICATE_EXACT_SCORE

    left_words = set(left.split())
    right_words = set(right.split())
    if len(left_words) < DUPLICATE_MIN_WORDS or len(right_words) < DUPLICATE_MIN_WORDS:
        return 0
    union = left_words | right_words
    if not union:
        return 0
    return round(100 * len(left_words & right_words) / len(union))


def _freq_limit_label(limit: tuple[int, int]) -> str:
    count, hours = limit
    return f"{count} сообщений за {hours} ч."


def _toggle_button(label: str, key: str, enabled: bool, section: str) -> InlineKeyboardButton:
    state = "🟢 вкл" if enabled else "⚪ выкл"
    return InlineKeyboardButton(text=f"{label}: {state}", callback_data=f"s|toggle|{key}|{section}")


def _mode_button(label: str, mode: str, active_mode: str) -> InlineKeyboardButton:
    prefix = "✅ " if mode == active_mode else ""
    return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"s|toggle|publish:{mode}|publish")


def _language_button(label: str, language: str, active_language: str) -> InlineKeyboardButton:
    prefix = "✅ " if language == active_language else ""
    return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"s|toggle|language:{language}|language")


def _ai_provider_button(label: str, provider: str, active_provider: str) -> InlineKeyboardButton:
    prefix = "✅ " if provider == active_provider else ""
    return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"s|toggle|ai_provider:{provider}|ai")


def _adjust_row(label: str, setting: str, minus_delta: int, plus_delta: int, section: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=f"➖ {label}", callback_data=f"s|adj|{setting}|{minus_delta}|{section}"),
        InlineKeyboardButton(text=f"➕ {label}", callback_data=f"s|adj|{setting}|{plus_delta}|{section}"),
    ]


def _ai_score_adjust_row(score: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="➖ AI порог", callback_data="s|ai_score_adj|-5"),
        InlineKeyboardButton(text=f"🎯 {score}%", callback_data="s|nav|ai"),
        InlineKeyboardButton(text="➕ AI порог", callback_data="s|ai_score_adj|5"),
    ]


def _freq_adjust_row(label: str, field: str, minus_delta: int, plus_delta: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=f"➖ {label}", callback_data=f"s|freq_adj|{field}|{minus_delta}"),
        InlineKeyboardButton(text=f"➕ {label}", callback_data=f"s|freq_adj|{field}|{plus_delta}"),
    ]


def _duplicate_days_adjust_row(label: str, minus_delta: int, plus_delta: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=f"➖ {label}", callback_data=f"s|duplicate_days_adj|{minus_delta}"),
        InlineKeyboardButton(text=f"➕ {label}", callback_data=f"s|duplicate_days_adj|{plus_delta}"),
    ]


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data="s|nav|main")]


def _content_key(message: Message) -> str | None:
    content_type = message.content_type.value if hasattr(message.content_type, "value") else str(message.content_type)
    key = SUBMIT_KINDS.get(content_type)
    if key == "document" and message.document and message.document.mime_type == "video/mp4":
        return "gif"
    return key


def _command_int(message: Message, minimum: int, maximum: int) -> int | None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    value = int(parts[1])
    if minimum <= value <= maximum:
        return value
    return None


def _parse_freq_limit_value(value: str) -> tuple[int, int] | None:
    numbers = re.findall(r"\d+", value)
    if len(numbers) != 2:
        return None

    count = int(numbers[0])
    hours = int(numbers[1])
    if count < 1 or hours < 1:
        return None
    return min(count, FREQ_LIMIT_MAX_COUNT), min(hours, FREQ_LIMIT_MAX_HOURS)


def _parse_message_key(data: str | None) -> tuple[int, int] | None:
    if not data:
        return None
    parts = data.split("|")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _parse_vote_callback(data: str | None) -> tuple[int, int, bool] | None:
    if not data:
        return None
    parts = data.split("|")
    if len(parts) != 4 or parts[3] not in {"y", "n"}:
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3] == "y"
    except ValueError:
        return None


def _parse_reject_callback(data: str | None) -> tuple[int, int] | None:
    if not data:
        return None
    parts = data.split("|")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _parse_reject_reason_callback(data: str | None) -> tuple[int, int, str] | None:
    if not data:
        return None
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != "rr" or parts[3] not in REJECT_REASONS:
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3]
    except ValueError:
        return None


def _parse_moderation_action_callback(data: str | None, prefix: str) -> tuple[int, int] | None:
    if not data:
        return None
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None
