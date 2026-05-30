from __future__ import annotations

from typing import Any


LANGUAGE_LABELS = {
    "ru": "Русский",
    "en": "English",
}

DEFAULT_START_TEXTS = {
    "ru": "👋 Пришлите сообщение, и я отправлю его на модерацию.",
    "en": "👋 Send me a message and I will submit it for moderation.",
}

DEFAULT_HELLO_TEXTS = {
    "ru": (
        "✨ Теперь в этот канал можно предложить публикацию через @{bot_username}. "
        "Пришлите сообщение боту, и оно попадет на проверку модераторам."
    ),
    "en": (
        "✨ You can now submit posts to this channel via @{bot_username}. "
        "Send a message to the bot and moderators will review it."
    ),
}

CONTENT_LABELS = {
    "ru": {
        "text": "текст",
        "photo": "фото",
        "voice": "голосовые сообщения",
        "video": "видео",
        "audio": "аудио",
        "document": "документы",
        "sticker": "стикеры",
        "gif": "GIF",
    },
    "en": {
        "text": "text",
        "photo": "photo",
        "voice": "voice messages",
        "video": "video",
        "audio": "audio",
        "document": "documents",
        "sticker": "stickers",
        "gif": "GIF",
    },
}

PUBLIC_TEXTS = {
    "ru": {
        "unknown_command": "❓ Неизвестная команда. Чтобы предложить публикацию, пришлите текст или фото.",
        "access_denied": "🔒 Доступ запрещен.",
        "unsupported_content": "⚠️ Этот тип сообщения пока не поддерживается.",
        "content_disabled": "🚫 Тип контента {content} сейчас отключен.",
        "text_limits": "✍️ Текст должен быть длиной от {min} до {max} символов.",
        "rate_limit_exceeded": "🚦 Лимит отправки исчерпан: можно отправить не больше {count} сообщений за {hours} ч. Попробуйте позже.",
        "confirm_submit": "📨 Отправить сообщение на модерацию?",
        "button_submit": "✅ Отправить",
        "button_cancel": "↩️ Отменить",
        "submission_cancelled": "↩️ Отправка отменена.",
        "repeat_submission": "🔁 Повторите отправку сообщения.",
        "sending_to_mods": "📨 Отправляю сообщение модераторам...",
        "send_to_mods_failed": "⚠️ Не смог отправить сообщение модераторам. Попробуйте позже.",
        "sent_to_moderation": "✅ Сообщение отправлено на модерацию.",
        "self_vote_denied": "🚫 Нельзя голосовать за собственное сообщение.",
        "voting_closed": "🔒 Голосование уже закрыто.",
        "vote_already_counted": "☑️ Ваш голос уже учтен.",
        "author_approved": "✅ Ваше сообщение одобрено и поставлено в очередь.",
        "vote_counted_approved": "✅ Голос учтен. Сообщение одобрено.",
        "author_rejected": "❌ К сожалению, сообщение не прошло модерацию.",
        "author_rejected_with_ai": "❌ К сожалению, сообщение не прошло модерацию.\n\n🤖 AI-анализ:\n{analysis}",
        "vote_counted_rejected": "❌ Голос учтен. Сообщение отклонено.",
        "vote_counted": "🗳️ Голос учтен.",
        "message_rejected": "❌ Сообщение отклонено.",
        "vote_expired": "⏳ К сожалению, голосование истекло.",
        "vote_expired_with_ai": "⏳ К сожалению, голосование истекло, и сообщение не было опубликовано.\n\n🤖 AI-анализ:\n{analysis}",
        "moderation_title": "🛡️ Модерация сообщения",
        "moderation_votes": "🗳️ Голоса: за {yes}/{needed}, против {no}/{needed} (всего {total})",
        "ai_analysis_heading": "🤖 AI-анализ ({provider}):",
        "ai_analysis_checking": "🤖 AI-анализ ({provider}): проверяю сообщение...",
        "ai_analysis_failed": "AI-анализ временно недоступен.",
        "status_published": "✅ Статус: опубликовано.",
        "status_ai_auto_approved": "🤖 Статус: автоодобрено AI после таймаута, ожидает публикации.",
        "status_approved_waiting": "⏳ Статус: одобрено, ожидает публикации.",
        "status_rejected": "❌ Статус: отклонено.",
        "status_closed": "🔒 Статус: голосование закрыто.",
        "moderation_question": "⚙️ Что делаем?",
        "button_yes": "✅ За {count}",
        "button_no": "❌ Против {count}",
        "button_reject": "🚫 Отклонить",
        "button_contact": "💬 Связаться",
        "button_ban": "⛔ Бан",
        "moderation_message_not_found": "⚠️ Сообщение не найдено.",
        "moderator_action_cancelled": "↩️ Действие отменено.",
        "contact_prompt": "💬 Пришлите сообщение для автора. /cancel - отмена.",
        "contact_empty": "⚠️ Пришлите текст сообщения или /cancel.",
        "contact_too_long": "⚠️ Сообщение слишком длинное. Максимум {max} символов.",
        "contact_to_author": "💬 Сообщение от модератора:\n\n{text}",
        "contact_sent": "✅ Сообщение автору отправлено.",
        "contact_failed": "⚠️ Не смог отправить сообщение автору. Возможно, он заблокировал бота.",
        "ban_prompt": "⛔ Пришлите причину бана для пользователя. /cancel - отмена.",
        "ban_reason_too_short": "⚠️ Причина слишком короткая. Минимум {min} символов или /cancel.",
        "ban_owner_denied": "🚫 Нельзя забанить владельца бота.",
        "ban_self_denied": "🚫 Нельзя забанить самого себя.",
        "ban_already": "ℹ️ Пользователь уже забанен.",
        "ban_notification": "⛔ Вы забанены и больше не можете отправлять сообщения на модерацию.\nПричина: {reason}",
        "ban_done": "⛔ Пользователь забанен. Активных заявок закрыто: {count}.",
        "ban_failed": "⚠️ Не смог забанить пользователя.",
        "unban_notification": "🔓 Доступ к боту восстановлен.",
        "user_stats_title": "📊 Ваша статистика",
        "user_stats_total": "📨 Отправлено: {count}",
        "user_stats_review": "🛡️ На модерации: {count}",
        "user_stats_queued": "⏳ Одобрено, ждет публикации: {count}",
        "user_stats_published": "✅ Опубликовано: {count}",
        "user_stats_rejected": "❌ Отклонено: {count}",
    },
    "en": {
        "unknown_command": "❓ Unknown command. To submit a post, send text or a photo.",
        "access_denied": "🔒 Access denied.",
        "unsupported_content": "⚠️ This message type is not supported yet.",
        "content_disabled": "🚫 {content} submissions are currently disabled.",
        "text_limits": "✍️ Text must be between {min} and {max} characters.",
        "rate_limit_exceeded": "🚦 Submission limit reached: you can submit up to {count} messages per {hours} h. Please try again later.",
        "confirm_submit": "📨 Submit this message for moderation?",
        "button_submit": "✅ Submit",
        "button_cancel": "↩️ Cancel",
        "submission_cancelled": "↩️ Submission cancelled.",
        "repeat_submission": "🔁 Please submit the message again.",
        "sending_to_mods": "📨 Sending the message to moderators...",
        "send_to_mods_failed": "⚠️ Could not send the message to moderators. Please try again later.",
        "sent_to_moderation": "✅ Message sent for moderation.",
        "self_vote_denied": "🚫 You cannot vote for your own message.",
        "voting_closed": "🔒 Voting is already closed.",
        "vote_already_counted": "☑️ Your vote has already been counted.",
        "author_approved": "✅ Your message was approved and queued for publishing.",
        "vote_counted_approved": "✅ Vote counted. The message was approved.",
        "author_rejected": "❌ Unfortunately, your message did not pass moderation.",
        "author_rejected_with_ai": "❌ Unfortunately, your message did not pass moderation.\n\n🤖 AI analysis:\n{analysis}",
        "vote_counted_rejected": "❌ Vote counted. The message was rejected.",
        "vote_counted": "🗳️ Vote counted.",
        "message_rejected": "❌ Message rejected.",
        "vote_expired": "⏳ Unfortunately, the voting timeout has expired.",
        "vote_expired_with_ai": "⏳ Unfortunately, the voting timeout has expired, and the message was not published.\n\n🤖 AI analysis:\n{analysis}",
        "moderation_title": "🛡️ Message moderation",
        "moderation_votes": "🗳️ Votes: yes {yes}/{needed}, no {no}/{needed} ({total} total)",
        "ai_analysis_heading": "🤖 AI analysis ({provider}):",
        "ai_analysis_checking": "🤖 AI analysis ({provider}): checking the message...",
        "ai_analysis_failed": "AI analysis is temporarily unavailable.",
        "status_published": "✅ Status: published.",
        "status_ai_auto_approved": "🤖 Status: auto-approved by AI after timeout, waiting to publish.",
        "status_approved_waiting": "⏳ Status: approved, waiting to be published.",
        "status_rejected": "❌ Status: rejected.",
        "status_closed": "🔒 Status: voting closed.",
        "moderation_question": "⚙️ Action?",
        "button_yes": "✅ Yes {count}",
        "button_no": "❌ No {count}",
        "button_reject": "🚫 Reject",
        "button_contact": "💬 Contact",
        "button_ban": "⛔ Ban",
        "moderation_message_not_found": "⚠️ Message not found.",
        "moderator_action_cancelled": "↩️ Action cancelled.",
        "contact_prompt": "💬 Send the message for the author. /cancel cancels.",
        "contact_empty": "⚠️ Send message text or /cancel.",
        "contact_too_long": "⚠️ Message is too long. Maximum is {max} characters.",
        "contact_to_author": "💬 Message from a moderator:\n\n{text}",
        "contact_sent": "✅ Message sent to the author.",
        "contact_failed": "⚠️ Could not message the author. They may have blocked the bot.",
        "ban_prompt": "⛔ Send the ban reason for this user. /cancel cancels.",
        "ban_reason_too_short": "⚠️ Reason is too short. Minimum is {min} characters or /cancel.",
        "ban_owner_denied": "🚫 The bot owner cannot be banned.",
        "ban_self_denied": "🚫 You cannot ban yourself.",
        "ban_already": "ℹ️ User is already banned.",
        "ban_notification": "⛔ You have been banned and can no longer submit messages for moderation.\nReason: {reason}",
        "ban_done": "⛔ User banned. Active submissions closed: {count}.",
        "ban_failed": "⚠️ Could not ban the user.",
        "unban_notification": "🔓 Bot access has been restored.",
        "user_stats_title": "📊 Your stats",
        "user_stats_total": "📨 Submitted: {count}",
        "user_stats_review": "🛡️ Under review: {count}",
        "user_stats_queued": "⏳ Approved, waiting to publish: {count}",
        "user_stats_published": "✅ Published: {count}",
        "user_stats_rejected": "❌ Rejected: {count}",
    },
}


def normalize_language(value: Any) -> str:
    language = str(value or "ru").lower()
    return language if language in LANGUAGE_LABELS else "ru"


def translate(language: Any, key: str, **kwargs: Any) -> str:
    normalized = normalize_language(language)
    template = PUBLIC_TEXTS.get(normalized, PUBLIC_TEXTS["ru"]).get(key, PUBLIC_TEXTS["ru"][key])
    return template.format(**kwargs)


def content_label(language: Any, key: str) -> str:
    normalized = normalize_language(language)
    return CONTENT_LABELS.get(normalized, CONTENT_LABELS["ru"]).get(key, key)


def default_text(language: Any, value: str | None, defaults: dict[str, str]) -> str:
    normalized = normalize_language(language)
    if not value or value in defaults.values():
        return defaults[normalized]
    return str(value)
