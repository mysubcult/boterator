# Boterator

Modern Telegram moderation bot for channels.

Boterator lets users submit posts to a bot, forwards the original Telegram message to a moderator chat, collects moderator votes with inline buttons, and publishes approved posts to the target channel.

## Features

- Master bot registers separate moderation bots created via BotFather.
- User submissions are forwarded to moderators and then forwarded to the channel, preserving the original sender header when Telegram allows it.
- Inline moderator voting with configurable vote count, timeout, publish delay, and content types.
- Moderator buttons to contact the author through the bot or ban the author from future submissions.
- Owner-only settings menu in private chat via `/settings`.
- Public users only need `/start` and `/stats`.
- Russian and English public/moderation texts per registered bot.
- Optional OpenAI or Gemini analysis for text and image submissions.
- Optional AI fallback: after the voting timeout, AI can auto-approve only when its initial verdict is `publish`, the score is above the configured threshold, and moderators did not vote against the post.
- Per-bot AI keys can be stored from the owner-only settings menu.
- Old finished moderation records are cleaned automatically.

## Requirements

- Docker and Docker Compose.
- A Telegram master bot token from BotFather.
- PostgreSQL. The compose file includes PostgreSQL.
- Optional: OpenAI or Gemini API key for AI analysis.

## Quick Start

```bash
cp docker-compose.yml.example docker-compose.yml
cp .env.example .env
```

Edit `.env`:

```env
TG_TOKEN=123456:master-bot-token
LOG_LEVEL=INFO
DATA_RETENTION_DAYS=90
CLEANUP_INTERVAL_HOURS=24
```

Start:

```bash
docker compose up -d --build
docker compose logs -f app
```

Do not commit `.env`; it contains secrets.

## Environment

Required:

- `TG_TOKEN` or `MASTER_BOT_TOKEN` - token of the master bot.
- `DATABASE_URL` - PostgreSQL DSN. The compose example sets it automatically for the app service.

Optional:

- `PUBLISH_MODE=forward|copy` - default mode for new bots. `forward` preserves Telegram forward attribution; `copy` sends without it.
- `LOG_LEVEL=INFO|WARNING|ERROR`
- `DATA_RETENTION_DAYS=90` - how long to keep finished moderation records and related votes.
- `CLEANUP_INTERVAL_HOURS=24` - how often cleanup runs.
- `AI_PROVIDER=none|openai|gemini`
- `OPENAI_API_KEY`, `OPENAI_MODEL`
- `GEMINI_API_KEY`, `GEMINI_MODEL`
- `AI_MAX_IMAGE_BYTES` - max image size downloaded for AI analysis.

Per-bot AI keys and models can also be configured from `/settings`; they are stored in PostgreSQL and are never shown back in Telegram.

## Register A Bot

1. Create a new Telegram bot in BotFather.
2. Open the master bot and send `/reg`.
3. Send the new bot token to the master bot.
4. Add the new bot to the moderator chat.
5. In the moderator chat, send `/attach@new_bot_username`.
6. Add the new bot as an administrator to the target channel.
7. Send the target channel username or id to the master bot.
8. Open the registered bot in private chat and use `/settings`.

## Owner Settings

Only the registered owner can open settings, and only in private chat with the registered bot.

Main entry:

```text
/settings
```

The menu includes:

- Voting: required votes, timeout, vote visibility, vote switching.
- Content: allowed submission types.
- AI analysis: provider, API key, model, editable full system prompt, AI auto-publish threshold.
- Publishing: forward/copy mode, publish delay, self-voting, status tags.
- Language: public bot and moderation language.
- Start message: custom `/start` greeting.
- Text limits.
- Anti-spam: user submission limit per period.
- Statistics.

Text commands still exist as shortcuts for the owner:

```text
/setvotes 2
/setdelay 15
/settimeout 24
/setfreqlimit 2 24
/setfreqlimit 0
/setmodchat -1001234567890
/allow photo on
/allow photo off
/setai on
/setai off
/setaikey openai <key>
/setaikey gemini <key>
/setaimodel gpt-5-mini-2025-08-07
/setaimodel openai gpt-5-mini-2025-08-07
/setaiprompt <full AI system prompt>
/resetaiprompt
/clearaikey
/setstart <text>
/resetstart
/banlist
/unban 123456789
/setlanguage ru
/setlanguage en
/setpublish forward
/setpublish copy
/stats
```

Regular users can use:

```text
/start
/stats
```

Unknown public commands are ignored or answered as unknown commands; they do not expose settings.

## AI Behavior

AI analysis is optional. If AI is disabled or unavailable, moderation continues normally.

When enabled:

- The original user message is forwarded to moderators immediately.
- The moderation status message first shows that AI is checking the submission.
- AI runs in a background task with a timeout.
- The same moderation status message is edited with the compact AI result.
- Moderators can vote before or after AI finishes.
- The owner can view and replace the full AI system prompt in `/settings` or with `/setaiprompt`. This prompt is used for the AI verdict and therefore also affects AI auto-publish decisions.

If AI fails, times out, or the model/API key is invalid:

- the message stays in moderation;
- moderator voting continues;
- auto-publish by AI will not trigger for that message;
- the failure is logged, but the bot process keeps running.

OpenAI model changes are checked with the current API key before saving. Gemini models are checked through the Gemini model list and a small `generateContent` test.

## Moderator Actions

The moderation message includes buttons for:

- voting yes/no;
- rejecting the submission immediately;
- contacting the author through the bot;
- banning the author.

When a moderator clicks contact or ban, the bot asks for the next message in the moderator chat. `/cancel` cancels the pending action. A ban stores `banned_at` and `ban_reason`, blocks future submissions from that user, and closes their active not-yet-approved moderation records.

## Moving Moderator Chat

To move a registered bot to another moderator group:

1. Add the registered bot to the new group.
2. From the owner account, send `/setmodchat@bot_username` in that new group.
3. New moderation requests will go to the new group.

The owner can also set a chat id from private chat:

```text
/setmodchat -1001234567890
```

Multiple registered Boterator bots can use the same moderator group. Each bot sends its own voting cards and publishes approved posts to its own target channel. For group commands, use the bot mention, for example `/setmodchat@bot_username`, so the intended bot handles the command.

## Data Retention

The bot does not keep finished moderation data forever.

By default:

- finished records in `incoming_messages` are deleted after `DATA_RETENTION_DAYS`;
- related old votes in `votes_history` are deleted when their moderation record is gone;
- active voting records and approved-but-not-yet-published records are not deleted.

This means very old `/stats` numbers can decrease after cleanup. That is intentional: the database stores operational moderation history, not permanent analytics.

## Logs

The app logs startup, warnings, errors, and cleanup summaries. No API keys are printed by the app.

The compose example enables Docker log rotation:

```yaml
logging:
  options:
    max-size: "10m"
    max-file: "3"
```

`aiogram.event`, `httpx`, and `httpcore` routine logs are muted to avoid noisy per-update/API-request logs.

## Maintenance

Check status:

```bash
docker compose ps
docker compose logs --tail=120 app
```

Restart after updating code:

```bash
docker compose up -d --build app
```

Backup database volume before risky changes:

```bash
docker compose exec postgres pg_dump -U boterator boterator > backup.sql
```

## Local Checks

```bash
uv run --python 3.12 --no-project python -m compileall boterator_server.py slaveholder_server.py boterator_app
uv run --python 3.12 --no-project --with aiogram --with asyncpg --with aiohttp --with openai python -c "from boterator_app.slave import SlaveRuntime; print('ok')"
```

## Security Notes

- Never commit `.env`, bot tokens, API keys, or server credentials.
- Rotate any token that was posted publicly or committed by mistake.
- Settings are owner-only; public users cannot open `/settings`.
- AI keys entered in Telegram are stored in the database and are not displayed back in messages.
