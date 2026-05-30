from __future__ import annotations

import base64
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp
from aiogram import Bot
from aiogram.types import Message

from .config import Settings


LOGGER = logging.getLogger(__name__)
CUSTOM_PROMPT_MAX_CHARS = 4000
SYSTEM_PROMPT_MAX_CHARS = 3200

SYSTEM_PROMPTS = {
    "ru": (
        "Ты помощник модераторов Telegram-канала. Анализируй присланное сообщение "
        "и отвечай по-русски очень кратко. Не утверждай, что можешь надежно доказать, "
        "написан ли текст ИИ: оценивай только признаки и риски. Дай субъективные проценты. "
        "Ответ строго до 650 символов, без таблиц и длинных пояснений, ровно 4 строки:\n"
        "🧭 Вердикт: опубликовать/проверить/отклонить | шанс публикации NN%\n"
        "🤖 AI-текст: NN%\n"
        "🧪 Сигналы: 2-3 коротких признака\n"
        "⚠️ Риски: 1-2 коротких риска"
    ),
    "en": (
        "You are an assistant for Telegram channel moderators. Analyze the submitted "
        "message and reply very briefly in English. Do not claim that you can reliably "
        "prove whether the text was written by AI; only assess signals and risks. "
        "Give subjective percentages. Keep the answer under 650 characters, with no "
        "tables or long explanations, exactly 4 lines:\n"
        "🧭 Verdict: publish/review/reject | publish chance NN%\n"
        "🤖 AI-written: NN%\n"
        "🧪 Signals: 2-3 short signals\n"
        "⚠️ Risks: 1-2 short risks"
    ),
}


@dataclass
class AnalysisResult:
    provider: str
    model: str
    text: str
    recommendation: str | None = None
    publish_score: int | None = None


@dataclass
class ModelValidationResult:
    ok: bool
    message: str


class AiAnalyzer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider = settings.ai_provider
        self._openai_clients: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return self.is_configured()

    def is_configured(self, ai_settings: Mapping[str, Any] | None = None) -> bool:
        provider = self.resolve_provider(ai_settings)
        return provider in {"openai", "gemini"} and bool(self._api_key(provider, ai_settings))

    def resolve_provider(self, ai_settings: Mapping[str, Any] | None = None) -> str:
        provider = str((ai_settings or {}).get("provider") or self.provider or "none").lower()
        return provider if provider in {"none", "openai", "gemini"} else "none"

    def api_key(self, provider: str, ai_settings: Mapping[str, Any] | None = None) -> str | None:
        return self._api_key(provider, ai_settings)

    async def analyze(
        self,
        bot: Bot,
        message: Message,
        ai_settings: Mapping[str, Any] | None = None,
        language: str = "ru",
    ) -> AnalysisResult | None:
        provider = self.resolve_provider(ai_settings)
        api_key = self._api_key(provider, ai_settings)
        model = self._model(provider, ai_settings)
        if provider == "none" or not api_key:
            return None

        try:
            text = _message_text(message)
            image = await self._extract_image(bot, message)
            prompt = _build_user_prompt(message, text, bool(image), language)
            system_prompt = build_system_prompt(language, ai_settings)
            if provider == "openai":
                result = await self._analyze_openai(prompt, image, api_key, model, system_prompt)
            elif provider == "gemini":
                result = await self._analyze_gemini(prompt, image, api_key, model, system_prompt)
            else:
                return None
        except Exception:
            LOGGER.exception("AI analysis failed")
            return AnalysisResult(provider=provider, model=model, text=_failure_text(language))

        recommendation, publish_score = _parse_analysis_result(result)
        return AnalysisResult(
            provider=provider,
            model=model,
            text=_trim(result, 900),
            recommendation=recommendation,
            publish_score=publish_score,
        )

    async def validate_model(
        self,
        provider: str,
        model: str,
        ai_settings: Mapping[str, Any] | None = None,
    ) -> ModelValidationResult:
        provider = self.resolve_provider({"provider": provider})
        api_key = self._api_key(provider, ai_settings)
        model = model.strip()
        if provider not in {"openai", "gemini"}:
            return ModelValidationResult(False, "Неизвестный AI-провайдер.")
        if not api_key:
            return ModelValidationResult(False, "Сначала добавьте API-ключ.")
        if not model or any(ch.isspace() for ch in model):
            return ModelValidationResult(False, "Название модели не должно быть пустым или содержать пробелы.")

        if provider == "openai":
            return await self._validate_openai_model(api_key, model)
        return await self._validate_gemini_model(api_key, model)

    def _api_key(self, provider: str, ai_settings: Mapping[str, Any] | None) -> str | None:
        settings = ai_settings or {}
        if provider == "openai":
            return settings.get("openai_api_key") or self.settings.openai_api_key
        if provider == "gemini":
            return settings.get("gemini_api_key") or self.settings.gemini_api_key
        return None

    def _model(self, provider: str, ai_settings: Mapping[str, Any] | None) -> str:
        settings = ai_settings or {}
        if provider == "openai":
            return str(settings.get("openai_model") or self.settings.openai_model)
        if provider == "gemini":
            return str(settings.get("gemini_model") or self.settings.gemini_model)
        return ""

    async def _validate_openai_model(self, api_key: str, model: str) -> ModelValidationResult:
        from openai import AsyncOpenAI

        client = self._openai_clients.get(api_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key)
            self._openai_clients[api_key] = client

        try:
            await asyncio.wait_for(client.models.retrieve(model), timeout=25)
        except Exception as exc:
            LOGGER.info("OpenAI model %s is not available: %s", model, exc.__class__.__name__)
            return ModelValidationResult(False, f"Модель {model} недоступна для этого OpenAI-ключа.")

        return ModelValidationResult(True, f"Модель {model} найдена в аккаунте и сохранена.")

    async def _validate_gemini_model(self, api_key: str, model: str) -> ModelValidationResult:
        normalized = model.removeprefix("models/")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key},
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    if response.status >= 400:
                        LOGGER.info("Gemini models list failed with status %s", response.status)
                        return ModelValidationResult(False, "Не смог проверить Gemini-модели этим ключом.")
                    body = await response.json()

                matched = None
                for item in body.get("models", []):
                    name = str(item.get("name", ""))
                    if name == f"models/{normalized}" or name.rsplit("/", 1)[-1] == normalized:
                        matched = item
                        break
                if matched is None:
                    return ModelValidationResult(False, f"Модель {normalized} недоступна для этого Gemini-ключа.")
                methods = set(matched.get("supportedGenerationMethods") or [])
                if methods and "generateContent" not in methods:
                    return ModelValidationResult(False, f"Модель {normalized} не поддерживает generateContent.")

                async with session.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{normalized}:generateContent",
                    params={"key": api_key},
                    json={
                        "contents": [{"role": "user", "parts": [{"text": "OK"}]}],
                        "generationConfig": {"maxOutputTokens": 8},
                    },
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as response:
                    if response.status >= 400:
                        LOGGER.info("Gemini model %s validation failed with status %s", normalized, response.status)
                        return ModelValidationResult(False, f"Модель {normalized} не прошла тест generateContent.")
        except Exception:
            LOGGER.info("Gemini model %s validation failed", normalized, exc_info=True)
            return ModelValidationResult(False, f"Не смог проверить модель {normalized}.")

        return ModelValidationResult(True, f"Модель {normalized} проверена и сохранена.")

    async def _extract_image(self, bot: Bot, message: Message) -> tuple[str, bytes] | None:
        file_id: str | None = None
        mime_type = "image/jpeg"
        file_size: int | None = None

        if message.photo:
            photo = message.photo[-1]
            file_id = photo.file_id
            file_size = photo.file_size
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            mime_type = message.document.mime_type or mime_type
            file_size = message.document.file_size

        if not file_id:
            return None
        if file_size and file_size > self.settings.ai_max_image_bytes:
            return None

        telegram_file = await bot.get_file(file_id)
        if telegram_file.file_size and telegram_file.file_size > self.settings.ai_max_image_bytes:
            return None
        if not telegram_file.file_path:
            return None

        stream = await bot.download_file(telegram_file.file_path)
        data = stream.read()
        if len(data) > self.settings.ai_max_image_bytes:
            return None
        return mime_type, data

    async def _analyze_openai(
        self,
        prompt: str,
        image: tuple[str, bytes] | None,
        api_key: str,
        model: str,
        system_prompt: str,
    ) -> str:
        from openai import AsyncOpenAI

        client = self._openai_clients.get(api_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key)
            self._openai_clients[api_key] = client

        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        if image:
            mime_type, data = image
            image_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
            content.append({"type": "input_image", "image_url": image_url})

        response = await client.responses.create(
            model=model,
            instructions=system_prompt,
            input=[{"role": "user", "content": content}],
            max_output_tokens=700,
        )
        return response.output_text.strip()

    async def _analyze_gemini(
        self,
        prompt: str,
        image: tuple[str, bytes] | None,
        api_key: str,
        model: str,
        system_prompt: str,
    ) -> str:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )

        parts: list[dict] = [{"text": prompt}]
        if image:
            mime_type, data = image
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            )

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                response.raise_for_status()
                body = await response.json()

        return (
            body.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )


def _message_text(message: Message) -> str:
    return message.text or message.caption or ""


def _build_user_prompt(message: Message, text: str, has_image: bool, language: str) -> str:
    if language == "en":
        parts = [
            f"Telegram message type: {message.content_type}",
            "Text/caption:",
            text.strip() or "<no text>",
        ]
        if has_image:
            parts.append("The message includes an image. Analyze it too.")
        return "\n".join(parts)

    parts = [
        f"Тип Telegram-сообщения: {message.content_type}",
        "Текст/подпись:",
        text.strip() or "<нет текста>",
    ]
    if has_image:
        parts.append("К сообщению приложено изображение. Проанализируй и его тоже.")
    return "\n".join(parts)


def build_system_prompt(language: str, ai_settings: Mapping[str, Any] | None = None) -> str:
    language = "en" if language == "en" else "ru"
    override = _system_prompt_override(ai_settings)
    if override:
        return override

    base = SYSTEM_PROMPTS[language]
    custom_prompt = _custom_prompt(ai_settings)
    if not custom_prompt:
        return base
    if language == "en":
        return _trim_system_prompt(
            f"{base}\n\n"
            "Owner channel context and moderation rules:\n"
            f"{custom_prompt}\n\n"
            "Treat these owner rules as authoritative. If the submission is off-topic "
            "or violates them, use verdict reject/review and lower the publish chance. "
            "Still keep the required 4-line format."
        )
    return _trim_system_prompt(
        f"{base}\n\n"
        "Дополнительный контекст канала и правила владельца:\n"
        f"{custom_prompt}\n\n"
        "Считай эти правила приоритетными. Если сообщение не подходит тематике канала "
        "или нарушает их, ставь вердикт отклонить/проверить и снижай шанс публикации. "
        "Формат ответа все равно ровно 4 строки."
    )


def _system_prompt_override(ai_settings: Mapping[str, Any] | None) -> str:
    value = (ai_settings or {}).get("system_prompt")
    if not isinstance(value, str):
        return ""
    return _trim_system_prompt(value.strip())


def _custom_prompt(ai_settings: Mapping[str, Any] | None) -> str:
    value = (ai_settings or {}).get("custom_prompt")
    if not isinstance(value, str):
        return ""
    return value.strip()[:CUSTOM_PROMPT_MAX_CHARS]


def _trim_system_prompt(value: str) -> str:
    value = value.strip()
    if len(value) <= SYSTEM_PROMPT_MAX_CHARS:
        return value
    return value[:SYSTEM_PROMPT_MAX_CHARS].rstrip()


def _failure_text(language: str) -> str:
    if language == "en":
        return "AI analysis is temporarily unavailable."
    return "AI-анализ временно недоступен."


def _trim(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _parse_analysis_result(value: str) -> tuple[str | None, int | None]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    first_line = lines[0].lower() if lines else value.lower()

    recommendation: str | None = None
    if "отклон" in first_line or re.search(r"\breject\b", first_line):
        recommendation = "reject"
    elif "провер" in first_line or re.search(r"\breview\b", first_line):
        recommendation = "review"
    elif "опубликов" in first_line or re.search(r"\bpublish\b", first_line):
        recommendation = "publish"

    score: int | None = None
    patterns = [
        r"(?:шанс публикации|publish chance)\D{0,24}(\d{1,3})\s*%",
        r"(\d{1,3})\s*%",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            score = max(0, min(100, int(match.group(1))))
            break

    return recommendation, score
