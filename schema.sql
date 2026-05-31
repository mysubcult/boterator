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
    reject_reason TEXT,
    reject_note TEXT,
    rejected_by BIGINT,
    rejected_at TIMESTAMPTZ,
    normalized_text TEXT,
    media_unique_id TEXT,
    auto_filter_reason TEXT,
    duplicate_of_original_chat_id BIGINT,
    duplicate_of_message_id BIGINT,
    duplicate_score INTEGER,
    published_message_id BIGINT,
    PRIMARY KEY (bot_id, original_chat_id, id)
);

CREATE TABLE IF NOT EXISTS ai_prompt_history (
    id BIGSERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prompt TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS im_normalized_text_idx
    ON incoming_messages (bot_id, normalized_text);
CREATE INDEX IF NOT EXISTS im_media_unique_id_idx
    ON incoming_messages (bot_id, media_unique_id, created_at DESC)
    WHERE media_unique_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ai_prompt_history_bot_idx
    ON ai_prompt_history (bot_id, created_at DESC);
