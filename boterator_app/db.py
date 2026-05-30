from __future__ import annotations

import copy
import json
from typing import Any

import asyncpg

from .i18n import DEFAULT_HELLO_TEXTS, DEFAULT_START_TEXTS


DEFAULT_SLAVE_SETTINGS: dict[str, Any] = {
    "delay": 15,
    "votes": 2,
    "vote_timeout": 24,
    "text_min": 1,
    "text_max": 4000,
    "language": "ru",
    "start": DEFAULT_START_TEXTS["ru"],
    "hello": DEFAULT_HELLO_TEXTS["ru"],
    "publish_mode": "forward",
    "public_vote": True,
    "power": False,
    "content_status": {
        "text": True,
        "photo": True,
        "voice": False,
        "video": False,
        "audio": False,
        "document": False,
        "sticker": False,
        "gif": False,
    },
    "selfvote": True,
    "msg_freq_limit": None,
    "allow_vote_switch": False,
    "tag_polls": False,
    "ai": {
        "enabled": False,
        "provider": "openai",
        "openai_api_key": None,
        "openai_model": "gpt-5-mini",
        "gemini_api_key": None,
        "gemini_model": "gemini-3.5-flash",
        "auto_publish_after_timeout": False,
        "auto_publish_min_score": 85,
        "system_prompt": None,
        "custom_prompt": None,
    },
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registered_bots (
    id BIGINT PRIMARY KEY,
    token TEXT NOT NULL,
    owner_id BIGINT NOT NULL,
    moderator_chat_id BIGINT NOT NULL,
    target_channel TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_moderation_message_at TIMESTAMPTZ,
    last_channel_message_at TIMESTAMPTZ,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS users (
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT,
    username TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    banned_at TIMESTAMPTZ,
    ban_reason TEXT,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS incoming_messages (
    id BIGINT NOT NULL,
    original_chat_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    bot_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_voting_fail BOOLEAN NOT NULL DEFAULT FALSE,
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    is_voting_success BOOLEAN NOT NULL DEFAULT FALSE,
    message JSONB NOT NULL,
    moderation_message_id BIGINT,
    moderation_fwd_message_id BIGINT,
    ai_provider TEXT,
    ai_model TEXT,
    ai_analysis TEXT,
    ai_recommendation TEXT,
    ai_publish_score INTEGER,
    ai_auto_approved BOOLEAN NOT NULL DEFAULT FALSE,
    published_message_id BIGINT,
    PRIMARY KEY (bot_id, original_chat_id, id)
);

CREATE TABLE IF NOT EXISTS votes_history (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    original_chat_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vote_yes BOOLEAN NOT NULL
);

ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS ai_provider TEXT;
ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS ai_model TEXT;
ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS ai_analysis TEXT;
ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS ai_recommendation TEXT;
ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS ai_publish_score INTEGER;
ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS ai_auto_approved BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE incoming_messages ADD COLUMN IF NOT EXISTS published_message_id BIGINT;
ALTER TABLE incoming_messages ALTER COLUMN id TYPE BIGINT;
ALTER TABLE incoming_messages ALTER COLUMN moderation_message_id TYPE BIGINT;
ALTER TABLE incoming_messages ALTER COLUMN moderation_fwd_message_id TYPE BIGINT;

ALTER TABLE registered_bots ALTER COLUMN token TYPE TEXT;
ALTER TABLE registered_bots ALTER COLUMN target_channel TYPE TEXT;

ALTER TABLE votes_history ADD COLUMN IF NOT EXISTS bot_id BIGINT;
UPDATE votes_history SET bot_id = 0 WHERE bot_id IS NULL;
ALTER TABLE votes_history ALTER COLUMN bot_id SET NOT NULL;
ALTER TABLE votes_history ALTER COLUMN message_id TYPE BIGINT;

CREATE INDEX IF NOT EXISTS im_pending_die_idx
    ON incoming_messages (bot_id, is_voting_success, is_voting_fail, created_at DESC);
CREATE INDEX IF NOT EXISTS im_pending_idx
    ON incoming_messages (bot_id, is_voting_success, is_published, created_at);
CREATE INDEX IF NOT EXISTS rb_active_idx ON registered_bots (active);
CREATE INDEX IF NOT EXISTS rb_moderator_chat_idx ON registered_bots (moderator_chat_id);
CREATE INDEX IF NOT EXISTS users_banned_idx ON users (bot_id, banned_at);
CREATE INDEX IF NOT EXISTS votes_history_mo_idx
    ON votes_history (bot_id, message_id, original_chat_id);
CREATE UNIQUE INDEX IF NOT EXISTS votes_history_unique_vote_idx
    ON votes_history (bot_id, user_id, message_id, original_chat_id);
"""


async def init_connection(conn: asyncpg.Connection) -> None:
    for codec_name in ("json", "jsonb"):
        await conn.set_type_codec(
            codec_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(database_url, min_size=1, max_size=10, init=init_connection)


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


def merge_settings(saved: dict[str, Any] | None) -> dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_SLAVE_SETTINGS)
    if saved:
        _deep_update(merged, saved)
    return merged


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
