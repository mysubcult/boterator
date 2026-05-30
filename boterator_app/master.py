from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from .config import Settings
from .db import DEFAULT_SLAVE_SETTINGS

if TYPE_CHECKING:
    from .slave import SlaveManager


LOGGER = logging.getLogger(__name__)


@dataclass
class RegistrationState:
    step: str
    owner_id: int
    token: str | None = None
    bot_id: int | None = None
    bot_username: str | None = None
    moderator_chat_id: int | None = None
    moderator_chat_title: str | None = None
    settings: dict[str, Any] | None = None


class MasterBot:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, slave_manager: "SlaveManager"):
        self.pool = pool
        self.settings = settings
        self.slave_manager = slave_manager
        self.bot = Bot(settings.master_token, default=DefaultBotProperties(parse_mode=None))
        self.dp = Dispatcher()
        self.router = Router()
        self.states: dict[int, RegistrationState] = {}
        self._register_handlers()
        self.dp.include_router(self.router)

    async def run(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=False)
        LOGGER.info("Starting master bot polling")
        await self.dp.start_polling(self.bot, allowed_updates=self.dp.resolve_used_update_types())

    async def stop(self) -> None:
        await self.bot.session.close()

    def _register_handlers(self) -> None:
        @self.router.message(CommandStart())
        async def start(message: Message) -> None:
            await message.answer(
                "🤖 Это master-бот Boterator.\n\n"
                "🚀 Команда /reg зарегистрирует отдельного бота, который будет принимать "
                "сообщения от авторов, отправлять их модераторам и публиковать одобренное в канал."
            )

        @self.router.message(Command("cancel"))
        async def cancel(message: Message) -> None:
            if message.from_user:
                self.states.pop(message.from_user.id, None)
            await message.answer("↩️ Регистрация отменена.")

        @self.router.message(Command("reg"))
        async def reg(message: Message) -> None:
            if not message.from_user:
                return
            slave_settings = copy.deepcopy(DEFAULT_SLAVE_SETTINGS)
            self.states[message.from_user.id] = RegistrationState(
                step="token",
                owner_id=message.from_user.id,
                settings=slave_settings,
            )
            await message.answer("🔑 Пришлите токен нового бота, полученный у @BotFather.")

        @self.router.message(F.text)
        async def registration_text(message: Message) -> None:
            if not message.from_user:
                return
            state = self.states.get(message.from_user.id)
            if not state:
                await message.answer("🚀 Используйте /reg, чтобы начать регистрацию бота.")
                return
            if state.step == "token":
                await self._handle_token(message, state)
            elif state.step == "channel":
                await self._handle_channel(message, state)

    async def _handle_token(self, message: Message, state: RegistrationState) -> None:
        token = (message.text or "").strip()
        if not _looks_like_token(token):
            await message.answer("⚠️ Токен выглядит некорректно. Пришлите токен в формате 123456:ABC...")
            return

        slave_bot = Bot(token, default=DefaultBotProperties(parse_mode=None))
        try:
            me = await slave_bot.get_me()
        except TelegramAPIError as exc:
            await message.answer(f"⚠️ Telegram не принял токен: {exc}")
            await slave_bot.session.close()
            return

        duplicate = await self.pool.fetchval(
            "SELECT TRUE FROM registered_bots WHERE id = $1 AND active = TRUE",
            me.id,
        )
        if duplicate:
            await message.answer("☑️ Этот бот уже зарегистрирован и активен.")
            await slave_bot.session.close()
            return

        username = me.username or str(me.id)
        state.token = token
        state.bot_id = me.id
        state.bot_username = username
        assert state.settings is not None
        state.settings["hello"] = state.settings["hello"].format(bot_username=username)

        await message.answer(
            f"✅ Токен принят: @{username}.\n\n"
            "👥 Теперь добавьте этого бота в чат модераторов и отправьте там "
            f"/attach@{username}. Я буду ждать до {self.settings.registration_timeout_seconds // 60} минут."
        )

        try:
            chat_info = await wait_for_moderation_group(
                slave_bot,
                username,
                self.settings.registration_timeout_seconds,
            )
        finally:
            await slave_bot.session.close()

        if not chat_info:
            await message.answer("⏳ Не дождался /attach в чате модераторов. Пришлите токен еще раз, чтобы повторить.")
            state.step = "token"
            return

        state.moderator_chat_id = chat_info["id"]
        state.moderator_chat_title = chat_info["title"]
        if chat_info["type"] == ChatType.PRIVATE.value:
            state.settings["votes"] = 1

        state.step = "channel"
        await message.answer(
            "✅ Чат модераторов подключен: "
            f"{chat_info['title']}.\n\n"
            f"📣 Добавьте @{username} администратором в целевой канал и пришлите username канала, например @mychannel."
        )

    async def _handle_channel(self, message: Message, state: RegistrationState) -> None:
        channel = (message.text or "").strip()
        if not _valid_channel_ref(channel):
            await message.answer("📣 Пришлите username канала в формате @channel или числовой id канала.")
            return
        assert state.token and state.bot_id and state.bot_username and state.moderator_chat_id and state.settings

        slave_bot = Bot(state.token, default=DefaultBotProperties(parse_mode=None))
        try:
            await slave_bot.send_message(channel, state.settings["hello"])
        except TelegramAPIError as exc:
            await message.answer(
                "⚠️ Не смог отправить приветствие в канал. Проверьте, что бот добавлен "
                f"администратором канала. Ошибка Telegram: {exc}"
            )
            return
        finally:
            await slave_bot.session.close()

        await self.pool.execute(
            """
            INSERT INTO registered_bots (
                id, token, owner_id, moderator_chat_id, target_channel, active, settings
            )
            VALUES ($1, $2, $3, $4, $5, TRUE, $6)
            ON CONFLICT (id) DO UPDATE SET
                token = EXCLUDED.token,
                owner_id = EXCLUDED.owner_id,
                moderator_chat_id = EXCLUDED.moderator_chat_id,
                target_channel = EXCLUDED.target_channel,
                active = TRUE,
                settings = EXCLUDED.settings
            """,
            state.bot_id,
            state.token,
            state.owner_id,
            state.moderator_chat_id,
            channel,
            state.settings,
        )

        await self.slave_manager.start_bot_by_id(state.bot_id)
        self.states.pop(state.owner_id, None)

        await message.answer(
            f"✅ Готово. @{state.bot_username} принимает сообщения и отправляет их на модерацию.\n\n"
            "⚙️ Настройки доступны владельцу в личке с зарегистрированным ботом: /settings."
        )


async def wait_for_moderation_group(bot: Bot, username: str, timeout_seconds: int) -> dict[str, Any] | None:
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    deadline = time.monotonic() + timeout_seconds
    offset: int | None = None
    username = username.lower()

    while time.monotonic() < deadline:
        timeout = max(1, min(25, int(deadline - time.monotonic())))
        updates = await bot.get_updates(
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "my_chat_member"],
        )
        for update in updates:
            offset = update.update_id + 1
            chat = _chat_from_attach_update(update, username, me.id)
            if chat:
                return chat
        await asyncio.sleep(0.2)
    return None


def _chat_from_attach_update(update: Update, username: str, bot_id: int) -> dict[str, Any] | None:
    if update.my_chat_member:
        chat = update.my_chat_member.chat
        status = update.my_chat_member.new_chat_member.status
        status_value = status.value if hasattr(status, "value") else str(status)
        if (
            _chat_type(chat) in {ChatType.GROUP.value, ChatType.SUPERGROUP.value, ChatType.PRIVATE.value}
            and status_value in {"member", "administrator"}
        ):
            return _serialize_chat(chat, update.my_chat_member.from_user)

    msg = update.message
    if not msg:
        return None

    if msg.new_chat_members and any(user.id == bot_id for user in msg.new_chat_members):
        return _serialize_chat(msg.chat, msg.from_user)

    text = (msg.text or "").strip().lower()
    first = text.split(maxsplit=1)[0] if text else ""
    if first in {"/attach", f"/attach@{username}"}:
        return _serialize_chat(msg.chat, msg.from_user)

    return None


def _serialize_chat(chat: Chat, sender: Any | None) -> dict[str, Any]:
    title = chat.title or chat.full_name or f"chat {chat.id}"
    return {
        "id": chat.id,
        "type": _chat_type(chat),
        "title": title,
        "sender": {
            "id": sender.id,
            "username": sender.username,
            "first_name": sender.first_name,
        }
        if sender
        else None,
    }


def _chat_type(chat: Chat) -> str:
    return chat.type.value if hasattr(chat.type, "value") else str(chat.type)


def _looks_like_token(token: str) -> bool:
    parts = token.split(":", 1)
    return len(parts) == 2 and parts[0].isdigit() and len(parts[1]) > 20


def _valid_channel_ref(value: str) -> bool:
    if value.startswith("@") and " " not in value and len(value) > 1:
        return True
    if value.startswith("-100") and value[4:].isdigit():
        return True
    return False
